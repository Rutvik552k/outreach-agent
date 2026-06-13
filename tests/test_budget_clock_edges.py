"""Clock-edge cases for BudgetTracker (ADR §5, C7).

The existing test_budget.py covers same-instant cap behavior. This file closes
the clock-sensitive gaps the critique's rate-budget arithmetic depends on:

 - daily upstream-PR guard rolls over at local-midnight-UTC (a PR from
   "yesterday" must not count against today's 1/day budget)
 - per-hour / per-minute caps are sliding windows, not calendar buckets
 - the secondary-limit kill window is exactly `secondary_hit_window_h` hours:
   a hit just OUTSIDE the window does not arm the kill condition; just INSIDE
   does.

BudgetTracker reads `datetime.now(timezone.utc)` internally and has no clock
seam, so these tests drive time by writing ledger/hit rows with controlled
timestamps through the same hash-chained append path the tracker uses, then
exercise the REAL authorize()/record_secondary_limit_hit() arithmetic. Rows are
appended via the Database API so the chain stays valid (no out-of-band tamper).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from outreach_agent.budget import BudgetTracker
from outreach_agent.config import Config
from outreach_agent.persistence import Database, new_ulid


def _insert_budget_row_at(db: Database, *, ts: datetime, kind: str,
                          category: str = "content_creation") -> None:
    """Append a hash-chained rate_budget row with a back-dated ts.

    Mirrors Database.append_budget_entry but lets us set ts, so the chain stays
    valid (prev_hash links correctly) while we simulate the passage of time.
    """
    import json
    from outreach_agent.persistence import (
        BUDGET_CHAIN_HEAD_KEY,
        GENESIS_HASH,
        chain_hash,
    )

    with db.transaction():
        prev = db.get_meta(BUDGET_CHAIN_HEAD_KEY, GENESIS_HASH) or GENESIS_HASH
        payload = {
            "entry_id": new_ulid(),
            "ts": ts.isoformat(timespec="microseconds"),
            "category": category,
            "kind": kind,
            "endpoint": "POST /test",
            "contribution_id": None,
        }
        row_hash = chain_hash(prev, payload)
        db.conn.execute(
            "INSERT INTO rate_budget(entry_id, ts, category, kind, endpoint,"
            " contribution_id, prev_hash, row_hash)"
            " VALUES(:entry_id,:ts,:category,:kind,:endpoint,:contribution_id,"
            " :prev_hash,:row_hash)",
            {**payload, "prev_hash": prev, "row_hash": row_hash},
        )
        db.set_meta(BUDGET_CHAIN_HEAD_KEY, row_hash)


def _insert_secondary_hit_at(db: Database, *, ts: datetime) -> None:
    with db.transaction():
        db.conn.execute(
            "INSERT INTO secondary_limit_hits(hit_id, ts, endpoint, retry_after_s)"
            " VALUES(?,?,?,?)",
            (new_ulid(), ts.isoformat(timespec="microseconds"), "POST /x", 1.0),
        )


def test_yesterdays_upstream_pr_does_not_block_today(budget: BudgetTracker,
                                                     db: Database) -> None:
    """Day rollover: a PR created yesterday must not count toward today's 1/day."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    _insert_budget_row_at(db, ts=yesterday, kind="upstream_pr")
    db.verify_chains()  # chain intact after the back-dated insert

    auth = budget.authorize("content_creation", kind="upstream_pr",
                            endpoint="POST /repos/u/r/pulls")
    assert auth.granted, "yesterday's PR wrongly counted against today's budget"


def test_upstream_pr_earlier_today_does_block(budget: BudgetTracker,
                                              db: Database) -> None:
    """Same-day PR (just after midnight UTC) must consume today's budget."""
    now = datetime.now(timezone.utc)
    just_after_midnight = now.replace(hour=0, minute=0, second=1, microsecond=0)
    if just_after_midnight > now:  # running exactly at 00:00:00 — skip the edge
        pytest.skip("test run at 00:00:00 UTC; midnight-edge ambiguous")
    _insert_budget_row_at(db, ts=just_after_midnight, kind="upstream_pr")

    auth = budget.authorize("content_creation", kind="upstream_pr",
                            endpoint="POST /repos/u/r/pulls")
    assert not auth.granted
    assert "daily upstream-PR budget" in auth.reason


