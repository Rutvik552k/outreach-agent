from __future__ import annotations

from pathlib import Path

from outreach_agent.cli import main


def test_cli_status_runs_startup_checks(tmp_path: Path, capsys) -> None:
    rc = main(["--db-path", str(tmp_path / "state.db"), "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "contribution states" in out
    assert "upstream PRs today: 0/1" in out


def test_cli_refuses_mutating_command_when_globally_paused(tmp_path: Path, capsys) -> None:
    """DEF-006: mutating commands stay blocked under pause (read-only
    `status`/`report` are now allowed — covered in test_cli_defects.py)."""
    from outreach_agent.persistence import Database

    db_path = tmp_path / "state.db"
    db = Database(db_path)
    db.set_global_pause("test pause")
    db.close()
    rc = main(["--db-path", str(db_path), "discover"])
    assert rc == 3
    assert "paused" in capsys.readouterr().err


def test_cli_resume_clears_pause_and_audits(tmp_path: Path, capsys) -> None:
    from outreach_agent.persistence import Database

    db_path = tmp_path / "state.db"
    db = Database(db_path)
    db.set_global_pause("merge rate low")
    db.close()
    rc = main(["--db-path", str(db_path), "resume"])
    assert rc == 0
    assert "cleared global pause" in capsys.readouterr().out
    db = Database(db_path)
    try:
        assert db.global_pause_reason() is None
        row = db.conn.execute(
            "SELECT actor FROM audit_log WHERE endpoint='cli:resume'"
        ).fetchone()
        assert row["actor"] == "user"
    finally:
        db.close()


def test_cli_report_renders(tmp_path: Path, capsys) -> None:
    rc = main(["--db-path", str(tmp_path / "state.db"), "report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "merge rate: n/a" in out
    assert "LLM spend this month" in out
