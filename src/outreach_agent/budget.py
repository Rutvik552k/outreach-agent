"""BudgetTracker — contract C7 (ADR-001 §5).

Authorization is computed transactionally from the hash-chained rate_budget
ledger. Granting appends the ledger row in the same transaction, so the cap
arithmetic and the spend record are atomic. Categories cover the full F-06
content-creation enumeration; the 1/day guard keys on kind == 'upstream_pr'.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config
from .errors import GlobalPauseError
from .persistence import Database, new_ulid, utc_now_iso

CONTENT_CREATION_KINDS = frozenset({
    "fork_create", "fork_draft_pr", "fork_draft_close", "upstream_pr",
    "review_reply", "comment",
})

_BACKOFF_KEY = "budget_backoff_until"
_RATE_STATE_KEY = "budget_rate_headers"


@dataclass(frozen=True)
class BudgetAuthorization:
    granted: bool
    wait_seconds: float
    reason: str
    entry_id: str | None = None


class BudgetTracker:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    # -- C7 entrypoint --------------------------------------------------------

    def authorize(
        self,
        category: str,
        *,
        kind: str,
        endpoint: str,
        contribution_id: str | None = None,
    ) -> BudgetAuthorization:
        if category not in ("content_creation", "other_mutation"):
            raise ValueError(f"unknown budget category {category!r}")
        if category == "content_creation" and kind not in CONTENT_CREATION_KINDS:
            raise ValueError(
                f"kind {kind!r} is not in the F-06 content-creation enumeration"
            )
        with self.db.transaction():
            pause = self.db.global_pause_reason()
            if pause:
                raise GlobalPauseError(f"global pause active: {pause}")

            now = datetime.now(timezone.utc)
            denied = self._check_backoff(now) or self._check_spacing(now)
            if denied is None and category == "content_creation":
                denied = self._check_content_caps(now)
            if denied is None and kind == "upstream_pr":
                denied = self._check_daily_pr_guard(now)
            if denied is not None:
                return denied

            entry_id = self.db.append_budget_entry(
                category=category, kind=kind, endpoint=endpoint,
                contribution_id=contribution_id,
            )
            return BudgetAuthorization(True, 0.0, "granted", entry_id)

    # -- individual checks ----------------------------------------------------

    def _count_since(self, cutoff: datetime, *, category: str | None = None,
                     kind: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM rate_budget WHERE ts >= ?"
        params: list[object] = [cutoff.isoformat(timespec="microseconds")]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return int(self.db.conn.execute(sql, params).fetchone()["n"])

    def _check_backoff(self, now: datetime) -> BudgetAuthorization | None:
        raw = self.db.get_meta(_BACKOFF_KEY)
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        if now >= until:
            return None
        wait = (until - now).total_seconds()
        return BudgetAuthorization(False, wait, "header-driven backoff active")

    def _check_spacing(self, now: datetime) -> BudgetAuthorization | None:
        row = self.db.conn.execute(
            "SELECT ts FROM rate_budget ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        last = datetime.fromisoformat(row["ts"])
        elapsed = (now - last).total_seconds()
        if elapsed < self.config.min_mutation_spacing_s:
            wait = self.config.min_mutation_spacing_s - elapsed
            return BudgetAuthorization(False, wait, "minimum mutation spacing (2s)")
        return None

    def _check_content_caps(self, now: datetime) -> BudgetAuthorization | None:
        per_min = self._count_since(now - timedelta(minutes=1), category="content_creation")
        if per_min >= self.config.content_creation_per_min:
            return BudgetAuthorization(False, 60.0, "content-creation per-minute cap")
        per_hr = self._count_since(now - timedelta(hours=1), category="content_creation")
        if per_hr >= self.config.content_creation_per_hr:
            return BudgetAuthorization(False, 3600.0, "content-creation per-hour cap")
        return None

    def _check_daily_pr_guard(self, now: datetime) -> BudgetAuthorization | None:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = self._count_since(midnight, kind="upstream_pr")
        if today >= self.config.upstream_pr_per_day:
            wait = (midnight + timedelta(days=1) - now).total_seconds()
            return BudgetAuthorization(False, wait, "daily upstream-PR budget exhausted (1/day)")
        return None

    # -- header-driven backoff state (§5, FM1) --------------------------------

    def record_rate_headers(self, remaining: int | None, reset_epoch: int | None) -> None:
        with self.db.transaction():
            self.db.set_meta(_RATE_STATE_KEY, json.dumps(
                {"remaining": remaining, "reset": reset_epoch, "at": utc_now_iso()}
            ))

    def rate_state(self) -> dict[str, object]:
        raw = self.db.get_meta(_RATE_STATE_KEY)
        return json.loads(raw) if raw else {}

    def record_secondary_limit_hit(self, endpoint: str, retry_after_s: float | None) -> None:
        """403/429 with secondary-limit signature. Two hits in 24h → global pause."""
        now = datetime.now(timezone.utc)
        backoff = retry_after_s if retry_after_s is not None else self.config.backoff_initial_s
        with self.db.transaction():
            self.db.conn.execute(
                "INSERT INTO secondary_limit_hits(hit_id, ts, endpoint, retry_after_s)"
                " VALUES(?,?,?,?)",
                (new_ulid(), now.isoformat(timespec="microseconds"), endpoint, retry_after_s),
            )
            self.db.set_meta(
                _BACKOFF_KEY, (now + timedelta(seconds=backoff)).isoformat()
            )
            window = now - timedelta(hours=self.config.secondary_hit_window_h)
            hits = int(self.db.conn.execute(
                "SELECT COUNT(*) AS n FROM secondary_limit_hits WHERE ts >= ?",
                (window.isoformat(timespec="microseconds"),),
            ).fetchone()["n"])
            if hits >= self.config.secondary_hit_kill_count:
                self.db.set_global_pause(
                    f"{hits} secondary-limit hits within "
                    f"{self.config.secondary_hit_window_h}h — kill condition (§5)"
                )
                self.db.append_audit(
                    actor="agent", phase="info", endpoint="budget:kill-condition",
                    outcome={"hits": hits, "endpoint": endpoint},
                )
