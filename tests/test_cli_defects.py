"""Regression tests for QA chain-step-5b defects DEF-001..DEF-006
(docs/qa/acceptance-report.md §4). Mocked CI lane — keyring is monkeypatched;
no Credential Manager, network, or Docker is touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import outreach_agent
from outreach_agent.cli import main
from outreach_agent.errors import CredentialError, OutreachError
from outreach_agent.persistence import CHAIN_BREAK_PAUSE_PREFIX
from outreach_agent.tokens import KeyringTokenSource

# DEF-005 repro environment: `PYTHONPATH=src python -m outreach_agent ...`
# (the venv may not have the package pip-installed; tests import it via
# pytest's pythonpath=["src"], so the subprocess needs the same path).
_SRC_DIR = str(Path(outreach_agent.__file__).resolve().parent.parent)


def _subprocess_env(*, drop_pythonioencoding: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    if drop_pythonioencoding:
        env.pop("PYTHONIOENCODING", None)
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return env


@pytest.fixture
def empty_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a machine with no credentials stored (first-run state)."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda service, name: None)


# --- DEF-001: missing credential -> typed error, sanitized one-line CLI exit --


def test_missing_credential_raises_typed_outreach_error(empty_keyring) -> None:
    with pytest.raises(CredentialError) as exc_info:
        KeyringTokenSource().github_token()
    # Must be an OutreachError so main()'s handler catches it (DEF-001 root
    # cause was a bare LookupError escaping `except OutreachError`).
    assert isinstance(exc_info.value, OutreachError)
    assert not isinstance(exc_info.value, LookupError)
    assert "github_oauth_token" in str(exc_info.value)


@pytest.mark.parametrize("argv_tail", [
    ["auth", "login"],   # first credential touched: github_oauth_client_id
    ["discover"],        # first credential touched: github_oauth_token
])
def test_cli_missing_credential_is_sanitized_nonzero_exit(
    empty_keyring, tmp_path: Path, capsys, argv_tail: list[str]
) -> None:
    rc = main(["--db-path", str(tmp_path / "state.db"), *argv_tail])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert "credential" in captured.err
    # one sanitized line ("title: detail"), not a multi-frame dump
    assert len([ln for ln in captured.err.splitlines() if ln.strip()]) == 1


# --- DEF-002: per-credential remediation, never circular ---------------------


def test_client_id_guidance_points_at_oauth_app_setup_not_auth_login(
    empty_keyring,
) -> None:
    msg = str(pytest.raises(CredentialError, KeyringTokenSource().oauth_client_id).value)
    assert "OAuth App" in msg
    assert "keyring set outreach-agent github_oauth_client_id" in msg
    # Never advise re-running the command that just failed (auth login).
    assert "auth login" not in msg


def test_client_secret_guidance_points_at_oauth_app_setup_not_auth_login(
    empty_keyring,
) -> None:
    msg = str(
        pytest.raises(CredentialError, KeyringTokenSource().oauth_client_secret).value
    )
    assert "keyring set outreach-agent github_oauth_client_secret" in msg
    assert "auth login" not in msg


def test_token_guidance_points_at_auth_login(empty_keyring) -> None:
    msg = str(pytest.raises(CredentialError, KeyringTokenSource().github_token).value)
    assert "outreach-agent auth login" in msg


def test_anthropic_key_guidance_has_own_storage_instruction(empty_keyring) -> None:
    msg = str(
        pytest.raises(CredentialError, KeyringTokenSource().anthropic_api_key).value
    )
    assert "keyring set outreach-agent anthropic_api_key" in msg
    assert "auth login" not in msg


# --- DEF-003: UTF-8 forced on stdout/stderr ----------------------------------


def test_report_bytes_are_utf8_even_on_cp1252_console(tmp_path: Path) -> None:
    """Run the real entry point in a subprocess with a legacy-codepage default
    and assert the § / — literals arrive as UTF-8 bytes (no mojibake)."""
    result = subprocess.run(
        [sys.executable, "-m", "outreach_agent",
         "--db-path", str(tmp_path / "state.db"), "report"],
        capture_output=True, timeout=120,
        env=_subprocess_env(drop_pythonioencoding=True),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    out = result.stdout.decode("utf-8")  # raises if not valid UTF-8
    assert "§2[6]" in out
    assert "—" in out
    assert "�" not in out  # no replacement chars in the payload itself


# --- DEF-005: python -m outreach_agent ---------------------------------------


def test_python_dash_m_outreach_agent_runs_cli(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "outreach_agent",
         "--db-path", str(tmp_path / "state.db"), "status"],
        capture_output=True, timeout=120, env=_subprocess_env(),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "contribution states" in result.stdout.decode("utf-8")


def test_dunder_main_delegates_to_cli_main() -> None:
    import outreach_agent.__main__ as dunder
    from outreach_agent import cli

    assert dunder.main is cli.main


# --- DEF-006: read-only commands under global pause --------------------------


def _paused_db(tmp_path: Path, reason: str) -> Path:
    from outreach_agent.persistence import Database

    db_path = tmp_path / "state.db"
    db = Database(db_path)
    db.set_global_pause(reason)
    db.close()
    return db_path


def test_status_allowed_under_pause_with_banner(tmp_path: Path, capsys) -> None:
    db_path = _paused_db(tmp_path, "merge rate 0.10 below threshold")
    rc = main(["--db-path", str(db_path), "status"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "GLOBAL PAUSE ACTIVE: merge rate 0.10 below threshold" in captured.err
    assert "contribution states" in captured.out  # the diagnostic still renders


def test_report_allowed_under_pause_with_banner(tmp_path: Path, capsys) -> None:
    db_path = _paused_db(tmp_path, "merge rate 0.10 below threshold")
    rc = main(["--db-path", str(db_path), "report"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "GLOBAL PAUSE ACTIVE" in captured.err
    assert "LLM spend this month" in captured.out


@pytest.mark.parametrize("command", ["discover", "prepare", "approve-sync",
                                     "profile", "auth"])
def test_mutating_commands_stay_blocked_under_pause(
    tmp_path: Path, capsys, command: str
) -> None:
    db_path = _paused_db(tmp_path, "merge rate 0.10 below threshold")
    argv = ["--db-path", str(db_path), command]
    if command == "auth":
        argv.append("login")
    rc = main(argv)
    captured = capsys.readouterr()
    assert rc == 3
    assert "agent is globally paused" in captured.err


@pytest.mark.parametrize("reason", [
    CHAIN_BREAK_PAUSE_PREFIX + "hash chain break in audit_log at seq=1",
    CHAIN_BREAK_PAUSE_PREFIX + "hash chain head mismatch for audit_log",
    CHAIN_BREAK_PAUSE_PREFIX + "github_object_id mirror mismatch in audit_log seq=3",
])
@pytest.mark.parametrize("command", ["status", "report"])
def test_chain_break_pause_blocks_even_read_only(
    tmp_path: Path, capsys, reason: str, command: str
) -> None:
    """FM12 exception: integrity-failure pause keeps EVERYTHING blocked except
    a minimal chain-status line — operating on untrusted state is worse than
    blindness."""
    db_path = _paused_db(tmp_path, reason)
    rc = main(["--db-path", str(db_path), command])
    captured = capsys.readouterr()
    assert rc == 3
    assert "chain-status" in captured.err
    assert reason in captured.err
    assert captured.out == ""  # nothing rendered from untrusted state
