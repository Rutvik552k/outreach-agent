from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from conftest import FORK, FORK_OWNER, UPSTREAM, FakeGitHubClient, make_pull_ref
from outreach_agent.config import Config
from outreach_agent.contracts import PreparedContribution
from outreach_agent.diff_checks import DiffChecks, DiffStat
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.persistence import Database, utc_now_iso
from outreach_agent.prep import FakeGitRunner, build_pr_text
from outreach_agent.publisher import (
    check_merge_rate_pause,
    run_graph_verification,
    submit_for_approval,
    sync_approval,
    sync_outcome,
)
from outreach_agent.sandbox import SandboxResult, Verdict
from outreach_agent.state_machine import ContributionStore, State


def _prepared(cid: str) -> PreparedContribution:
    return PreparedContribution(
        contribution_id=cid,
        branch="agent/12-fix",
        base_sha="abc123",
        diff_stat=DiffStat(files=1, insertions=2, deletions=1),
        diff_checks=DiffChecks(False, False, False, False),
        sandbox_run=SandboxResult(0, 0, 30, "log", Verdict.GREEN),
        pr_text=build_pr_text(title="Fix crash", description_md="Handles ''.",
                              issue_url="https://github.com/acme/some-lib/issues/12",
                              model="claude-opus-4-8"),
    )


@pytest.fixture
def store(db: Database) -> ContributionStore:
    return ContributionStore(db)


def _to_draft(store: ContributionStore, gateway: GitHubGateway,
              db: Database, config: Config) -> str:
    cid = store.create(candidate_id=None, repo_full_name=UPSTREAM)
    for s in (State.SCORED, State.POLICY_CLEARED, State.PREPARED, State.CI_GREEN):
        store.transition(cid, s)
    git = FakeGitRunner()
    number = submit_for_approval(
        db=db, store=store, gateway=gateway, git=git, config=config,
        contribution_id=cid, prepared=_prepared(cid),
        fork_full_name=FORK, fork_default_branch="main",
        upstream_full_name=UPSTREAM, work_dir=None,
    )
    assert number == 7
    push = next(args for args, _ in git.calls if args[0] == "push")
    # M-3: refspec follows `--` end-of-options; branch shape pre-validated.
    assert push[-2] == "--" and push[-1].startswith("agent/")
    return cid


def test_push_refuses_unpinned_branch_shape(store: ContributionStore,
                                            gateway: GitHubGateway,
                                            db: Database, config: Config) -> None:
    """M-3: a branch not matching ^agent/<issue>-<slug>$ (e.g. a leading-dash
    value a future refactor could let through) is refused before git runs."""
    from outreach_agent.errors import GitOperationError

    cid = store.create(candidate_id=None, repo_full_name=UPSTREAM)
    for s in (State.SCORED, State.POLICY_CLEARED, State.PREPARED, State.CI_GREEN):
        store.transition(cid, s)
    git = FakeGitRunner()
    bad = dataclasses.replace(_prepared(cid), branch="--force-with-lease=x")
    with pytest.raises(GitOperationError):
        submit_for_approval(
            db=db, store=store, gateway=gateway, git=git, config=config,
            contribution_id=cid, prepared=bad,
            fork_full_name=FORK, fork_default_branch="main",
            upstream_full_name=UPSTREAM, work_dir=None,
        )
    assert git.calls == []


def _label_event(name: str, actor: str, event_id: int = 100) -> dict[str, Any]:
    return {"event": "labeled", "actor": {"login": actor},
            "label": {"name": name}, "id": event_id}


def test_submit_uses_title_marker_never_label(store: ContributionStore,
                                              gateway: GitHubGateway,
                                              fake_client: FakeGitHubClient,
                                              db: Database, config: Config) -> None:
    """C4 v2.2: awaiting-approval marker lives in the draft TITLE; the gateway
    performs no label mutation (C-2 coarse rule would void the draft)."""
    cid = _to_draft(store, gateway, db, config)
    assert store.get_state(cid) == State.DRAFT_ON_FORK
    sent = fake_client.created_pulls[0]
    assert sent["draft"] is True and sent["base"] == "main"
    title = [c for c in fake_client.calls if c[0] == "create_pull"][0][2]["title"]
    assert config.label_awaiting in title
    label_rows = db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE endpoint LIKE '%/labels%'"
    ).fetchone()
    assert label_rows["n"] == 0


def test_approval_publishes_upstream_then_closes_draft(
        store: ContributionStore, gateway: GitHubGateway,
        fake_client: FakeGitHubClient, db: Database, config: Config) -> None:
    cid = _to_draft(store, gateway, db, config)
    fake_client.timeline = [_label_event(config.label_approve, FORK_OWNER)]
    outcome = sync_approval(
        db=db, store=store, gateway=gateway, config=config,
        contribution_id=cid, fork_owner=FORK_OWNER, fork_full_name=FORK,
        draft_pr_number=7, upstream_full_name=UPSTREAM,
        upstream_base_branch="main", prepared_title="Fix crash",
        prepared_body=_prepared(cid).pr_text.body_md,
        head_branch="agent/12-fix", policy_recheck=lambda: True,
    )
    assert outcome.status == "published"
    assert store.get_state(cid) == State.UPSTREAM_OPEN
    # two-PR model: draft on fork + upstream PR + draft close, in order (F-04)
    kinds = [r["kind"] for r in db.conn.execute(
        "SELECT kind FROM rate_budget ORDER BY seq")]
    assert kinds == ["fork_draft_pr", "upstream_pr", "fork_draft_close"]
    upstream_call = [c for c in fake_client.created_pulls if not c["draft"]][0]
    assert upstream_call["head"] == f"{FORK_OWNER}:agent/12-fix"


