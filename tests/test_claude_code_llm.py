"""NFR-7 — Claude Code CLI backend (default) + backend selection factory.

Mocked lane: `subprocess.run` is monkeypatched at the llm_gateway module
seam — the real `claude` CLI is NEVER invoked here (cf. testing rule: no
real external services in the default lane). The one real invocation lives
in the opt-in `local` marker lane at the bottom (deselected by default).

CLI interface ground truth (local `claude --help` + live probes,
2026-06-12): `-p` non-interactive, prompt via stdin, `--output-format
json` → single JSON object with `result`, `is_error`, `subtype`,
`usage.input_tokens/output_tokens`; failure (bogus model) → exit 1.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from outreach_agent.config import Config
from outreach_agent.errors import (
    CredentialError,
    LlmBackendError,
    LlmCliError,
    LlmUnavailableError,
    SecretLeakError,
)
from outreach_agent.llm_gateway import (
    AnthropicLLMClient,
    ClaudeCodeLLMClient,
    LLMGateway,
    build_llm_client,
)
from outreach_agent.persistence import Database

# Mirrors the schema observed in the live probe (trimmed to fields we read).
_RESULT_JSON = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "a generated patch",
    "usage": {"input_tokens": 571, "output_tokens": 4},
    "total_cost_usd": 0.2,
})


def _completed(stdout: str = _RESULT_JSON, returncode: int = 0,
               stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


@pytest.fixture
def cli_client(tmp_path: Path) -> ClaudeCodeLLMClient:
    return ClaudeCodeLLMClient(
        r"C:\fake\claude.exe", timeout_s=300,
        scratch_dir=tmp_path / "scratch",
    )


# -- backend selection (factory) ------------------------------------------------


def test_default_backend_is_claude_code() -> None:
    assert Config(db_path=Path("x")).llm_backend == "claude-code"


def test_factory_builds_cli_client_when_cli_present(config: Config,
                                                    monkeypatch) -> None:
    import outreach_agent.llm_gateway as lg

    monkeypatch.setattr(lg.shutil, "which", lambda name: r"C:\bin\claude.exe")
    client = build_llm_client(config)
    assert isinstance(client, ClaudeCodeLLMClient)
    assert client.subscription_backed is True


def test_factory_cli_absent_names_the_install(config: Config, monkeypatch) -> None:
    import outreach_agent.llm_gateway as lg

    monkeypatch.setattr(lg.shutil, "which", lambda name: None)
    with pytest.raises(LlmBackendError) as exc_info:
        build_llm_client(config)
    detail = str(exc_info.value)
    assert "claude" in detail and "install" in detail.lower()
    assert exc_info.value.problem.retriable is False


def test_factory_anthropic_without_key_raises_credential_error(
        config: Config, monkeypatch) -> None:
    """NFR-7: the key is optional ONLY because the default backend does not
    need it — selecting anthropic without a stored key still fails with the
    remediation-carrying CredentialError."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda service, name: None)
    cfg = dataclasses.replace(config, llm_backend="anthropic")
    with pytest.raises(CredentialError) as exc_info:
        build_llm_client(cfg)
    assert "keyring set outreach-agent anthropic_api_key" in str(exc_info.value)


def test_factory_anthropic_with_key_builds_api_client(config: Config,
                                                      monkeypatch) -> None:
    import keyring

    monkeypatch.setattr(
        keyring, "get_password",
        lambda service, name: "sk-test-key-0123456789",
    )
    cfg = dataclasses.replace(config, llm_backend="anthropic")
    client = build_llm_client(cfg)
    assert isinstance(client, AnthropicLLMClient)
    assert getattr(client, "subscription_backed", False) is False


def test_factory_unknown_backend_rejected(config: Config) -> None:
    cfg = dataclasses.replace(config, llm_backend="openai")
    with pytest.raises(LlmBackendError):
        build_llm_client(cfg)


# -- subprocess invocation contract ----------------------------------------------


def test_argv_stdin_encoding_and_neutral_cwd(cli_client: ClaudeCodeLLMClient,
                                             tmp_path: Path,
                                             monkeypatch) -> None:
    """Argv is a LIST (no shell), prompt rides STDIN (never argv), decoding
    is utf-8/replace (cp1252 trap), and cwd is the empty scratch dir — NOT
    the repo work dir (CLAUDE.md prompt-injection containment)."""
    import outreach_agent.llm_gateway as lg

    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _completed()

    monkeypatch.setattr(lg.subprocess, "run", fake_run)
    completion = cli_client.complete(
        model="claude-opus-4-8", system="you fix bugs",
        prompt="fix the bug", max_tokens=8192,
    )

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0] == r"C:\fake\claude.exe"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert argv[argv.index("--system-prompt") + 1] == "you fix bugs"
    assert argv[argv.index("--tools") + 1] == ""          # tools disabled
    assert "--disable-slash-commands" in argv
    assert "--no-session-persistence" in argv
    assert "fix the bug" not in argv                       # prompt never in argv

    kwargs = seen["kwargs"]
    assert kwargs["input"] == "fix the bug"                # stdin
    assert kwargs.get("shell", False) is False             # never shell=True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["timeout"] == 300
    scratch = Path(kwargs["cwd"])
    assert scratch == tmp_path / "scratch"
    assert scratch.is_dir() and not any(scratch.iterdir())  # neutral + empty

    assert completion.text == "a generated patch"
    assert completion.input_tokens == 571
    assert completion.output_tokens == 4


