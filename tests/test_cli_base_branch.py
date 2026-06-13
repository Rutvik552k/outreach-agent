"""I-1 regression (docs/security/audit-step6.md): cmd_approve_sync must resolve
the upstream repo's ACTUAL default branch for the upstream PR base — never the
old `upstream_base_branch="main"` hardcode (cli.py) — and cache the lookup per
repo within a run (one C5 read per repo, not per draft). Mocked CI lane:
gateway/LLM injected at the cli seams via monkeypatch; no keyring or network.
"""

from __future__ import annotations

import json

import pytest

from conftest import FORK, FORK_OWNER, UPSTREAM, FakeGitHubClient, make_pull_ref
from outreach_agent import cli
from outreach_agent.config import Config
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.persistence import Database
from outreach_agent.state_machine import ContributionStore, State


def _seed_draft_on_fork(db: Database, *, repo_full_name: str, branch: str,
                        draft_pr_number: int = 7) -> str:
    """Walk a contribution through the legal state path to draft-on-fork with
    the row fields cmd_approve_sync reads (branch, prepared_json, fork data)."""
    candidate_id = f"cand-{branch}"
    with db.transaction():
        db.conn.execute(
            "INSERT INTO candidates(candidate_id, repo_full_name, issue_number,"
            " issue_url, stack, contribution_type, score_json, discovered_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (candidate_id, repo_full_name, 12,
             f"https://github.com/{repo_full_name}/issues/12", "python",
             "bugfix-static-analysis", "{}", "2026-06-12T00:00:00+00:00"),
        )
    store = ContributionStore(db)
    cid = store.create(candidate_id=candidate_id, repo_full_name=repo_full_name)
    store.transition(cid, State.SCORED, reason="test seed")
    store.transition(cid, State.POLICY_CLEARED, reason="test seed",
                     fields={"fork_full_name": FORK})
    store.transition(cid, State.PREPARED, reason="test seed", fields={
        "branch": branch,
        "prepared_json": json.dumps(
            {"title": f"Fix via {branch}", "body_md": "test body"}),
    })
    store.transition(cid, State.CI_GREEN, reason="test seed")
    store.transition(cid, State.DRAFT_ON_FORK, reason="test seed",
                     fields={"fork_draft_pr_number": draft_pr_number})
    return cid


@pytest.fixture
def cli_seams(monkeypatch: pytest.MonkeyPatch, gateway: GitHubGateway,
              db: Database, config: Config) -> None:
    """Inject the mocked-lane fakes at the same seams production wires
    (C5 gateway, NFR-7 LLM factory) and configure the login meta."""
    db.set_meta("github_login", FORK_OWNER)
    monkeypatch.setattr(cli, "_build_gateway", lambda db_, config_: gateway)
    monkeypatch.setattr(
        cli, "_build_llm",
        lambda db_, config_: LLMGateway(FakeLLMClient([]), db_, config_),
    )


def test_upstream_pr_base_is_repo_default_branch_not_main(
        cli_seams: None, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        db: Database, config: Config) -> None:
    """A repo whose default branch is 'develop' gets its upstream PR opened
    against 'develop' — the I-1 hardcode would have targeted 'main'."""
    _seed_draft_on_fork(db, repo_full_name=UPSTREAM, branch="fix/issue-12")
    fake_client.default_branches[UPSTREAM] = "develop"
    # approval signal by the fork owner; draft + upstream PR reads stay open
    fake_client.timeline = [
        {"event": "labeled", "actor": {"login": FORK_OWNER},
         "label": {"name": config.label_approve}, "id": 4242},
    ]
    fake_client.pull_response = make_pull_ref(
        number=991, state="open", draft=False,
        base_repo_full_name=UPSTREAM, head_repo_full_name=FORK,
    )

    rc = cli.cmd_approve_sync(db, config)

    assert rc == 0
    upstream_pulls = [p for p in fake_client.created_pulls
                      if f"{p['owner']}/{p['repo']}" == UPSTREAM]
    assert len(upstream_pulls) == 1
    assert upstream_pulls[0]["base"] == "develop"
    assert upstream_pulls[0]["head"] == f"{FORK_OWNER}:fix/issue-12"
    store = ContributionStore(db)
    rows = db.conn.execute("SELECT contribution_id FROM contributions").fetchall()
    assert store.get_state(rows[0]["contribution_id"]) == State.UPSTREAM_OPEN


def test_default_branch_lookup_cached_per_repo_per_run(
        cli_seams: None, fake_client: FakeGitHubClient,
        db: Database, config: Config) -> None:
    """N drafts on one repo cost ONE default-branch read; a second repo costs
    a second. Pending drafts (no approval signal) still exercise the lookup
    because the base is resolved before sync_approval is called."""
    _seed_draft_on_fork(db, repo_full_name=UPSTREAM, branch="fix/a",
                        draft_pr_number=7)
    _seed_draft_on_fork(db, repo_full_name=UPSTREAM, branch="fix/b",
                        draft_pr_number=8)
    _seed_draft_on_fork(db, repo_full_name="other/repo", branch="fix/c",
                        draft_pr_number=9)
    fake_client.timeline = []  # no approval → all outcomes "pending"

    rc = cli.cmd_approve_sync(db, config)

    assert rc == 0
    lookups = [c for c in fake_client.calls if c[0] == "get_repo_default_branch"]
    assert len(lookups) == 2  # one per distinct repo, not one per draft
    assert {(c[1][0], c[1][1]) for c in lookups} == {
        tuple(UPSTREAM.split("/", 1)), ("other", "repo"),
    }
    # unconfigured repos fall back to the fake's "main" default — existing
    # tests keep their behavior without per-test setup
    assert fake_client.default_branches == {}
