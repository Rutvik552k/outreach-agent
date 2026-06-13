"""Approval flow + Publisher — components §2[4][5] (FR-3/FR-4, F-04, F-05).

submit_for_approval: push branch → intra-fork draft PR (awaiting marker in the
TITLE, never a label — C4 v2.2/C-2 coarse rule) → draft-on-fork.
sync_approvals: poll the draft timeline; rejection → rejected; approval →
atomic pre-publish gate (incl. the C-2 audit cross-check) → upstream PR →
close fork draft → upstream-open.
sync_outcomes: poll upstream PRs; merged → graph-verify scheduling; closed →
KPI outcome; 403/404/422 → upstream-unavailable (F-11). §8 merge-rate
auto-pause evaluated after every recorded outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .approval import pre_publish_gate, scan_timeline
from .config import Config
from .contracts import PreparedContribution
from .errors import BudgetDeniedError
from .github_gateway import GitHubGateway
from .graph_verify import GraphVerdict, verify_graph_credit
from .persistence import Database, new_ulid, utc_now_iso
from .prep import GitRunner, assert_safe_branch
from .state_machine import ContributionStore, State

_UNAVAILABLE_MARKERS = ("403", "404", "422")


def _draft_body(prepared: PreparedContribution, *, upstream_full_name: str) -> str:
    risk = "\n".join(f"- {note}" for note in prepared.risk_notes) or "- none flagged"
    return (
        f"**Status: {'awaiting approval'}** — review the diff, then approve with "
        "the `agent:approve-upstream` label or an `/approve` comment; reject "
        "with `agent:reject`, `/reject`, or by closing this draft.\n\n"
        f"Target upstream: `{upstream_full_name}`\n"
        f"Diff: {prepared.diff_stat.files} files, "
        f"+{prepared.diff_stat.insertions}/-{prepared.diff_stat.deletions}\n\n"
        "## Risk notes (V5)\n"
        f"{risk}\n\n"
        "## Proposed upstream PR text\n\n"
        f"### {prepared.pr_text.title}\n\n{prepared.pr_text.body_md}\n"
    )


def submit_for_approval(
    *,
    db: Database,
    store: ContributionStore,
    gateway: GitHubGateway,
    git: GitRunner,
    config: Config,
    contribution_id: str,
    prepared: PreparedContribution,
    fork_full_name: str,
    fork_default_branch: str,
    upstream_full_name: str,
    work_dir: Any,
) -> int:
    """ci-green → draft-on-fork. Returns the fork draft PR number."""
    # M-3: validate-then-pass + `--` end-of-options (see prep.py module head).
    assert_safe_branch(prepared.branch)
    git.run(["push", "origin", "--", prepared.branch], cwd=work_dir)
    pr = gateway.create_draft_pr_on_fork(
        fork_full_name=fork_full_name,
        head_branch=prepared.branch,
        base_branch=fork_default_branch,
        title=f"[{config.label_awaiting}] {prepared.pr_text.title}",
        body=_draft_body(prepared, upstream_full_name=upstream_full_name),
        contribution_id=contribution_id,
    )
    store.transition(
        contribution_id, State.DRAFT_ON_FORK,
        reason=f"fork draft PR #{pr.number} opened",
        fields={
            "fork_full_name": fork_full_name,
            "fork_draft_pr_number": pr.number,
            "fork_draft_pr_node_id": pr.node_id,
        },
    )
    return pr.number


@dataclass(frozen=True)
class ApprovalOutcome:
    status: str  # "pending" | "rejected" | "published" | "gate-failed" | "budget-blocked"
    detail: str
    upstream_pr_number: int | None = None


def sync_approval(
    *,
    db: Database,
    store: ContributionStore,
    gateway: GitHubGateway,
    config: Config,
    contribution_id: str,
    fork_owner: str,
    fork_full_name: str,
    draft_pr_number: int,
    upstream_full_name: str,
    upstream_base_branch: str,
    prepared_title: str,
    prepared_body: str,
    head_branch: str,
    policy_recheck: Callable[[], bool],
) -> ApprovalOutcome:
    """draft-on-fork → approved → upstream-open, or rejected/pending."""
    fork_owner_login, fork_repo = fork_full_name.split("/", 1)
    events = gateway.get_timeline_events(fork_owner_login, fork_repo, draft_pr_number)
    scan = scan_timeline(events, fork_owner=fork_owner, config=config)

    draft = gateway.get_pr(fork_owner_login, fork_repo, draft_pr_number)
    if draft.state != "open" and scan.approval is None:
        store.transition(contribution_id, State.REJECTED,
                         reason="user closed the fork draft PR (F-12)")
        _record_kpi(db, contribution_id, outcome="rejected",
                    counts_in_merge_rate=False)
        return ApprovalOutcome("rejected", "draft closed by user")
    if scan.rejection is not None:
        store.transition(contribution_id, State.REJECTED,
                         reason=f"rejection by {scan.rejection.actor}: "
                                f"{scan.rejection.detail}")
        gateway.close_fork_draft_pr(
            fork_full_name=fork_full_name, pull_number=draft_pr_number,
            contribution_id=contribution_id,
        )
        _record_kpi(db, contribution_id, outcome="rejected",
                    counts_in_merge_rate=False)
        return ApprovalOutcome("rejected", scan.rejection.detail)
    if scan.approval is None:
        return ApprovalOutcome("pending", "no approval signal yet")

    store.transition(contribution_id, State.APPROVED,
                     reason=f"approval signal by {scan.approval.actor} "
                            f"({scan.approval.kind})")

    gate = pre_publish_gate(
        db=db, config=config, contribution_id=contribution_id,
        fork_owner=fork_owner, fork_full_name=fork_full_name,
        draft_pr_number=draft_pr_number,
        fetch_timeline=lambda: gateway.get_timeline_events(
            fork_owner_login, fork_repo, draft_pr_number),
        fetch_draft_pr=lambda: gateway.get_pr(
            fork_owner_login, fork_repo, draft_pr_number),
        policy_recheck=policy_recheck,
    )
    if not gate.passed:
        target = State.POLICY_BLOCKED if "policy" in gate.reason else State.REJECTED
        store.transition(contribution_id, target, reason=gate.reason)
        return ApprovalOutcome("gate-failed", gate.reason)

    try:
        upstream_pr = gateway.create_upstream_pr(
            upstream_full_name=upstream_full_name,
            fork_owner_login=fork_owner_login,
            head_branch=head_branch,
            base_branch=upstream_base_branch,
            title=prepared_title,
            body=prepared_body,
            contribution_id=contribution_id,
        )
    except BudgetDeniedError as exc:
        store.transition(contribution_id, State.BUDGET_BLOCKED, reason=str(exc))
        return ApprovalOutcome("budget-blocked", str(exc))

    store.transition(
        contribution_id, State.UPSTREAM_OPEN,
        reason=f"upstream PR #{upstream_pr.number} opened (two-PR model, F-04)",
        fields={
            "upstream_pr_number": upstream_pr.number,
            "upstream_pr_node_id": upstream_pr.node_id,
        },
    )
    gateway.close_fork_draft_pr(
        fork_full_name=fork_full_name, pull_number=draft_pr_number,
        contribution_id=contribution_id,
    )
    return ApprovalOutcome("published", f"upstream PR #{upstream_pr.number}",
                           upstream_pr.number)


def _record_kpi(db: Database, contribution_id: str, *, outcome: str,
                counts_in_merge_rate: bool, graph_credit: str | None = None) -> None:
    with db.transaction():
        db.conn.execute(
            "INSERT INTO kpi_outcomes(outcome_id, contribution_id, outcome,"
            " counts_in_merge_rate, graph_credit, recorded_at) VALUES(?,?,?,?,?,?)",
            (new_ulid(), contribution_id, outcome,
             1 if counts_in_merge_rate else 0, graph_credit, utc_now_iso()),
        )


def check_merge_rate_pause(db: Database, config: Config) -> bool:
    """§8: merge rate < threshold over the trailing window of decided upstream
    outcomes (min outcomes required) → global pause. Returns True if paused."""
    rows = db.conn.execute(
        "SELECT outcome FROM kpi_outcomes WHERE counts_in_merge_rate=1"
        " ORDER BY recorded_at DESC LIMIT ?",
        (config.merge_rate_window,),
    ).fetchall()
    if len(rows) < config.merge_rate_min_outcomes:
        return False
    merged = sum(1 for r in rows if r["outcome"] == "merged")
    rate = merged / len(rows)
    if rate < config.merge_rate_pause_threshold:
        with db.transaction():
            db.set_global_pause(
                f"merge rate {rate:.0%} < {config.merge_rate_pause_threshold:.0%} "
                f"over trailing {len(rows)} decided PRs (§8) — manual resume required"
            )
            db.append_audit(
                actor="agent", phase="info", endpoint="kpi:auto-pause(§8)",
                outcome={"rate": rate, "window": len(rows)},
            )
        return True
    return False


def sync_outcome(
    *,
    db: Database,
    store: ContributionStore,
    gateway: GitHubGateway,
    config: Config,
    contribution_id: str,
    upstream_full_name: str,
    upstream_pr_number: int,
) -> State:
    """upstream-open/review-loop → merged (→ graph-verify) | closed |
    upstream-unavailable."""
    owner, repo = upstream_full_name.split("/", 1)
    try:
        pr = gateway.get_pr(owner, repo, upstream_pr_number)
    except Exception as exc:  # reads are unwrapped client errors
        if any(m in str(exc) for m in _UNAVAILABLE_MARKERS):
            store.transition(contribution_id, State.UPSTREAM_UNAVAILABLE,
                             reason=f"upstream PR fetch failed: {exc} (F-11)")
            _record_kpi(db, contribution_id, outcome="upstream-unavailable",
                        counts_in_merge_rate=False)
            return State.UPSTREAM_UNAVAILABLE
        raise
    if pr.merged:
        store.transition(
            contribution_id, State.MERGED,
            reason="upstream PR merged",
            fields={"merge_commit_sha": pr.merge_commit_sha,
                    "merged_at": utc_now_iso()},
        )
        store.transition(contribution_id, State.GRAPH_VERIFY,
                         reason=f"scheduled ≥{config.graph_verify_delay_h}h "
                                "post-merge (F-01/F-02)")
        _record_kpi(db, contribution_id, outcome="merged", counts_in_merge_rate=True)
        check_merge_rate_pause(db, config)
        return State.GRAPH_VERIFY
    if pr.state == "closed":
        store.transition(contribution_id, State.CLOSED,
                         reason="upstream PR closed without merge")
        _record_kpi(db, contribution_id, outcome="closed", counts_in_merge_rate=True)
        check_merge_rate_pause(db, config)
        return State.CLOSED
    return State.UPSTREAM_OPEN


def run_graph_verification(
    *,
    db: Database,
    store: ContributionStore,
    gateway: GitHubGateway,
    config: Config,
    contribution_id: str,
    upstream_full_name: str,
    upstream_pr_number: int,
    default_branch: str,
    user_emails: set[str],
    now: datetime | None = None,
) -> State:
    """graph-verify → graph-credited | graph-missing; MANUAL verdict stays in
    graph-verify and is surfaced by the weekly report (§10.4 fallback)."""
    now = now or datetime.now(timezone.utc)
    row = db.conn.execute(
        "SELECT merged_at, merge_commit_sha FROM contributions WHERE contribution_id=?",
        (contribution_id,),
    ).fetchone()
    merged_at = datetime.fromisoformat(row["merged_at"])
    if now < merged_at + timedelta(hours=config.graph_verify_delay_h):
        return State.GRAPH_VERIFY  # not due yet
    result = verify_graph_credit(
        gateway,
        upstream_full_name=upstream_full_name,
        pull_number=upstream_pr_number,
        default_branch=default_branch,
        user_emails=user_emails,
        merge_commit_sha=row["merge_commit_sha"],
    )
    if result.verdict is GraphVerdict.CREDITED:
        store.transition(contribution_id, State.GRAPH_CREDITED, reason=result.detail)
        _record_kpi(db, contribution_id, outcome="graph-credited",
                    counts_in_merge_rate=False, graph_credit="credited")
        return State.GRAPH_CREDITED
    if result.verdict is GraphVerdict.MISSING:
        store.transition(contribution_id, State.GRAPH_MISSING, reason=result.detail)
        _record_kpi(db, contribution_id, outcome="graph-missing",
                    counts_in_merge_rate=False, graph_credit="missing")
        return State.GRAPH_MISSING
    with db.transaction():
        db.append_audit(
            actor="agent", phase="info", endpoint="graph-verify:manual-check",
            contribution_id=contribution_id,
            outcome={"detail": result.detail},
        )
    return State.GRAPH_VERIFY