def test_utf8_output_decoded_not_cp1252(cli_client: ClaudeCodeLLMClient,
                                        monkeypatch) -> None:
    """Output containing chars invalid in cp1252 round-trips intact."""
    import outreach_agent.llm_gateway as lg

    payload = json.dumps({
        "is_error": False, "result": "café — naïve ✓ 中文",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    })
    monkeypatch.setattr(lg.subprocess, "run",
                        lambda argv, **kw: _completed(stdout=payload))
    completion = cli_client.complete(model="m", system="s", prompt="p",
                                     max_tokens=10)
    assert completion.text == "café — naïve ✓ 中文"


# -- failure mapping --------------------------------------------------------------


def test_timeout_raises_retriable_unavailable(cli_client: ClaudeCodeLLMClient,
                                              monkeypatch) -> None:
    import outreach_agent.llm_gateway as lg

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(lg.subprocess, "run", fake_run)
    with pytest.raises(LlmUnavailableError) as exc_info:
        cli_client.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert exc_info.value.problem.retriable is True


def test_nonzero_exit_raises_nonretriable_cli_error(
        cli_client: ClaudeCodeLLMClient, monkeypatch) -> None:
    """Live probe ground truth: bogus model → exit 1 + one-line diagnostic."""
    import outreach_agent.llm_gateway as lg

    monkeypatch.setattr(
        lg.subprocess, "run",
        lambda argv, **kw: _completed(
            stdout="There's an issue with the selected model "
                   "(totally-bogus-model-xyz).",
            returncode=1),
    )
    with pytest.raises(LlmCliError) as exc_info:
        cli_client.complete(model="m", system="s", prompt="p", max_tokens=10)
    assert exc_info.value.problem.retriable is False
    assert "exited 1" in str(exc_info.value)


def test_is_error_result_raises_cli_error(cli_client: ClaudeCodeLLMClient,
                                          monkeypatch) -> None:
    import outreach_agent.llm_gateway as lg

    payload = json.dumps({"is_error": True, "subtype": "error_during_execution"})
    monkeypatch.setattr(lg.subprocess, "run",
                        lambda argv, **kw: _completed(stdout=payload))
    with pytest.raises(LlmCliError):
        cli_client.complete(model="m", system="s", prompt="p", max_tokens=10)


def test_non_json_output_raises_cli_error(cli_client: ClaudeCodeLLMClient,
                                          monkeypatch) -> None:
    import outreach_agent.llm_gateway as lg

    monkeypatch.setattr(lg.subprocess, "run",
                        lambda argv, **kw: _completed(stdout="not json at all"))
    with pytest.raises(LlmCliError):
        cli_client.complete(model="m", system="s", prompt="p", max_tokens=10)


def test_launch_oserror_raises_backend_error(cli_client: ClaudeCodeLLMClient,
                                             monkeypatch) -> None:
    import outreach_agent.llm_gateway as lg

    def fake_run(argv, **kwargs):
        raise OSError("exe vanished")

    monkeypatch.setattr(lg.subprocess, "run", fake_run)
    with pytest.raises(LlmBackendError):
        cli_client.complete(model="m", system="s", prompt="p", max_tokens=10)


# -- gateway integration: NFR-6 safety + 0-cost spend ------------------------------


def test_secret_check_fires_before_subprocess(db: Database, config: Config,
                                              tmp_path: Path,
                                              monkeypatch) -> None:
    """NFR-6 applies identically to the CLI backend: the deny check runs in
    the gateway BEFORE the subprocess — nothing launched, nothing ledgered."""
    import outreach_agent.llm_gateway as lg

    def must_not_run(argv, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("subprocess.run reached despite secret in prompt")

    monkeypatch.setattr(lg.subprocess, "run", must_not_run)
    client = ClaudeCodeLLMClient(r"C:\fake\claude.exe", timeout_s=5,
                                 scratch_dir=tmp_path / "s")
    gateway = LLMGateway(client, db, config)
    with pytest.raises(SecretLeakError):
        gateway.generate(purpose="x", system="s", prompt="leak ghp_abc123")
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM llm_spend").fetchone()["n"] == 0


def test_zero_cost_ledger_and_no_budget_gate(db: Database, config: Config,
                                             tmp_path: Path,
                                             monkeypatch) -> None:
    """NFR-7: subscription backend records calls + tokens at $0 and is NOT
    gated by the F-13 monthly cap (cap gates the anthropic backend only)."""
    import outreach_agent.llm_gateway as lg

    monkeypatch.setattr(lg.subprocess, "run", lambda argv, **kw: _completed())
    client = ClaudeCodeLLMClient(r"C:\fake\claude.exe", timeout_s=5,
                                 scratch_dir=tmp_path / "s")
    # Cap of $0 would hard-stop any API backend immediately.
    capped = dataclasses.replace(config, llm_monthly_spend_cap_usd=0.0)
    gateway = LLMGateway(client, db, capped)
    text = gateway.generate(purpose="fix-generation", system="s", prompt="p")
    assert text == "a generated patch"
    row = db.conn.execute("SELECT * FROM llm_spend").fetchone()
    assert row["cost_usd"] == 0.0
    assert row["input_tokens"] == 571 and row["output_tokens"] == 4
    assert gateway.month_spend_usd() == 0.0


# -- opt-in live lane (deselected by default; `pytest -m local`) -------------------


@pytest.mark.local
def test_real_claude_cli_says_ok(tmp_path: Path) -> None:
    """Live host lane: one real headless invocation via the actual client.
    Needs the claude CLI installed and an active subscription login."""
    import shutil as _shutil

    exe = _shutil.which("claude")
    if exe is None:
        pytest.skip("claude CLI not on PATH")
    client = ClaudeCodeLLMClient(exe, timeout_s=120,
                                 scratch_dir=tmp_path / "scratch")
    completion = client.complete(
        model="claude-opus-4-8",
        system="You are a test probe. Obey exactly.",
        prompt="Reply with exactly: OK", max_tokens=16,
    )
    assert "OK" in completion.text
