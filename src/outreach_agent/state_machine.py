"""Contribution lifecycle state machine (ADR-001 §6 v2 — two-PR model).

Every transition is persisted and audited in the same transaction.
Illegal transitions raise IllegalTransitionError.
"""

from __future__ import annotations

from enum import StrEnum

from .errors import IllegalTransitionError
from .persistence import Database, new_ulid, utc_now_iso


class State(StrEnum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    POLICY_CLEARED = "policy-cleared"
    PREPARED = "prepared"
    CI_GREEN = "ci-green"
    DRAFT_ON_FORK = "draft-on-fork"
    APPROVED = "approved"
    UPSTREAM_OPEN = "upstream-open"
    REVIEW_LOOP = "review-loop"
    MERGED = "merged"
    GRAPH_VERIFY = "graph-verify"
    GRAPH_CREDITED = "graph-credited"          # terminal, KPI++
    GRAPH_MISSING = "graph-missing"            # terminal, partial failure (F-01/F-02)
    CLOSED = "closed"                          # terminal, KPI--
    REJECTED = "rejected"                      # terminal
    UPSTREAM_UNAVAILABLE = "upstream-unavailable"  # terminal, KPI-excluded (F-11)
    WORKFLOW_FILE_TOUCH_UNSUPPORTED = "workflow-file-touch-unsupported"  # terminal skip (V3)
    # Re-enterable failure states
    POLICY_BLOCKED = "policy-blocked"
    CI_FAILED = "ci-failed"
    SANDBOX_UNFIT = "sandbox-unfit"            # F-10
    BUDGET_BLOCKED = "budget-blocked"
    LLM_BLOCKED = "llm-blocked"                # F-13
    ERROR = "error"


TERMINAL_STATES: frozenset[State] = frozenset({
    State.GRAPH_CREDITED,
    State.GRAPH_MISSING,
    State.CLOSED,
    State.REJECTED,
    State.UPSTREAM_UNAVAILABLE,
    State.WORKFLOW_FILE_TOUCH_UNSUPPORTED,
})

_FAILURE = {State.BUDGET_BLOCKED, State.ERROR}

TRANSITIONS: dict[State, frozenset[State]] = {
    State.DISCOVERED: frozenset({State.SCORED, State.ERROR}),
    State.SCORED: frozenset({State.POLICY_CLEARED, State.POLICY_BLOCKED, State.ERROR}),
    State.POLICY_CLEARED: frozenset({
        State.PREPARED, State.LLM_BLOCKED, State.SANDBOX_UNFIT, State.BUDGET_BLOCKED,
        State.WORKFLOW_FILE_TOUCH_UNSUPPORTED, State.POLICY_BLOCKED, State.ERROR,
    }),
    State.PREPARED: frozenset({
        State.CI_GREEN, State.CI_FAILED, State.SANDBOX_UNFIT, State.LLM_BLOCKED,
        State.WORKFLOW_FILE_TOUCH_UNSUPPORTED, State.ERROR,
    }),
    State.CI_GREEN: frozenset({
        State.DRAFT_ON_FORK, State.BUDGET_BLOCKED, State.PREPARED,
        State.WORKFLOW_FILE_TOUCH_UNSUPPORTED, State.ERROR,
    }),
    State.DRAFT_ON_FORK: frozenset({State.APPROVED, State.REJECTED, State.ERROR}),
    State.APPROVED: frozenset({
        # Gate failure at publish (F-05) → rejected or policy-blocked, never upstream.
        State.UPSTREAM_OPEN, State.REJECTED, State.POLICY_BLOCKED,
        State.BUDGET_BLOCKED, State.WORKFLOW_FILE_TOUCH_UNSUPPORTED, State.ERROR,
    }),
    State.UPSTREAM_OPEN: frozenset({
        State.REVIEW_LOOP, State.MERGED, State.CLOSED, State.UPSTREAM_UNAVAILABLE, State.ERROR,
    }),
    State.REVIEW_LOOP: frozenset({
        State.PREPARED,  # changes-requested → prepared'
        State.MERGED, State.CLOSED, State.UPSTREAM_UNAVAILABLE, State.ERROR,
    }),
    State.MERGED: frozenset({State.GRAPH_VERIFY, State.ERROR}),
    State.GRAPH_VERIFY: frozenset({State.GRAPH_CREDITED, State.GRAPH_MISSING, State.ERROR}),
    # Re-enterable failure states (ADR §6)
    State.POLICY_BLOCKED: frozenset({State.POLICY_CLEARED, State.CLOSED, State.ERROR}),
    State.CI_FAILED: frozenset({State.PREPARED, State.POLICY_CLEARED, State.ERROR}),
    State.SANDBOX_UNFIT: frozenset({State.POLICY_CLEARED, State.PREPARED, State.ERROR}),
    State.BUDGET_BLOCKED: frozenset({
        State.CI_GREEN, State.DRAFT_ON_FORK, State.APPROVED, State.POLICY_CLEARED, State.ERROR,
    }),
    State.LLM_BLOCKED: frozenset({State.POLICY_CLEARED, State.ERROR}),
    State.ERROR: frozenset({
        State.POLICY_CLEARED, State.PREPARED, State.CI_GREEN, State.DRAFT_ON_FORK,
        State.APPROVED, State.UPSTREAM_OPEN, State.REVIEW_LOOP, State.CLOSED,
    }),
    # Terminal states: no outgoing transitions
    State.GRAPH_CREDITED: frozenset(),
    State.GRAPH_MISSING: frozenset(),
    State.CLOSED: frozenset(),
    State.REJECTED: frozenset(),
    State.UPSTREAM_UNAVAILABLE: frozenset(),
    State.WORKFLOW_FILE_TOUCH_UNSUPPORTED: frozenset(),
}


def assert_transition(current: State, target: State) -> None:
    if current in TERMINAL_STATES:
        raise IllegalTransitionError(
            f"{current} is terminal; no transition to {target} is permitted"
        )
    if target not in TRANSITIONS.get(current, frozenset()):
        raise IllegalTransitionError(f"transition {current} -> {target} is not defined in ADR-001 §6")


class ContributionStore:
    """Persisted state machine over the contributions table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, *, candidate_id: str | None, repo_full_name: str) -> str:
        contribution_id = new_ulid()
        now = utc_now_iso()
        with self.db.transaction():
            self.db.conn.execute(
                "INSERT INTO contributions(contribution_id, candidate_id, repo_full_name,"
                " state, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (contribution_id, candidate_id, repo_full_name,
                 State.DISCOVERED.value, now, now),
            )
            self.db.append_audit(
                actor="agent", phase="info", endpoint="state:create",
                contribution_id=contribution_id,
                outcome={"state": State.DISCOVERED.value, "repo": repo_full_name},
            )
        return contribution_id

    def get_state(self, contribution_id: str) -> State:
        row = self.db.conn.execute(
            "SELECT state FROM contributions WHERE contribution_id=?", (contribution_id,)
        ).fetchone()
        if row is None:
            raise IllegalTransitionError(f"unknown contribution {contribution_id}")
        return State(row["state"])

    def transition(
        self,
        contribution_id: str,
        target: State,
        *,
        reason: str | None = None,
        fields: dict[str, object] | None = None,
    ) -> None:
        """Atomically: re-read state, validate, update row + audit in one transaction."""
        allowed_cols = {
            "fork_full_name", "branch", "base_sha", "fork_draft_pr_number",
            "fork_draft_pr_node_id", "upstream_pr_number", "upstream_pr_node_id",
            "merge_commit_sha", "merged_at", "prepared_json", "last_synced_at",
        }
        extra = fields or {}
        bad = set(extra) - allowed_cols
        if bad:
            raise ValueError(f"non-whitelisted contribution fields: {bad}")
        with self.db.transaction():
            current = self.get_state(contribution_id)
            assert_transition(current, target)
            sets = ", ".join(f"{k}=?" for k in extra)
            params: list[object] = list(extra.values())
            sql = (
                f"UPDATE contributions SET state=?, state_reason=?, updated_at=?"
                f"{', ' + sets if sets else ''} WHERE contribution_id=? AND state=?"
            )
            cur = self.db.conn.execute(
                sql,
                [target.value, reason, utc_now_iso(), *params, contribution_id, current.value],
            )
            if cur.rowcount != 1:
                raise IllegalTransitionError(
                    f"concurrent state change detected for {contribution_id}"
                )
            self.db.append_audit(
                actor="agent", phase="info", endpoint="state:transition",
                contribution_id=contribution_id,
                outcome={"from": current.value, "to": target.value, "reason": reason},
            )
