"""LLMGateway — ADR §7 (F-13, FM9, NFR-6). All Claude call sites route here.

Ground source: installed anthropic SDK 0.109.1 —
`Anthropic(api_key=, timeout=, max_retries=)` and
`messages.create(max_tokens=, messages=, model=, system=, ...)` verified via
inspect.signature; `Usage.input_tokens`/`output_tokens` fields confirmed.
The SDK's built-in max_retries handles 5xx/connection retries (config-pinned
to 2); 4xx is non-retriable per §7.

Safety (NFR-6): outbound deny-regex over every prompt/system string fails
closed BEFORE any network send. Spend: hard monthly cap from the llm_spend
SQLite ledger checked BEFORE each call (F-13 hard stop → llm-blocked).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .config import Config
from .errors import LlmBudgetError, LlmUnavailableError, SecretLeakError
from .outbound_safety import (
    loaded_secret_values,
    normalize_outbound_text,
    register_secret_value,
)
from .persistence import Database, new_ulid, utc_now_iso

# Prefix-based, fail-closed (ADR §7): GitHub tokens, Anthropic keys, PEM blocks.
# M-1 hardening: this regex is a defence-in-depth BACKSTOP, not the primary
# secret boundary. It runs over NFKC-normalized, zero-width-stripped text
# (homoglyph/split evasion), and is paired with exact VALUE matching against
# every credential loaded in-process (outbound_safety registry) — which covers
# the GitHub OAuth client secret and anything else with no fixed prefix.
_DENY = re.compile(
    r"(ghp_|github_pat_|sk-ant-|-----BEGIN[ A-Z]*PRIVATE KEY-----)"
)


@dataclass(frozen=True)
class LLMCompletion:
    text: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    def complete(self, *, model: str, system: str, prompt: str,
                 max_tokens: int) -> LLMCompletion: ...


class AnthropicLLMClient:
    """anthropic-SDK-backed implementation; constructed lazily so the mocked
    CI lane never imports a live transport."""

    def __init__(self, api_key: str, *, timeout_s: float, max_retries: int) -> None:
        import anthropic

        # M-1: any credential entering the process is value-registered so the
        # outbound guard can redact it by exact value, fail-closed.
        register_secret_value(api_key)
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries,
        )

    def complete(self, *, model: str, system: str, prompt: str,
                 max_tokens: int) -> LLMCompletion:
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except (self._anthropic.APITimeoutError,
                self._anthropic.APIConnectionError) as exc:
            raise LlmUnavailableError(f"Claude API unreachable after retries: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LlmUnavailableError(
                    f"Claude API {exc.status_code} after retries"
                ) from exc
            raise
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        return LLMCompletion(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )


class FakeLLMClient:
    """Test seam: canned completions; records every request."""

    def __init__(self, responses: list[str] | None = None,
                 *, input_tokens: int = 1000, output_tokens: int = 500) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, object]] = []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.fail_next: Exception | None = None

    def complete(self, *, model: str, system: str, prompt: str,
                 max_tokens: int) -> LLMCompletion:
        self.calls.append(dict(model=model, system=system, prompt=prompt,
                               max_tokens=max_tokens))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        text = self.responses.pop(0) if self.responses else "fake completion"
        return LLMCompletion(text, self.input_tokens, self.output_tokens)


class LLMGateway:
    def __init__(self, client: LLMClient, db: Database, config: Config) -> None:
        self.client = client
        self.db = db
        self.config = config
        self._prices = dict(
            (model, (p_in, p_out)) for model, p_in, p_out in config.llm_prices_per_mtok
        )

    # -- spend ledger (F-13 hard stop) ----------------------------------------

    def _price(self, model: str) -> tuple[float, float]:
        if model in self._prices:
            return self._prices[model]
        # Unknown model → most expensive known rate: overcounts toward the cap,
        # never under (fail-closed budget direction).
        return max(self._prices.values()) if self._prices else (5.0, 25.0)

    def month_spend_usd(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self.db.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_spend WHERE ts >= ?",
            (month_start.isoformat(timespec="microseconds"),),
        ).fetchone()
        return float(row["total"])

    def _assert_budget(self) -> None:
        spend = self.month_spend_usd()
        if spend >= self.config.llm_monthly_spend_cap_usd:
            raise LlmBudgetError(
                f"monthly LLM spend ${spend:.2f} >= cap "
                f"${self.config.llm_monthly_spend_cap_usd:.2f} — hard stop (F-13)"
            )

    def _record_spend(self, *, model: str, purpose: str,
                      completion: LLMCompletion) -> float:
        p_in, p_out = self._price(model)
        cost = (completion.input_tokens / 1e6) * p_in \
            + (completion.output_tokens / 1e6) * p_out
        with self.db.transaction():
            self.db.conn.execute(
                "INSERT INTO llm_spend(entry_id, ts, model, purpose, input_tokens,"
                " output_tokens, cost_usd) VALUES(?,?,?,?,?,?,?)",
                (new_ulid(), utc_now_iso(), model, purpose,
                 completion.input_tokens, completion.output_tokens, round(cost, 6)),
            )
        return cost

    # -- NFR-6 outbound deny-regex (fail-closed, prompt path only) -------------

    @staticmethod
    def assert_no_secrets(*texts: str) -> None:
        """NFR-6 + M-1: normalize, then (a) deny-regex, (b) exact-value match
        against every loaded credential. Fails closed BEFORE any send. The
        value-match error never echoes the value itself."""
        secret_values = loaded_secret_values()
        for text in texts:
            normalized = normalize_outbound_text(text)
            match = _DENY.search(normalized)
            if match:
                raise SecretLeakError(
                    f"outbound text matched deny pattern {match.group(0)[:12]!r} — "
                    "refusing to send (NFR-6 fail-closed)"
                )
            for value in secret_values:
                if value in normalized:
                    raise SecretLeakError(
                        "outbound text contains the value of a loaded credential "
                        "— refusing to send (NFR-6/M-1 fail-closed)"
                    )

    # -- §7 entrypoint ----------------------------------------------------------

    def generate(self, *, purpose: str, system: str, prompt: str,
                 model: str | None = None,
                 max_tokens: int | None = None) -> str:
        model = model or self.config.model
        max_tokens = max_tokens or self.config.llm_max_output_tokens
        self.assert_no_secrets(system, prompt)
        self._assert_budget()
        completion = self.client.complete(
            model=model, system=system, prompt=prompt, max_tokens=max_tokens,
        )
        self._record_spend(model=model, purpose=purpose, completion=completion)
        return completion.text
