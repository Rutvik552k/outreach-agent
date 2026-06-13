from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import FakeGitHubClient
from outreach_agent.config import Config
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.persistence import Database, utc_now_iso
from outreach_agent.policy import evaluate_policy, preflight, recheck_policy


def _insert_candidate(db: Database, candidate_id: str = "c1",
                      repo: str = "acme/some-lib") -> None:
    with db.transaction():
        db.conn.execute(
            "INSERT INTO candidates(candidate_id, repo_full_name, issue_number,"
            " issue_url, stack, contribution_type, score_json, discovered_at)"
            " VALUES(?,?,1,'https://x','python','bugfix-static-analysis','{}',?)",
            (candidate_id, repo, utc_now_iso()),
        )


def test_hard_skip_repo_blocked(gateway: GitHubGateway, config: Config) -> None:
    """ADR §2[2] seed: curl-class repos are hard-skipped before any fetch."""
    for repo in ("curl/curl", "ghostty-org/ghostty", "tldraw/tldraw",
                 "matplotlib/matplotlib"):
        verdict = evaluate_policy(gateway, repo_full_name=repo,
                                  candidate_id="c1", config=config)
        assert verdict.verdict == "blocked"
        assert "hard-skip" in verdict.reasons[0]


def test_restrictive_contributing_blocked(gateway: GitHubGateway,
                                          fake_client: FakeGitHubClient,
                                          config: Config) -> None:
    fake_client.repo_files["acme/some-lib/CONTRIBUTING.md"] = (
        "# Contributing\nWe do not accept AI-generated pull requests.\n"
    )
    verdict = evaluate_policy(gateway, repo_full_name="acme/some-lib",
                              candidate_id="c1", config=config)
    assert verdict.verdict == "blocked"
    assert any("AI" in r for r in verdict.reasons)
    assert "CONTRIBUTING.md" in verdict.sources_checked


def test_external_pr_ban_blocked(gateway: GitHubGateway,
                                 fake_client: FakeGitHubClient,
                                 config: Config) -> None:
    fake_client.repo_files["acme/some-lib/.github/CONTRIBUTING.md"] = (
        "We do not accept external pull requests at this time."
    )
    verdict = evaluate_policy(gateway, repo_full_name="acme/some-lib",
                              candidate_id="c1", config=config)
    assert verdict.verdict == "blocked"


def test_clean_repo_cleared(gateway: GitHubGateway,
                            fake_client: FakeGitHubClient,
                            config: Config) -> None:
    fake_client.repo_files["acme/some-lib/CONTRIBUTING.md"] = (
        "Please open an issue first, then a PR with tests. We welcome "
        "contributions from everyone."
    )
    verdict = evaluate_policy(gateway, repo_full_name="acme/some-lib",
                              candidate_id="c1", config=config)
    assert verdict.verdict == "cleared"


def test_preflight_caches_within_ttl(gateway: GitHubGateway,
                                     fake_client: FakeGitHubClient,
                                     db: Database, config: Config) -> None:
    _insert_candidate(db)
    preflight(gateway, db, config, repo_full_name="acme/some-lib",
              candidate_id="c1")
    fetches_before = len([c for c in fake_client.calls if c[0] == "get_repo_file"])
    preflight(gateway, db, config, repo_full_name="acme/some-lib",
              candidate_id="c1")
    fetches_after = len([c for c in fake_client.calls if c[0] == "get_repo_file"])
    assert fetches_after == fetches_before  # cache hit, no new reads


def test_recheck_ignores_ttl_and_detects_policy_change(
        gateway: GitHubGateway, fake_client: FakeGitHubClient,
        db: Database, config: Config) -> None:
    """FM5: repo policy changed after pre-flight → publish-time recheck blocks."""
    _insert_candidate(db)
    verdict = preflight(gateway, db, config, repo_full_name="acme/some-lib",
                        candidate_id="c1")
    assert verdict.verdict == "cleared"
    fake_client.repo_files["acme/some-lib/CONTRIBUTING.md"] = (
        "AI-generated contributions are not accepted and will be closed."
    )
    assert recheck_policy(gateway, db, config, repo_full_name="acme/some-lib",
                          candidate_id="c1") is False
    row = db.conn.execute(
        "SELECT verdict FROM policy_verdicts WHERE candidate_id='c1'"
    ).fetchone()
    assert row["verdict"] == "blocked"
