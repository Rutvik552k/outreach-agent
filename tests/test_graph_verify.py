from __future__ import annotations

from conftest import UPSTREAM, FakeGitHubClient
from outreach_agent.graph_verify import GraphVerdict, verify_graph_credit

USER_EMAILS = {"12345+rutvik@users.noreply.github.com"}


def _commit(sha: str, email: str, subject: str) -> dict:
    return {"sha": sha, "commit": {"author": {"email": email}, "message": subject}}


def test_primary_mechanism_credited(fake_client: FakeGitHubClient) -> None:
    sha = "a" * 40
    commit = _commit(sha, "12345+rutvik@users.noreply.github.com", "Fix bug (#991)")
    fake_client.commits[sha] = commit
    fake_client.branch_commits = [commit]
    result = verify_graph_credit(
        fake_client, upstream_full_name=UPSTREAM, pull_number=991,
        default_branch="main", user_emails=USER_EMAILS, merge_commit_sha=sha,
    )
    assert result.verdict is GraphVerdict.CREDITED
    assert result.mechanism == "merge_commit_sha"


def test_primary_mechanism_attribution_stripped(fake_client: FakeGitHubClient) -> None:
    """F-01: squash attributed the commit to the merger → graph-missing."""
    sha = "b" * 40
    commit = _commit(sha, "maintainer@acme.dev", "Fix bug (#991)")
    fake_client.commits[sha] = commit
    fake_client.branch_commits = [commit]
    result = verify_graph_credit(
        fake_client, upstream_full_name=UPSTREAM, pull_number=991,
        default_branch="main", user_emails=USER_EMAILS, merge_commit_sha=sha,
    )
    assert result.verdict is GraphVerdict.MISSING


def test_fallback_scan_when_sha_not_on_default_branch(fake_client: FakeGitHubClient) -> None:
    """§10.4 fallback: merge_commit_sha unresolvable → (#pr) subject scan."""
    sha = "c" * 40
    fake_client.commits[sha] = _commit(sha, "x@y.z", "whatever")
    fake_client.branch_commits = [
        _commit("d" * 40, "12345+rutvik@users.noreply.github.com", "Squash fix (#991)"),
    ]
    result = verify_graph_credit(
        fake_client, upstream_full_name=UPSTREAM, pull_number=991,
        default_branch="main", user_emails=USER_EMAILS, merge_commit_sha=sha,
    )
    assert result.verdict is GraphVerdict.CREDITED
    assert result.mechanism == "default-branch-scan"


def test_fallback_ambiguous_goes_manual(fake_client: FakeGitHubClient) -> None:
    fake_client.branch_commits = [
        _commit("e" * 40, "a@b.c", "fix one (#991)"),
        _commit("f" * 40, "d@e.f", "fix two (#991)"),
    ]
    result = verify_graph_credit(
        fake_client, upstream_full_name=UPSTREAM, pull_number=991,
        default_branch="main", user_emails=USER_EMAILS, merge_commit_sha=None,
    )
    assert result.verdict is GraphVerdict.MANUAL
