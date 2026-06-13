from __future__ import annotations

from typing import Any

from conftest import FakeGitHubClient
from outreach_agent.config import Config
from outreach_agent.discovery import (
    attribution_history,
    build_queries,
    classify,
    discover,
    score_item,
)
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.persistence import Database, new_ulid, utc_now_iso


def _issue(repo: str = "acme/some-lib", number: int = 12, title: str = "Fix crash",
           labels: tuple[str, ...] = ("good first issue",),
           comments: int = 3, **extra: Any) -> dict[str, Any]:
    return {
        "repository_url": f"https://api.github.com/repos/{repo}",
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "labels": [{"name": label} for label in labels],
        "comments": comments,
        "reactions": {"total_count": 2},
        **extra,
    }


def test_allowlist_queries_come_first(config: Config) -> None:
    cfg = Config(db_path=config.db_path,
                 discovery_allowlist=("acme/some-lib:python",))
    queries = build_queries(cfg)
    assert queries[0][0].startswith("repo:acme/some-lib")
    assert queries[0][1] == "python"
    assert all("is:issue is:open" in q for q, _ in queries)


def test_banned_type_titles_are_dropped() -> None:
    """FR-1 banned types: typo/whitespace/image-optimization unrepresentable."""
    assert classify(_issue(title="Fix typo in README")) is None
    assert classify(_issue(title="Whitespace cleanup")) is None
    assert classify(_issue(title="Image optimization pass")) is None


def test_classification_maps_to_allowed_types_only() -> None:
    assert classify(_issue(title="Crash when parsing empty file")) == "bugfix-static-analysis"
    assert classify(_issue(title="Add tests for parser")) == "test-addition"
    assert classify(_issue(title="Bump lodash dependency")) == "dependency-bump"
    assert classify(_issue(title="Question about usage")) == "issue-triage"


def test_attribution_history_deprioritizes_stripping_repos(db: Database) -> None:
    """F-01: graph-missing outcomes lower the repo's attribution score."""
    assert attribution_history(db, "acme/some-lib") == 1.0
    with db.transaction():
        db.conn.execute(
            "INSERT INTO contributions(contribution_id, repo_full_name, state,"
            " created_at, updated_at) VALUES('cx','acme/some-lib','graph-missing',?,?)",
            (utc_now_iso(), utc_now_iso()),
        )
        db.conn.execute(
            "INSERT INTO kpi_outcomes(outcome_id, contribution_id, outcome,"
            " counts_in_merge_rate, graph_credit, recorded_at)"
            " VALUES(?,?,?,?,?,?)",
            (new_ulid(), "cx", "graph-missing", 0, "missing", utc_now_iso()),
        )
    assert attribution_history(db, "acme/some-lib") == 0.0
    score = score_item(_issue(), attribution=0.0)
    full = score_item(_issue(), attribution=1.0)
    assert score.total < full.total


def test_discover_scores_persists_and_dedupes(gateway: GitHubGateway,
                                              fake_client: FakeGitHubClient,
                                              db: Database) -> None:
    fake_client.search_results = [
        _issue(number=1, title="Crash on empty input"),
        _issue(number=1, title="Crash on empty input"),       # duplicate
        _issue(number=2, title="Fix typo in docs"),           # banned
        _issue(number=3, title="PR not issue", pull_request={"url": "x"}),
    ]
    candidates = discover(gateway, db, config=gateway.config)
    numbers = {c.issue_number for c in candidates}
    assert numbers == {1}
    row = db.conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()
    assert row["n"] == 1
    audit = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint='discovery:run'"
    ).fetchone()
    assert audit is not None