def test_no_signal_stays_pending(store: ContributionStore, gateway: GitHubGateway,
                                 fake_client: FakeGitHubClient,
                                 db: Database, config: Config) -> None:
    cid = _to_draft(store, gateway, db, config)
    fake_client.timeline = []
    outcome = sync_approval(
        db=db, store=store, gateway=gateway, config=config,
        contribution_id=cid, fork_owner=FORK_OWNER, fork_full_name=FORK,
        draft_pr_number=7, upstream_full_name=UPSTREAM,
        upstream_base_branch="main", prepared_title="t", prepared_body="b",
        head_branch="agent/12-fix", policy_recheck=lambda: True,
    )
    assert outcome.status == "pending"
    assert store.get_state(cid) == State.DRAFT_ON_FORK
    assert not [c for c in fake_client.created_pulls if not c["draft"]]


def test_rejection_signal_closes_and_records(store: ContributionStore,
                                             gateway: GitHubGateway,
                                             fake_client: FakeGitHubClient,
                                             db: Database, config: Config) -> None:
    cid = _to_draft(store, gateway, db, config)
    fake_client.timeline = [
        {"event": "commented", "actor": {"login": FORK_OWNER},
         "body": "/reject too risky", "id": 9},
    ]
    outcome = sync_approval(
        db=db, store=store, gateway=gateway, config=config,
        contribution_id=cid, fork_owner=FORK_OWNER, fork_full_name=FORK,
        draft_pr_number=7, upstream_full_name=UPSTREAM,
        upstream_base_branch="main", prepared_title="t", prepared_body="b",
        head_branch="agent/12-fix", policy_recheck=lambda: True,
    )
    assert outcome.status == "rejected"
    assert store.get_state(cid) == State.REJECTED


def test_merge_detection_schedules_graph_verify(store: ContributionStore,
                                                gateway: GitHubGateway,
                                                fake_client: FakeGitHubClient,
                                                db: Database, config: Config) -> None:
    cid = store.create(candidate_id=None, repo_full_name=UPSTREAM)
    for s in (State.SCORED, State.POLICY_CLEARED, State.PREPARED, State.CI_GREEN,
              State.DRAFT_ON_FORK, State.APPROVED, State.UPSTREAM_OPEN):
        store.transition(cid, s)
    fake_client.pull_response = make_pull_ref(
        number=991, state="closed", draft=False, merged=True,
        merge_commit_sha="deadbeef", base_repo_full_name=UPSTREAM,
    )
    state = sync_outcome(
        db=db, store=store, gateway=gateway, config=config,
        contribution_id=cid, upstream_full_name=UPSTREAM, upstream_pr_number=991,
    )
    assert state == State.GRAPH_VERIFY
    row = db.conn.execute(
        "SELECT merge_commit_sha, merged_at FROM contributions"
        " WHERE contribution_id=?", (cid,)).fetchone()
    assert row["merge_commit_sha"] == "deadbeef" and row["merged_at"]
    kpi = db.conn.execute("SELECT outcome FROM kpi_outcomes").fetchone()
    assert kpi["outcome"] == "merged"


def test_graph_verify_waits_24h_then_credits(store: ContributionStore,
                                             gateway: GitHubGateway,
                                             fake_client: FakeGitHubClient,
                                             db: Database, config: Config) -> None:
    cid = store.create(candidate_id=None, repo_full_name=UPSTREAM)
    for s in (State.SCORED, State.POLICY_CLEARED, State.PREPARED, State.CI_GREEN,
              State.DRAFT_ON_FORK, State.APPROVED, State.UPSTREAM_OPEN,
              State.MERGED, State.GRAPH_VERIFY):
        store.transition(cid, s)
    merged_at = datetime.now(timezone.utc) - timedelta(hours=30)
    with db.transaction():
        db.conn.execute(
            "UPDATE contributions SET merged_at=?, merge_commit_sha='deadbeef'"
            " WHERE contribution_id=?", (merged_at.isoformat(), cid))
    commit = {"sha": "deadbeef",
              "commit": {"author": {"email": "me@example.com"},
                         "message": "Fix crash (#991)"}}
    fake_client.commits["deadbeef"] = commit
    fake_client.branch_commits = [commit]

    # not due yet → stays
    early = run_graph_verification(
        db=db, store=store, gateway=gateway, config=config,
        contribution_id=cid, upstream_full_name=UPSTREAM, upstream_pr_number=991,
        default_branch="main", user_emails={"me@example.com"},
        now=merged_at + timedelta(hours=1))
    assert early == State.GRAPH_VERIFY

    state = run_graph_verification(
        db=db, store=store, gateway=gateway, config=config,
        contribution_id=cid, upstream_full_name=UPSTREAM, upstream_pr_number=991,
        default_branch="main", user_emails={"me@example.com"})
    assert state == State.GRAPH_CREDITED
    kpi = db.conn.execute(
        "SELECT graph_credit FROM kpi_outcomes WHERE graph_credit IS NOT NULL"
    ).fetchone()
    assert kpi["graph_credit"] == "credited"


def test_merge_rate_auto_pause(db: Database, config: Config,
                               store: ContributionStore) -> None:
    """§8: < 35% over trailing window (min 5 outcomes) → global pause."""
    from outreach_agent.publisher import _record_kpi

    for i in range(5):
        cid = store.create(candidate_id=None, repo_full_name=UPSTREAM)
        _record_kpi(db, cid, outcome="closed" if i else "merged",
                    counts_in_merge_rate=True)
    assert check_merge_rate_pause(db, config) is True  # 1/5 = 20% < 35%
    assert db.global_pause_reason() is not None
