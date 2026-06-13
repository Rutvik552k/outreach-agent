"""Review Monitor (§2[6], FR-4/AC-5) — mocked CI lane (ADR §12).

Covers: thread persistence + LLM drafting, never-auto-post, the C4 reply
approval machinery on the upstream PR (owner binding, C-2 exact-id
cross-check, fail-closed ambiguity), budgeted posting, and the
changes-requested → prepared' re-entry hook.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import FORK_OWNER, UPSTREAM, FakeGitHubClient
from outreach_agent.config import Config
from outreach_agent.errors import LlmBudgetError
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.persistence import Database
from outreach_agent.review_monitor import (
    pending_review_drafts,
    run_review_monitor,
    scan_reply_signals,
)
from outreach_agent.state_machine import ContributionStore, State

MAINTAINER = "upstream-maintainer"


@pytest.fixture
def store(db: Database) -> ContributionStore:
    return ContributionStore(db)


@pytest.fixture
def llm(db: Database, config: Config) -> LLMGateway:
    return LLMGateway(FakeLLMClient(["Thanks — fixed in the next push."]),
                      db, config)


def _upstream_contribution(store: ContributionStore) -> str:
    cid = store.create(candidate_id=None, repo_full_name=UPSTREAM)
    for s in (State.SCORED, State.POLICY_CLEARED, State.PREPARED, State.CI_GREEN,
              State.DRAFT_ON_FORK, State.APPROVED, State.UPSTREAM_OPEN):
        store.transition(cid, s)
    return cid


def _review_comment(comment_id: int, author: str, body: str,
                    in_reply_to: int | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {"id": comment_id, "user": {"login": author}, "body": body}
    if in_reply_to is not None:
        c["in_reply_to_id"] = in_reply_to
    return c


def _monitor(db, store, gateway, llm, config, cid):
    return run_review_monitor(
        db=db, store=store, gateway=gateway, llm=llm, config=config,
        contribution_id=cid, upstream_full_name=UPSTREAM,
        upstream_pr_number=55, fork_owner=FORK_OWNER,
    )


def test_polls_persists_and_drafts_maintainer_comments(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, llm: LLMGateway, config: Config) -> None:
    cid = _upstream_contribution(store)
    fake_client.review_comments = [
        _review_comment(101, MAINTAINER, "Please add a regression test."),
        _review_comment(102, FORK_OWNER, "my own comment — not a thread"),
        _review_comment(103, MAINTAINER, "reply in thread", in_reply_to=101),
    ]
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.drafted == 1 and result.posted == 0
    rows = db.conn.execute(
        "SELECT * FROM review_threads WHERE contribution_id=?", (cid,)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["upstream_comment_id"] == 101
    assert row["author_login"] == MAINTAINER
    assert row["response_state"] == "pending"
    assert row["draft_response"] == "Thanks — fixed in the next push."
    # never auto-post: no reply mutation without an approval signal
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM rate_budget WHERE kind='review_reply'"
    ).fetchone()["n"] == 0
    # surfaced for approval (AC-5)
    drafts = pending_review_drafts(db)
    assert len(drafts) == 1 and drafts[0]["upstream_comment_id"] == 101


def test_llm_blocked_leaves_thread_pending_and_retries(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, config: Config) -> None:
    """F-13: an LLM hard stop never loses the thread; the draft is retried."""
    cid = _upstream_contribution(store)
    fake_client.review_comments = [_review_comment(201, MAINTAINER, "Nit: rename.")]
    fake = FakeLLMClient(["Renamed as suggested."])
    fake.fail_next = LlmBudgetError("cap reached")
    llm = LLMGateway(fake, db, config)
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.drafted == 0
    row = db.conn.execute("SELECT * FROM review_threads").fetchone()
    assert row["response_state"] == "pending" and row["draft_response"] is None
    # next run drafts it
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.drafted == 1
    row = db.conn.execute("SELECT * FROM review_threads").fetchone()
    assert row["draft_response"] == "Renamed as suggested."


def test_approve_reply_signal_posts_budgeted_reply(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, llm: LLMGateway, config: Config) -> None:
    cid = _upstream_contribution(store)
    fake_client.review_comments = [_review_comment(301, MAINTAINER, "Why this cast?")]
    fake_client.timeline = [
        {"event": "commented", "actor": {"login": FORK_OWNER},
         "body": "/approve-reply 301", "id": 7001},
    ]
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.approved == 1 and result.posted == 1
    row = db.conn.execute("SELECT * FROM review_threads").fetchone()
    assert row["response_state"] == "posted"
    # the post is a budgeted mutation through the gateway (C5/C7)
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM rate_budget WHERE kind='review_reply'"
    ).fetchone()["n"] == 1
    reply_calls = [c for c in fake_client.calls
                   if c[0] == "create_review_comment_reply"]
    assert reply_calls[0][1][:4] == (UPSTREAM.split("/")[0],
                                     UPSTREAM.split("/")[1], 55, 301)
    # the user approval is audited with actor=user (AC-3 style proof)
    audit = db.conn.execute(
        "SELECT actor FROM audit_log WHERE endpoint='review-monitor:reply-approved'"
    ).fetchone()
    assert audit["actor"] == "user"


def test_reject_reply_signal_never_posts(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, llm: LLMGateway, config: Config) -> None:
    cid = _upstream_contribution(store)
    fake_client.review_comments = [_review_comment(401, MAINTAINER, "Hmm.")]
    fake_client.timeline = [
        {"event": "commented", "actor": {"login": FORK_OWNER},
         "body": "/reject-reply 401 too defensive", "id": 7002},
    ]
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.rejected == 1 and result.posted == 0
    row = db.conn.execute("SELECT * FROM review_threads").fetchone()
    assert row["response_state"] == "rejected"
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM rate_budget WHERE kind='review_reply'"
    ).fetchone()["n"] == 0


def test_invalid_actor_signal_is_violation_not_approval(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, llm: LLMGateway, config: Config) -> None:
    """V2: only the fork owner can approve a reply."""
    cid = _upstream_contribution(store)
    fake_client.review_comments = [_review_comment(501, MAINTAINER, "Q")]
    fake_client.timeline = [
        {"event": "commented", "actor": {"login": "third-party"},
         "body": "/approve-reply 501", "id": 7003},
    ]
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.approved == 0 and result.posted == 0
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log"
        " WHERE endpoint='review-monitor:reply-signal-violation'"
    ).fetchone()["n"] == 1


def test_agent_originated_signal_rejected_by_cross_check(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, llm: LLMGateway, config: Config) -> None:
    """C-2 exact-id membership: a signal whose event id matches an
    agent-confirmed comment mutation on this contribution is rejected."""
    cid = _upstream_contribution(store)
    owner, repo = UPSTREAM.split("/", 1)
    # legit agent comment on the upstream PR (allowed; not approval-class) —
    # FakeGitHubClient returns id 9001 for the first comment
    gateway.comment(repo_full_name=UPSTREAM, issue_number=55,
                    body="Posting CI evidence.", contribution_id=cid)
    fake_client.review_comments = [_review_comment(601, MAINTAINER, "Q")]
    fake_client.timeline = [
        {"event": "commented", "actor": {"login": FORK_OWNER},
         "body": "/approve-reply 601", "id": 9001},  # collides with agent comment id
    ]
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.approved == 0 and result.posted == 0
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log"
        " WHERE endpoint='review-monitor:reply-cross-check(C-2)'"
    ).fetchone()["n"] == 1
    row = db.conn.execute("SELECT response_state FROM review_threads").fetchone()
    assert row["response_state"] == "pending"


def test_changes_requested_reenters_prep(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, llm: LLMGateway, config: Config) -> None:
    """ADR §6: review-loop ⇄ (changes-requested → prepared')."""
    cid = _upstream_contribution(store)
    fake_client.pr_reviews = [
        {"id": 1, "state": "COMMENTED", "user": {"login": MAINTAINER}},
        {"id": 2, "state": "CHANGES_REQUESTED", "user": {"login": MAINTAINER}},
    ]
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.reentered is True
    assert store.get_state(cid) == State.PREPARED
    # idempotent: a second run while in prepared does not re-fire
    result = _monitor(db, store, gateway, llm, config, cid)
    assert result.reentered is False
    assert store.get_state(cid) == State.PREPARED


def test_scan_reply_signals_token_exactness(config: Config) -> None:
    """Exact-token parsing: '/approve-reply' never reads as draft '/approve',
    and malformed targets are violations."""
    events = [
        {"event": "commented", "actor": {"login": FORK_OWNER},
         "body": "/approve-reply abc", "id": 1},
        {"event": "commented", "actor": {"login": FORK_OWNER},
         "body": "/approve-reply", "id": 2},  # missing target → ignored
        {"event": "commented", "actor": {"login": FORK_OWNER},
         "body": "/approve-reply 42 looks good", "id": 3},
    ]
    signals, violations = scan_reply_signals(
        events, fork_owner=FORK_OWNER, config=config)
    assert len(signals) == 1
    assert signals[0].comment_id == 42 and signals[0].command == "approve"
    assert len(violations) == 1  # the unparseable 'abc' target


def test_report_surfaces_pending_drafts(
        db: Database, store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, llm: LLMGateway, config: Config) -> None:
    from outreach_agent.report import build_report, render_report

    cid = _upstream_contribution(store)
    fake_client.review_comments = [_review_comment(701, MAINTAINER, "Q")]
    _monitor(db, store, gateway, llm, config, cid)
    report = build_report(db, config)
    assert report.pending_review_drafts == 1
    assert "pending review-response drafts (need /approve-reply): 1" \
        in render_report(report, config)
