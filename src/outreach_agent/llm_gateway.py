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

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import Config
from .errors import (
    LlmBackendError,
    LlmBudgetError,
    LlmCliError,
    LlmUnavailableError,
    SecretLeakError,
)
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


def _default_scratch_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "outreach-agent" / "claude-scratch"


class ClaudeCodeLLMClient:
    """NFR-7 default backend: headless Claude Code CLI (`claude -p`) on the
    host, riding the user's subscription — no API key required.

    Ground source (verified on the LOCAL install, 2026-06-12, zero web):
    `claude --help` confirms `-p/--print` (non-interactive), `--output-format
    json|text` (print-only), `--model <model>`, `--system-prompt <prompt>`,
    `--tools ""` (disable all tools), `--disable-slash-commands`,
    `--no-session-persistence`. A live probe (`"Reply with exactly: OK" |
    claude -p --output-format json ...`) confirmed: prompt is read from
    stdin; output is one JSON object with `result` (text), `is_error`,
    `subtype`, and `usage.input_tokens`/`usage.output_tokens`; exit code 0.
    A failure probe (bogus `--model`) confirmed: exit code 1 with a one-line
    diagnostic. `claude` resolves to a native `claude.exe` (not a .cmd
    shim), so list-argv CreateProcess escaping is safe.

    Injection containment (decision): the subprocess cwd is a dedicated
    EMPTY scratch directory, never the repo work dir. Claude Code
    auto-discovers CLAUDE.md/settings from its cwd — running it inside a
    cloned third-party repo would let that repo's CLAUDE.md steer
    generation (prompt-injection vector). Defence in depth: `--tools ""`,
    `--disable-slash-commands`, and `--no-session-persistence` reduce the
    CLI to pure text generation. `--bare` is deliberately NOT used: its
    help states OAuth/keychain auth is never read under --bare, which
    would break the subscription auth NFR-7 exists to use.

    Notes:
    - The prompt goes via STDIN (never argv) so it stays out of process
      lists; only the agent-authored system template rides argv.
    - `max_tokens` is accepted for protocol compatibility but the CLI
      exposes no output-token cap flag (verified in --help); it is not
      enforced on this backend.
    - `subscription_backed = True`: the gateway records 0-cost ledger
      entries (calls + reported tokens kept for observability) and the
      F-13 monthly spend cap does not gate this backend.
    """

    subscription_backed = True

    def __init__(self, executable: str, *, timeout_s: float,
                 scratch_dir: Path | None = None) -> None:
        self._executable = executable
        self._timeout_s = timeout_s
        # Created lazily at call time, not construction time, so building
        # the client (e.g. in the factory) never touches the filesystem.
        self._scratch_dir = scratch_dir or _default_scratch_dir()

    def _argv(self, *, model: str, system: str) -> list[str]:
        return [
            self._executable, "-p",
            "--output-format", "json",
            "--model", model,
            "--system-prompt", system,
            "--tools", "",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]

    def complete(self, *, model: str, system: str, prompt: str,
                 max_tokens: int) -> LLMCompletion:
        argv = self._argv(model=model, system=system)
        self._scratch_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                argv,
                input=prompt,                # stdin — never argv (verified)
                capture_output=True, text=True,
                # CLI output is UTF-8; never decode as the Windows locale
                # codec (cp1252 trap — same rationale as sandbox.py).
                encoding="utf-8", errors="replace",
                timeout=self._timeout_s,
                cwd=str(self._scratch_dir),  # injection containment, see above
            )
        except subprocess.TimeoutExpired as exc:
            raise LlmUnavailableError(
                f"claude CLI timed out after {self._timeout_s}s (retriable)"
            ) from exc
        except OSError as exc:
            raise LlmBackendError(
                f"failed to launch claude CLI at {self._executable!r}: {exc}"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise LlmCliError(
                f"claude CLI exited {proc.returncode}: "
                f"{detail[0][:300] if detail else '<no output>'}"
            )
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise LlmCliError(
                "claude CLI returned non-JSON output despite "
                "--output-format json (first 200 chars: "
                f"{proc.stdout[:200]!r})"
            ) from exc
        if data.get("is_error"):
            raise LlmCliError(
                f"claude CLI reported an error result "
                f"(subtype={data.get('subtype')!r})"
            )
        usage = data.get("usage") or {}
        return LLMCompletion(
            text=str(data.get("result") or ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )


def build_llm_client(config: Config, token_source=None) -> LLMClient:
    """NFR-7 backend factory — the single place backend selection happens.

    claude-code (default): requires the `claude` CLI on PATH; no API key.
    anthropic: requires the keyring-stored API key (CredentialError with
    remediation if absent — the key is OPTIONAL overall per NFR-7).
    """
    backend = config.llm_backend
    if backend == "claude-code":
        exe = shutil.which(config.claude_cli_executable)
        if exe is None:
            raise LlmBackendError(
                f"llm_backend=claude-code but {config.claude_cli_executable!r} "
                "was not found on PATH; install Claude Code "
                "(https://claude.com/claude-code, e.g. `npm install -g "
                "@anthropic-ai/claude-code`) or set llm_backend=anthropic"
            )
        return ClaudeCodeLLMClient(exe, timeout_s=config.claude_cli_timeout_s)
    if backend == "anthropic":
        if token_source is None:
            from .tokens import KeyringTokenSource
            token_source = KeyringTokenSource()
        return AnthropicLLMClient(
            token_source.anthropic_api_key(),
            timeout_s=config.llm_timeout_s,
            max_retries=config.llm_max_retries,
        )
    raise LlmBackendError(
        f"unknown llm_backend {backend!r}; expected 'claude-code' or 'anthropic'"
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
                      completion: LLMCompletion,
                      zero_cost: bool = False) -> float:
        # NFR-7: subscription-backed backends (claude-code) record 0-cost
        # entries — the call and reported tokens are still ledgered for
        # observability, but no dollars accrue toward the F-13 cap.
        if zero_cost:
            cost = 0.0
        else:
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
        # NFR-6/NFR-7: the outbound secret check is backend-agnostic — it
        # runs HERE, before ANY client (API call or CLI subprocess) sees
        # the text. Fail-closed for every backend identically.
        self.assert_no_secrets(system, prompt)
        # F-13: the monthly spend cap gates API-spend backends only; a
        # subscription-backed client (claude-code) spends no API dollars.
        subscription = bool(getattr(self.client, "subscription_backed", False))
        if not subscription:
            self._assert_budget()
        completion = self.client.complete(
            model=model, system=system, prompt=prompt, max_tokens=max_tokens,
        )
        self._record_spend(model=model, purpose=purpose, completion=completion,
                           zero_cost=subscription)
        return completion.text
