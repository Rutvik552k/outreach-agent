from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from outreach_agent.budget import BudgetTracker
from outreach_agent.config import Config
from outreach_agent.errors import GlobalPauseError
from outreach_agent.persistence import Database


def test_budget_one_upstream_pr_per_day(budget: BudgetTracker) -> None:
    """§5/AC-6: the daily guard keys on upstream-PR creation only."""
    first = budget.authorize("content_creation", kind="upstream_pr",
                             endpoint="POST /repos/u/r/pulls")
    assert first.granted

    second = budget.authorize("content_creation", kind="upstream_pr",
                              endpoint="POST /repos/u/r/pulls")
    assert not second.granted
    assert "daily upstream-PR budget" in second.reason
    assert second.wait_seconds > 0

    # Other content-creation kinds are NOT blocked by the daily PR guard.
    other = budget.authorize("content_creation", kind="comment",
                             endpoint="POST /repos/u/r/issues/1/comments")
    assert other.granted


def test_full_f06_enumeration_accepted(budget: BudgetTracker) -> None:
    for kind in ("fork_create", "fork_draft_pr", "fork_draft_close",
                 "review_reply", "comment"):
        auth = budget.authorize("content_creation", kind=kind, endpoint=f"POST /{kind}")
        assert auth.granted, kind


def test_unknown_kind_rejected_for_content_creation(budget: BudgetTracker) -> None:
    with pytest.raises(ValueError):
        budget.authorize("content_creation", kind="push_branch", endpoint="git")


def test_per_minute_cap(budget: BudgetTracker) -> None:
    for _ in range(8):
        assert budget.authorize("content_creation", kind="comment",
                                endpoint="POST /c").granted
    ninth = budget.authorize("content_creation", kind="comment", endpoint="POST /c")
    assert not ninth.granted
    assert "per-minute" in ninth.reason


def test_min_spacing_enforced(tmp_path: Path) -> None:
    config = Config(db_path=tmp_path / "s.db", min_mutation_spacing_s=2.0)
    db = Database(config.db_path)
    tracker = BudgetTracker(db, config)
    assert tracker.authorize("other_mutation", kind="branch_delete",
                             endpoint="DELETE /x").granted
    second = tracker.authorize("other_mutation", kind="branch_delete",
                               endpoint="DELETE /x")
    assert not second.granted
    assert "spacing" in second.reason
    db.close()


def test_two_secondary_hits_trigger_kill_condition(budget: BudgetTracker,
                                                   db: Database) -> None:
    budget.record_secondary_limit_hit("POST /a", retry_after_s=30)
    assert db.global_pause_reason() is None
    budget.record_secondary_limit_hit("POST /b", retry_after_s=30)
    assert db.global_pause_reason() is not None
    assert "kill condition" in db.global_pause_reason()
    with pytest.raises(GlobalPauseError):
        budget.authorize("content_creation", kind="comment", endpoint="POST /c")


def test_header_backoff_denies_until_expiry(budget: BudgetTracker, db: Database) -> None:
    budget.record_secondary_limit_hit("POST /a", retry_after_s=120)
    denied = budget.authorize("content_creation", kind="comment", endpoint="POST /c")
    assert not denied.granted
    assert "backoff" in denied.reason
    assert 0 < denied.wait_seconds <= 120


def test_ledger_rows_are_hash_chained(budget: BudgetTracker, db: Database) -> None:
    budget.authorize("content_creation", kind="comment", endpoint="POST /c")
    budget.authorize("other_mutation", kind="label_apply", endpoint="POST /l")
    db.verify_chains()
    rows = db.conn.execute("SELECT prev_hash, row_hash FROM rate_budget ORDER BY seq").fetchall()
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
