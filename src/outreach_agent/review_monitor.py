"""Review Monitor — component §2[6] (FR-4, AC-5).

Polls upstream PR review comments each run, persists them as review_threads,
drafts substantive responses via the LLMGateway, and NEVER auto-posts: posting
a reply is a budgeted mutation that requires an explicit approval signal on
the UPSTREAM PR timeline (the reply target is distinct from draft-PR
approval), verified with the same C4 machinery — owner-bound actor check plus
the C-2 exact github-object-id cross-check, fail-closed on any ambiguity.

Approval UX: the user comments `/approve-reply <comment_id>` (or
`/reject-reply <comment_id>`) on the upstream PR. Structural incapability
(C-1) already prevents the agent from emitting these commands: the gateway's
comment surface refuses any body starting with `/approve` or `/reject`
(github_gateway._assert_comment_capability).

changes-requested → prepared' re-entry (ADR §6): a review with state
CHANGES_REQUESTED (PullRequestReview.state, githubkit models/group_0414.py)
sends the contribution back into prep via review-loop.

Ground sources (githubkit 0.15.5 installed source):
- list reviews: rest/pulls.py:2729 (GET .../pulls/{pull_number}/reviews).
- review comment fields id/in_reply_to_id/user/body/path/diff_hunk:
  models/group_0394.py.
- reply mutation: ADR §10.1 via gateway.reply_to_review_comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .errors import BudgetDeniedError, LlmBudgetError, LlmUnavailableError
from .github_gateway import GitHubGateway
from .llm_gateway import LLMGateway
from .persistence import Database, new_ulid, utc_now_iso
from .state_machine import ContributionStore, State

_REVIEW_SYSTEM = (
    "You draft replies to code-review comments on an open-source pull request "
    "authored with AI assistance (disclosed). Be substantive, specific, and "
    "courteous; address the technical point directly; never argue tone. If the "
    "reviewer is right, say so and state the concrete fix. Plain markdown, no "
    "preamble."
)


# -- polling + drafting -------------------------------------------------------


def poll_review_threads(
    *,
    db: Database,
    gateway: GitHubGateway,
    llm: LLMGateway,
    config: Config,
    contribution_id: str,
    upstream_full_name: str,
    upstream_pr_number: int,
    fork_owner: str,
) -> list[str]:
    """Persist new top-level maintainer review comments and draft responses.

    Only top-level comments (no in_reply_to_id) are reply targets per ADR
    §10.1. The user's own comments are not threads needing a response.
    Returns the thread ids newly drafted this run.
    """
    owner, repo = upstream_full_name.split("/", 1)
    comments = gateway.list_review_comments(owner, repo, upstream_pr_number)
    known = {
        row["upstream_comment_id"]
        for row in db.conn.execute(
            "SELECT upstream_comment_id FROM review_threads WHERE contribution_id=?",
            (contribution_id,),
        )
    }
    new_ids: list[str] = []
    for comment in comments:
        if comment.get("in_reply_to_id"):
            continue
        author = (comment.get("user") or {}).get("login", "")
        if not author or author == fork_owner:
            continue
        comment_id = int(comment["id"])
        if comment_id in known:
            continue
        thread_id = new_ulid()
        now = utc_now_iso()
        with db.transaction():
            db.conn.execute(
                "INSERT INTO review_threads(thread_id, contribution_id,"
                " upstream_comment_id, author_login, body, draft_response,"
                " response_state, created_at, updated_at)"
                " VALUES(?,?,?,?,?,NULL,'pending',?,?)",
                (thread_id, contribution_id, comment_id, author,
                 comment.get("body") or "", now, now),
            )
            db.append_audit(
                actor="agent", phase="info", endpoint="review-monitor:thread-new",
                contribution_id=contribution_id,
                outcome={"upstream_comment_id": comment_id, "author": author},
            )
        new_ids.append(thread_id)

    # Draft for every pending thread without a draft (covers threads whose
    # earlier draft attempt hit an LLM outage/spend cap — F-13 re-enterable).
    drafted: list[str] = []
    for row in db.conn.execute(
        "SELECT thread_id, author_login, body FROM review_threads"
        " WHERE contribution_id=? AND response_state='pending'"
        " AND draft_response IS NULL",
        (contribution_id,),
    ).fetchall():
        prompt = (
            f"PR: {upstream_full_name}#{upstream_pr_number}\n"
            f"Review comment by {row['author_login']}:\n\n{row['body']}\n\n"
            "Draft a reply."
        )
        try:
            draft = llm.generate(
                purpose="review-response", system=_REVIEW_SYSTEM, prompt=prompt,
            )
        except (LlmBudgetError, LlmUnavailableError) as exc:
            with db.transaction():
                db.append_audit(
                    actor="agent", phase="info",
                    endpoint="review-monitor:draft-blocked(F-13)",
                    contribution_id=contribution_id,
                    outcome={"thread_id": row["thread_id"], "error": str(exc)},
                )
            continue
        with db.transaction():
            db.conn.execute(
                "UPDATE review_threads SET draft_response=?, updated_at=?"
                " WHERE thread_id=?",
                (draft, utc_now_iso(), row["thread_id"]),
            )
        drafted.append(row["thread_id"])
    return drafted


# -- reply approval signals (C4 machinery on the upstream PR) -----------------


@dataclass(frozen=True)
class ReplySignal:
    command: str  # "approve" | "reject"
    comment_id: int  # the review comment the draft replies to
    actor: str
    event_id: str


def scan_reply_signals(
    events: list[dict[str, Any]],
    *,
    fork_owner: str,
    config: Config,
) -> tuple[list[ReplySignal], list[str]]:
    """Scan upstream-PR timeline `commented` events for reply commands.

    Identical actor binding to C4: actor.login == fork_owner, exact-token
    command match. Invalid actors are violations, never signals (V2).
    """
    signals: list[ReplySignal] = []
    violations: list[str] = []
    for event in events:
        if event.get("event") != "commented":
            continue
        body = (event.get("body") or "").strip()
        tokens = body.split()
        if len(tokens) < 2:
            continue
        if tokens[0] == config.comment_approve_reply:
            command = "approve"
        elif tokens[0] == config.comment_reject_reply:
            command = "reject"
        else:
            continue
        try:
            comment_id = int(tokens[1])
        except ValueError:
            violations.append(f"unparseable reply-command target {tokens[1]!r}")
            continue
        actor = (event.get("actor") or event.get("user") or {})
        actor_login = actor.get("login", "") if isinstance(actor, dict) else str(actor)
        if not actor_login or actor_login != fork_owner:
            violations.append(
                f"{tokens[0]} by invalid actor {actor_login!r} "
                f"(fork_owner={fork_owner!r}) — rejected (V2)"
            )
            continue
        signals.append(ReplySignal(
            command, comment_id, actor_login,
            str(event.get("id") or event.get("node_id") or ""),
        ))
    return signals, violations


def reply_signal_agent_originated(
    db: Database,
    *,
    contribution_id: str,
    signal: ReplySignal,
) -> tuple[bool, str]:
    """C-2 exact-id-set cross-check for reply approvals.

    Unlike the fork-draft check, the agent DOES legitimately comment on the
    upstream PR (posted review replies), so mere presence of agent comment
    mutations cannot void the PR. The check is exact membership only: the
    signal's timeline event id against the set of agent-confirmed
    comment/reply mutation github_object_ids on this contribution (comment-id
    correlation VERIFIED per approval.py / ADR v2.3). A confirmed
    comment-class mutation missing its object id makes the set incomplete →
    fail-closed (returns agent-originated).
    """
    ids: set[str] = set()
    for row in db.conn.execute(
        "SELECT endpoint, github_object_id FROM audit_log"
        " WHERE actor='agent' AND phase='confirmed' AND contribution_id=?"
        " AND (endpoint LIKE '%/comments%' OR endpoint LIKE '%/replies%')",
        (contribution_id,),
    ).fetchall():
        if row["github_object_id"] is None:
            return True, (
                f"agent-confirmed comment mutation {row['endpoint']!r} lacks "
                "github_object_id — id set incomplete, fail-closed (C-2)"
            )
        ids.add(str(row["github_object_id"]))
    if not signal.event_id:
        return True, "signal has no event id — ambiguous, fail-closed (C-2)"
    if signal.event_id in ids:
        return True, (
            f"signal event id {signal.event_id} is in the agent-confirmed "
            "mutation id set — agent-originated, rejected (C-2 exact membership)"
        )
    return False, "signal not agent-originated (C-2 exact membership)"


def apply_reply_signals(
    *,
    db: Database,
    gateway: GitHubGateway,
    config: Config,
    contribution_id: str,
    upstream_full_name: str,
    upstream_pr_number: int,
    fork_owner: str,
) -> tuple[int, int]:
    """Mark drafted threads approved/rejected from upstream-PR signals.
    Returns (approved, rejected) counts."""
    owner, repo = upstream_full_name.split("/", 1)
    events = gateway.get_timeline_events(owner, repo, upstream_pr_number)
    signals, violations = scan_reply_signals(
        events, fork_owner=fork_owner, config=config,
    )
    for violation in violations:
        with db.transaction():
            db.append_audit(
                actor="agent", phase="info",
                endpoint="review-monitor:reply-signal-violation",
                contribution_id=contribution_id, outcome={"detail": violation},
            )
    approved = rejected = 0
    for signal in signals:
        row = db.conn.execute(
            "SELECT thread_id, response_state, draft_response FROM review_threads"
            " WHERE contribution_id=? AND upstream_comment_id=?",
            (contribution_id, signal.comment_id),
        ).fetchone()
        if row is None or row["response_state"] != "pending" \
                or row["draft_response"] is None:
            continue
        originated, reason = reply_signal_agent_originated(
            db, contribution_id=contribution_id, signal=signal,
        )
        if originated:
            with db.transaction():
                db.append_audit(
                    actor="agent", phase="info",
                    endpoint="review-monitor:reply-cross-check(C-2)",
                    contribution_id=contribution_id,
                    outcome={"thread_id": row["thread_id"], "reason": reason},
                )
            continue
        new_state = "approved" if signal.command == "approve" else "rejected"
        with db.transaction():
            db.conn.execute(
                "UPDATE review_threads SET response_state=?, updated_at=?"
                " WHERE thread_id=?",
                (new_state, utc_now_iso(), row["thread_id"]),
            )
            db.append_audit(
                actor="user", phase="confirmed",
                endpoint=f"review-monitor:reply-{new_state}",
                contribution_id=contribution_id,
                outcome={
                    "thread_id": row["thread_id"],
                    "upstream_comment_id": signal.comment_id,
                    "signal_actor": signal.actor,
                    "github_event_id": signal.event_id,
                    "cross_check": reason,
                },
            )
        if new_state == "approved":
            approved += 1
        else:
            rejected += 1
    return approved, rejected


def post_approved_replies(
    *,
    db: Database,
    gateway: GitHubGateway,
    contribution_id: str,
    upstream_full_name: str,
    upstream_pr_number: int,
) -> int:
    """Post approved drafts via the budgeted reply mutation (§10.1). Budget
    denial leaves the thread approved for the next run. Returns posted count."""
    owner, repo = upstream_full_name.split("/", 1)
    posted = 0
    for row in db.conn.execute(
        "SELECT thread_id, upstream_comment_id, draft_response FROM review_threads"
        " WHERE contribution_id=? AND response_state='approved'",
        (contribution_id,),
    ).fetchall():
        try:
            gateway.reply_to_review_comment(
                owner, repo, upstream_pr_number,
                row["upstream_comment_id"], row["draft_response"],
                contribution_id=contribution_id,
            )
        except BudgetDeniedError as exc:
            with db.transaction():
                db.append_audit(
                    actor="agent", phase="info",
                    endpoint="review-monitor:reply-budget-blocked",
                    contribution_id=contribution_id,
                    outcome={"thread_id": row["thread_id"], "error": str(exc)},
                )
            break  # budget exhausted; later threads will not fare better
        with db.transaction():
            db.conn.execute(
                "UPDATE review_threads SET response_state='posted', updated_at=?"
                " WHERE thread_id=?",
                (utc_now_iso(), row["thread_id"]),
            )
        posted += 1
    return posted


# -- changes-requested → prepared' re-entry (ADR §6) --------------------------


def detect_changes_requested_reentry(
    *,
    store: ContributionStore,
    gateway: GitHubGateway,
    contribution_id: str,
    upstream_full_name: str,
    upstream_pr_number: int,
) -> bool:
    """If any review has state CHANGES_REQUESTED and the contribution is in
    upstream-open/review-loop, re-enter prep (prepared')."""
    current = store.get_state(contribution_id)
    if current not in (State.UPSTREAM_OPEN, State.REVIEW_LOOP):
        return False
    owner, repo = upstream_full_name.split("/", 1)
    reviews = gateway.list_pr_reviews(owner, repo, upstream_pr_number)
    if not any(r.get("state") == "CHANGES_REQUESTED" for r in reviews):
        return False
    if current is State.UPSTREAM_OPEN:
        store.transition(contribution_id, State.REVIEW_LOOP,
                         reason="changes-requested review received")
    store.transition(
        contribution_id, State.PREPARED,
        reason="changes-requested → prepared' re-entry into prep (ADR §6)",
    )
    return True


# -- orchestration ------------------------------------------------------------


@dataclass(frozen=True)
class ReviewMonitorResult:
    drafted: int
    approved: int
    rejected: int
    posted: int
    reentered: bool


def run_review_monitor(
    *,
    db: Database,
    store: ContributionStore,
    gateway: GitHubGateway,
    llm: LLMGateway,
    config: Config,
    contribution_id: str,
    upstream_full_name: str,
    upstream_pr_number: int,
    fork_owner: str,
) -> ReviewMonitorResult:
    drafted = poll_review_threads(
        db=db, gateway=gateway, llm=llm, config=config,
        contribution_id=contribution_id,
        upstream_full_name=upstream_full_name,
        upstream_pr_number=upstream_pr_number,
        fork_owner=fork_owner,
    )
    approved, rejected = apply_reply_signals(
        db=db, gateway=gateway, config=config,
        contribution_id=contribution_id,
        upstream_full_name=upstream_full_name,
        upstream_pr_number=upstream_pr_number,
        fork_owner=fork_owner,
    )
    posted = post_approved_replies(
        db=db, gateway=gateway, contribution_id=contribution_id,
        upstream_full_name=upstream_full_name,
        upstream_pr_number=upstream_pr_number,
    )
    reentered = detect_changes_requested_reentry(
        store=store, gateway=gateway, contribution_id=contribution_id,
        upstream_full_name=upstream_full_name,
        upstream_pr_number=upstream_pr_number,
    )
    return ReviewMonitorResult(len(drafted), approved, rejected, posted, reentered)


def pending_review_drafts(db: Database) -> list[dict[str, Any]]:
    """Drafted-but-unapproved responses, for CLI/report surfacing (AC-5)."""
    return [
        dict(row)
        for row in db.conn.execute(
            "SELECT t.thread_id, t.contribution_id, t.upstream_comment_id,"
            " t.author_login, t.body, t.draft_response, c.repo_full_name,"
            " c.upstream_pr_number"
            " FROM review_threads t"
            " JOIN contributions c ON c.contribution_id = t.contribution_id"
            " WHERE t.response_state='pending' AND t.draft_response IS NOT NULL"
            " ORDER BY t.created_at",
        ).fetchall()
    ]