def test_per_hour_cap_is_a_sliding_window_not_a_calendar_hour(db: Database) -> None:
    """A content-creation call 61 minutes ago must NOT count toward the per-hour
    cap; one 59 minutes ago must."""
    config = Config(db_path=db.db_path, min_mutation_spacing_s=0.0,
                    content_creation_per_hr=2, content_creation_per_min=100)
    tracker = BudgetTracker(db, config)
    now = datetime.now(timezone.utc)
    _insert_budget_row_at(db, ts=now - timedelta(minutes=61), kind="comment")
    _insert_budget_row_at(db, ts=now - timedelta(minutes=59), kind="comment")
    # Only the 59-min-old row is inside the window → 1 used, cap 2 → granted.
    first = tracker.authorize("content_creation", kind="comment", endpoint="POST /c")
    assert first.granted
    # Now 2 inside the window (the 59-min one + the one just granted) → cap hit.
    second = tracker.authorize("content_creation", kind="comment", endpoint="POST /c")
    assert not second.granted
    assert "per-hour" in second.reason


def test_per_minute_cap_is_a_sliding_window(db: Database) -> None:
    config = Config(db_path=db.db_path, min_mutation_spacing_s=0.0,
                    content_creation_per_min=2, content_creation_per_hr=1000)
    tracker = BudgetTracker(db, config)
    now = datetime.now(timezone.utc)
    _insert_budget_row_at(db, ts=now - timedelta(seconds=61), kind="comment")
    _insert_budget_row_at(db, ts=now - timedelta(seconds=30), kind="comment")
    first = tracker.authorize("content_creation", kind="comment", endpoint="POST /c")
    assert first.granted  # only the 30s-old one is inside the 60s window
    second = tracker.authorize("content_creation", kind="comment", endpoint="POST /c")
    assert not second.granted
    assert "per-minute" in second.reason


def test_secondary_hit_just_outside_24h_window_does_not_arm_kill(db: Database) -> None:
    """A prior hit 24h+1min ago is outside the window; a fresh hit is only the
    1st inside-window hit → no kill condition."""
    config = Config(db_path=db.db_path, min_mutation_spacing_s=0.0,
                    secondary_hit_window_h=24, secondary_hit_kill_count=2)
    tracker = BudgetTracker(db, config)
    now = datetime.now(timezone.utc)
    _insert_secondary_hit_at(db, ts=now - timedelta(hours=24, minutes=1))

    tracker.record_secondary_limit_hit("POST /now", retry_after_s=1.0)
    assert db.global_pause_reason() is None, "stale hit outside window wrongly armed kill"


def test_secondary_hit_just_inside_24h_window_arms_kill(db: Database) -> None:
    config = Config(db_path=db.db_path, min_mutation_spacing_s=0.0,
                    secondary_hit_window_h=24, secondary_hit_kill_count=2)
    tracker = BudgetTracker(db, config)
    now = datetime.now(timezone.utc)
    _insert_secondary_hit_at(db, ts=now - timedelta(hours=23, minutes=59))

    tracker.record_secondary_limit_hit("POST /now", retry_after_s=1.0)
    reason = db.global_pause_reason()
    assert reason is not None and "kill condition" in reason


def test_backoff_expiry_is_clock_driven(db: Database) -> None:
    """Header-driven backoff denies until its expiry instant, then clears."""
    config = Config(db_path=db.db_path, min_mutation_spacing_s=0.0)
    tracker = BudgetTracker(db, config)
    # An already-expired backoff window must not deny.
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    with db.transaction():
        db.set_meta("budget_backoff_until", past.isoformat())
    auth = tracker.authorize("content_creation", kind="comment", endpoint="POST /c")
    assert auth.granted, "expired backoff window still denied"
