from __future__ import annotations

import dataclasses

import pytest

from conftest import FORK, UPSTREAM, FakeGitHubClient, make_pull_ref
from outreach_agent.errors import (
    BudgetDeniedError,
    GitHubMutationError,
    IntraForkInvariantError,
)
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.persistence import Database


def _audit_phases(db: Database, endpoint_like: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT phase FROM audit_log WHERE endpoint LIKE ? ORDER BY seq",
        (endpoint_like,),
    ).fetchall()
    return [r["phase"] for r in rows]


def test_intra_fork_invariant_violation_aborts(gateway: GitHubGateway,
                                               fake_client: FakeGitHubClient,
                                               db: Database) -> None:
    """F-03: GitHub defaulting the fork PR's base to the upstream parent must
    abort the flow and audit the violation."""
    fake_client.pull_response = make_pull_ref(
        base_repo_full_name=UPSTREAM,  # the documented default-to-parent pitfall
        head_repo_full_name=FORK,
    )
    with pytest.raises(IntraForkInvariantError):
        gateway.create_draft_pr_on_fork(
            fork_full_name=FORK, head_branch="agent/123-fix", base_branch="main",
            title="t", body="b",
        )
    violations = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint='invariant:intra-fork(F-03)'"
    ).fetchall()
    assert len(violations) == 1
    assert UPSTREAM in violations[0]["outcome_json"]


def test_intra_fork_pr_happy_path(gateway: GitHubGateway,
                                  fake_client: FakeGitHubClient,
                                  db: Database) -> None:
    pr = gateway.create_draft_pr_on_fork(
        fork_full_name=FORK, head_branch="agent/123-fix", base_branch="main",
        title="t", body="b",
    )
    assert pr.draft is True
    sent = fake_client.created_pulls[0]
    assert sent["base"] == "main" and sent["draft"] is True
    assert _audit_phases(db, "POST /repos/{fork_owner}/{fork}/pulls") == \
        ["intent", "confirmed"]


def test_mutation_failure_audits_failed(gateway: GitHubGateway,
                                        fake_client: FakeGitHubClient,
                                        db: Database) -> None:
    fake_client.fail_next = RuntimeError("422 Validation Failed")
    with pytest.raises(GitHubMutationError):
        gateway.comment(repo_full_name=UPSTREAM, issue_number=5, body="hi")
    assert _audit_phases(db, "POST /repos/{owner}/{repo}/issues%") == \
        ["intent", "failed"]


def test_get_issue_is_a_read_no_budget_no_audit(gateway: GitHubGateway,
                                                fake_client: FakeGitHubClient,
                                                db: Database) -> None:
    """ADR-002 §4 (C5): get_issue is a READ — it returns the title + body, does
    NOT consume budget, and writes NO audit row (reads route through _read, not
    _mutate)."""
    fake_client.issues["acme/some-lib#12"] = {
        "title": "Crash on empty input",
        "body": "parse('') raises IndexError",
    }
    audit_before = db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
    issue = gateway.get_issue("acme", "some-lib", 12)
    assert issue == {"number": 12, "title": "Crash on empty input",
                     "body": "parse('') raises IndexError"}
    # no audit row was appended (reads are not budgeted/audited mutations)
    audit_after = db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
    assert audit_after == audit_before
    assert any(c[0] == "get_issue" for c in fake_client.calls)


def test_get_issue_missing_body_normalized_to_empty(
        gateway: GitHubGateway, fake_client: FakeGitHubClient) -> None:
    """ADR-002 §4: a Missing/None issue body is normalised to "" so generation
    proceeds on the title alone (degraded, not blocked)."""
    fake_client.issues["acme/some-lib#7"] = {"title": "Title only"}
    issue = gateway.get_issue("acme", "some-lib", 7)
    assert issue["title"] == "Title only" and issue["body"] == ""


