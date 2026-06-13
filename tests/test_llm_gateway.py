from __future__ import annotations

import pytest

from outreach_agent.config import Config
from outreach_agent.errors import LlmBudgetError, SecretLeakError
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.outbound_safety import (
    loaded_secret_values,
    register_secret_value,
)
from outreach_agent.persistence import Database


@pytest.fixture
def llm(db: Database, config: Config) -> LLMGateway:
    return LLMGateway(FakeLLMClient(["a patch"]), db, config)


def test_generate_records_spend(llm: LLMGateway, db: Database) -> None:
    text = llm.generate(purpose="fix-generation", system="sys", prompt="fix it")
    assert text == "a patch"
    row = db.conn.execute("SELECT * FROM llm_spend").fetchone()
    assert row["model"] == "claude-opus-4-8"
    assert row["purpose"] == "fix-generation"
    # 1000 in @ $5/MTok + 500 out @ $25/MTok = 0.005 + 0.0125
    assert abs(row["cost_usd"] - 0.0175) < 1e-9
    assert llm.month_spend_usd() > 0


@pytest.mark.parametrize("secret", [
    "token ghp_abc123",
    "key github_pat_11AABBCC",
    "anthropic sk-ant-api03-xyz",
    "-----BEGIN RSA PRIVATE KEY-----",
])
def test_deny_regex_fails_closed_before_send(db: Database, config: Config,
                                             secret: str) -> None:
    """NFR-6: outbound deny-regex — nothing is sent, nothing is spent."""
    client = FakeLLMClient()
    gateway = LLMGateway(client, db, config)
    with pytest.raises(SecretLeakError):
        gateway.generate(purpose="x", system="sys", prompt=f"context: {secret}")
    assert client.calls == []
    assert db.conn.execute("SELECT COUNT(*) AS n FROM llm_spend").fetchone()["n"] == 0


def test_monthly_spend_cap_hard_stop(db: Database, config: Config) -> None:
    """F-13: cap reached → LlmBudgetError BEFORE the call (→ llm-blocked)."""
    import dataclasses

    tight = dataclasses.replace(config, llm_monthly_spend_cap_usd=0.01)
    client = FakeLLMClient(["one"], input_tokens=1_000_000, output_tokens=0)
    gateway = LLMGateway(client, db, tight)
    gateway.generate(purpose="a", system="s", prompt="p")  # $5 spent
    with pytest.raises(LlmBudgetError):
        gateway.generate(purpose="b", system="s", prompt="p")
    assert len(client.calls) == 1  # second call never reached the client


def test_unknown_model_charged_at_max_known_rate(db: Database, config: Config) -> None:
    client = FakeLLMClient(["x"], input_tokens=1_000_000, output_tokens=0)
    gateway = LLMGateway(client, db, config)
    gateway.generate(purpose="t", system="s", prompt="p", model="mystery-model")
    row = db.conn.execute("SELECT cost_usd FROM llm_spend").fetchone()
    assert row["cost_usd"] == 5.0  # opus input rate, never an undercount


# -- M-1 (audit step 6): value-redaction + normalization -----------------------


def test_loaded_credential_value_redaction_fails_closed(db: Database,
                                                        config: Config) -> None:
    """M-1: a prompt containing the VALUE of any loaded credential (e.g. the
    GitHub OAuth client secret, which has no prefix the regex could match)
    is refused before send; nothing sent, nothing spent, value not echoed."""
    register_secret_value("oauth-client-secret-9f8e7d6c5b4a")
    client = FakeLLMClient()
    gateway = LLMGateway(client, db, config)
    with pytest.raises(SecretLeakError) as exc_info:
        gateway.generate(purpose="x", system="s",
                         prompt="cfg dump: oauth-client-secret-9f8e7d6c5b4a end")
    assert "oauth-client-secret" not in str(exc_info.value)  # never echoed
    assert client.calls == []
    assert db.conn.execute("SELECT COUNT(*) AS n FROM llm_spend").fetchone()["n"] == 0


def test_value_redaction_catches_zero_width_split(db: Database,
                                                  config: Config) -> None:
    """M-1: zero-width chars inside the credential value do not evade the
    exact-value match (text is normalized before matching)."""
    register_secret_value("oauth-client-secret-9f8e7d6c5b4a")
    client = FakeLLMClient()
    gateway = LLMGateway(client, db, config)
    with pytest.raises(SecretLeakError):
        gateway.generate(purpose="x", system="s",
                         prompt="oauth-client-\u200bsecret-9f8e7d6c5b4a")
    assert client.calls == []


@pytest.mark.parametrize("evasion", [
    "token ghp\u200b_abc123",      # zero-width split of the prefix
    "token ｇｈｐ＿abc123",         # fullwidth homoglyphs (NFKC → ghp_)
    "key github\u2060_pat_11AA",   # word-joiner split
])
def test_deny_regex_normalization_blunts_homoglyph_and_split(
        db: Database, config: Config, evasion: str) -> None:
    """M-1: the deny-regex runs over NFKC-normalized, zero-width-stripped
    text, so split/homoglyph variants of the known prefixes still match."""
    client = FakeLLMClient()
    gateway = LLMGateway(client, db, config)
    with pytest.raises(SecretLeakError):
        gateway.generate(purpose="x", system="s", prompt=evasion)
    assert client.calls == []


def test_keyring_loaded_credentials_are_value_registered(monkeypatch) -> None:
    """M-1 wiring: every credential fetched through KeyringTokenSource is
    registered for value-redaction — including the OAuth client secret."""
    import keyring

    from outreach_agent.tokens import KeyringTokenSource

    monkeypatch.setattr(
        keyring, "get_password",
        lambda service, name: f"kr-{name}-value-0123456789abcdef",
    )
    source = KeyringTokenSource()
    secret = source.oauth_client_secret()
    token = source.github_token()
    values = loaded_secret_values()
    assert secret in values and token in values


def test_trivially_short_values_are_not_registered() -> None:
    """M-1 guard: registering a <8-char string is a no-op (substring matching
    on e.g. 'abc' would refuse virtually every prompt)."""
    register_secret_value("short")
    assert "short" not in loaded_secret_values()