def test_budget_denial_blocks_call_before_intent(gateway: GitHubGateway,
                                                 fake_client: FakeGitHubClient,
                                                 db: Database) -> None:
    gateway.create_upstream_pr(
        upstream_full_name=UPSTREAM, fork_owner_login="rutvik",
        head_branch="agent/123-fix", base_branch="main", title="t", body="b",
    )
    with pytest.raises(BudgetDeniedError):
        gateway.create_upstream_pr(
            upstream_full_name=UPSTREAM, fork_owner_login="rutvik",
            head_branch="agent/456-fix", base_branch="main", title="t", body="b",
        )
    # Only the first attempt reached the client.
    creates = [c for c in fake_client.calls if c[0] == "create_pull"]
    assert len(creates) == 1


def test_upstream_pr_uses_user_colon_branch_head(gateway: GitHubGateway,
                                                 fake_client: FakeGitHubClient) -> None:
    """F-04 two-PR model: upstream PR head must be 'user:branch'."""
    fake_client.pull_response = make_pull_ref(
        base_repo_full_name=UPSTREAM, head_repo_full_name=FORK, draft=False,
    )
    gateway.create_upstream_pr(
        upstream_full_name=UPSTREAM, fork_owner_login="rutvik",
        head_branch="agent/123-fix", base_branch="main", title="t", body="b",
    )
    sent = fake_client.created_pulls[0]
    assert sent["head"] == "rutvik:agent/123-fix"
    assert sent["draft"] is False


def test_reply_to_review_comment_signature(gateway: GitHubGateway,
                                           fake_client: FakeGitHubClient) -> None:
    """§10.1: pull_number is part of the reply path."""
    gateway.reply_to_review_comment("acme", "some-lib", 991, 12345, "thanks")
    name, args, kwargs = [c for c in fake_client.calls
                          if c[0] == "create_review_comment_reply"][0]
    assert args == ("acme", "some-lib", 991, 12345)
    assert kwargs == {"body": "thanks"}


def test_close_fork_draft_is_distinct_budgeted_mutation(gateway: GitHubGateway,
                                                        db: Database) -> None:
    gateway.close_fork_draft_pr(fork_full_name=FORK, pull_number=7)
    row = db.conn.execute(
        "SELECT kind FROM rate_budget ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row["kind"] == "fork_draft_close"


def test_confirmed_mutation_stores_github_object_id(gateway: GitHubGateway,
                                                    db: Database) -> None:
    """C-2: gateway comment mutations capture the GitHub-returned object id in
    the confirmed audit event — column mirror AND hash-covered outcome_json."""
    result = gateway.comment(repo_full_name=UPSTREAM, issue_number=5, body="repro")
    row = db.conn.execute(
        "SELECT github_object_id, outcome_json FROM audit_log"
        " WHERE phase='confirmed' AND endpoint LIKE '%/comments' ORDER BY seq DESC"
    ).fetchone()
    assert row["github_object_id"] == str(result["id"])
    assert str(result["id"]) in row["outcome_json"]
    # tampering with the mirror column alone must break chain verification
    db.conn.execute("UPDATE audit_log SET github_object_id='forged' "
                    "WHERE github_object_id=?", (str(result["id"]),))
    import pytest as _pytest

    from outreach_agent.errors import ChainIntegrityError
    with _pytest.raises(ChainIntegrityError):
        db.verify_chains()
    db.clear_global_pause()
    db.conn.execute("UPDATE audit_log SET github_object_id=? "
                    "WHERE github_object_id='forged'", (str(result["id"]),))


def test_mutation_landed_idempotency_helper(gateway: GitHubGateway,
                                            fake_client: FakeGitHubClient) -> None:
    fake_client.pull_response = make_pull_ref(state="open")
    assert gateway.mutation_landed(
        lambda: gateway.get_pr("rutvik", "some-lib", 7).state == "open"
    )
    def _raises() -> bool:
        raise RuntimeError("404")
    assert gateway.mutation_landed(_raises) is False
