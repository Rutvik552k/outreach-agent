"""Profile-Growth Engine (§2[7], FR-5, AC-7) — mocked CI lane (ADR §12).

AC-7 (CI-verifiable per §12): the engine produces at least a profile README
improvement proposal + a pinned-repo recommendation + one own-repo cadence
plan. Reads only — no GitHub mutation may occur in this module.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import FORK_OWNER, FakeGitHubClient
from outreach_agent.config import Config
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.persistence import Database
from outreach_agent.profile_growth import (
    KIND_CADENCE,
    KIND_PINNED,
    KIND_README,
    list_profile_actions,
    run_profile_growth,
)

LOGIN = FORK_OWNER


def _repo(name: str, *, stars: int = 0, fork: bool = False, archived: bool = False,
          private: bool = False, pushed: str = "2026-06-01T00:00:00Z",
          topics: list[str] | None = None, issues: int = 0,
          description: str = "") -> dict[str, Any]:
    return {
        "full_name": f"{LOGIN}/{name}", "name": name, "description": description,
        "fork": fork, "archived": archived, "private": private,
        "stargazers_count": stars, "topics": topics or [],
        "open_issues_count": issues, "pushed_at": pushed,
    }


@pytest.fixture
def llm(db: Database, config: Config) -> LLMGateway:
    return LLMGateway(
        FakeLLMClient(["# Improved profile README", "## Weekly cadence plan"]),
        db, config,
    )


def _run(db, gateway, llm, config):
    return run_profile_growth(
        db=db, gateway=gateway, llm=llm, config=config, login=LOGIN,
    )


def test_ac7_profile_engine_produces_all_three_artifacts(
        db: Database, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        llm: LLMGateway, config: Config) -> None:
    """AC-7 named test: README proposal + pinned recommendation + cadence plan,
    all persisted in profile_actions."""
    fake_client.own_repos = [_repo("toolkit", stars=12, topics=["python"])]
    fake_client.repo_files[f"{LOGIN}/{LOGIN}/README.md"] = "# old readme"
    result = _run(db, gateway, llm, config)

    actions = {a["action_id"]: a for a in list_profile_actions(db)}
    assert len(actions) == 3
    readme = actions[result.readme_action_id]
    pinned = actions[result.pinned_action_id]
    cadence = actions[result.cadence_action_id]
    assert readme["kind"] == KIND_README
    assert pinned["kind"] == KIND_PINNED
    assert cadence["kind"] == KIND_CADENCE
    assert all(a["state"] == "proposed" for a in actions.values())
    assert readme["payload"]["proposal_md"] == "# Improved profile README"
    assert readme["payload"]["readme_exists"] is True
    assert pinned["payload"]["recommended"][0]["full_name"] == f"{LOGIN}/toolkit"
    assert "text_md" in pinned["payload"]
    assert cadence["payload"]["week_plan_md"] == "## Weekly cadence plan"


def test_profile_engine_performs_no_github_mutation(
        db: Database, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        llm: LLMGateway, config: Config) -> None:
    """Reads only: proposals are local artifacts; any actual mutation (README
    PR) flows through approval + publisher, never this engine."""
    fake_client.own_repos = [_repo("toolkit", stars=3)]
    _run(db, gateway, llm, config)
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM rate_budget").fetchone()["n"] == 0
    mutating = {"create_pull", "update_pull_state", "create_fork",
                "create_issue_comment", "create_review_comment_reply"}
    assert not [c for c in fake_client.calls if c[0] in mutating]


def test_pinned_recommendation_ranks_and_filters(
        db: Database, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        llm: LLMGateway, config: Config) -> None:
    """Stars desc, then recency; forks/archived/private excluded; capped at
    the 6-pin GitHub limit; text notes manual apply (pinning is GraphQL-only,
    unconfirmable from githubkit REST source)."""
    fake_client.own_repos = [
        _repo("a-fork", stars=99, fork=True),
        _repo("old-archived", stars=98, archived=True),
        _repo("secret", stars=97, private=True),
        _repo("low-old", stars=1, pushed="2025-01-01T00:00:00Z"),
        _repo("low-new", stars=1, pushed="2026-06-01T00:00:00Z"),
        _repo("mid", stars=5),
        _repo("top", stars=50),
        _repo("r4", stars=4), _repo("r3", stars=3), _repo("r2", stars=2),
    ]
    result = _run(db, gateway, llm, config)
    pinned = [a for a in list_profile_actions(db)
              if a["action_id"] == result.pinned_action_id][0]
    names = [r["full_name"] for r in pinned["payload"]["recommended"]]
    assert len(names) == 6  # GitHub pin limit
    assert names[0] == f"{LOGIN}/top" and names[1] == f"{LOGIN}/mid"
    assert f"{LOGIN}/a-fork" not in names
    assert f"{LOGIN}/old-archived" not in names
    assert f"{LOGIN}/secret" not in names
    # equal stars → most recently pushed first
    assert names.index(f"{LOGIN}/low-new") < len(names) \
        and f"{LOGIN}/low-old" not in names
    assert "manually" in pinned["payload"]["text_md"]


def test_readme_proposal_handles_missing_profile_repo_readme(
        db: Database, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        llm: LLMGateway, config: Config) -> None:
    fake_client.own_repos = [_repo("toolkit", stars=1)]
    # no README registered → gateway read returns None
    result = _run(db, gateway, llm, config)
    readme = [a for a in list_profile_actions(db)
              if a["action_id"] == result.readme_action_id][0]
    assert readme["payload"]["readme_exists"] is False
    assert readme["payload"]["target_repo"] == f"{LOGIN}/{LOGIN}"


def test_readme_prompt_contains_current_readme_and_repo_data(
        db: Database, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        config: Config) -> None:
    fake_llm = FakeLLMClient(["readme", "plan"])
    llm = LLMGateway(fake_llm, db, config)
    fake_client.own_repos = [_repo("toolkit", stars=7, description="CLI helpers")]
    fake_client.repo_files[f"{LOGIN}/{LOGIN}/README.md"] = "# current content"
    _run(db, gateway, llm, config)
    readme_call = [c for c in fake_llm.calls
                   if c["model"] and "current content" in str(c["prompt"])]
    assert readme_call, "README prompt must include the current README"
    assert f"{LOGIN}/toolkit" in str(readme_call[0]["prompt"])


def test_report_surfaces_profile_actions(
        db: Database, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        llm: LLMGateway, config: Config) -> None:
    from outreach_agent.report import build_report, render_report

    fake_client.own_repos = [_repo("toolkit")]
    _run(db, gateway, llm, config)
    report = build_report(db, config)
    assert report.profile_actions == {
        KIND_README: 1, KIND_PINNED: 1, KIND_CADENCE: 1,
    }
    text = render_report(report, config)
    assert f"{KIND_PINNED}: 1" in text


def test_cli_profile_command_prints_artifacts(tmp_path, capsys, monkeypatch,
                                              config: Config) -> None:
    """CLI wiring for the new `profile` command (gateway/LLM seams injected)."""
    from outreach_agent import cli
    from outreach_agent.budget import BudgetTracker
    from outreach_agent.github_gateway import GitHubGateway

    db_path = tmp_path / "cli-profile.db"

    fake_client = FakeGitHubClient()
    fake_client.own_repos = [_repo("toolkit", stars=2)]

    def fake_gateway(db, cfg):
        return GitHubGateway(fake_client, db, BudgetTracker(db, cfg), cfg,
                             agent_login=LOGIN, fork_owner=LOGIN)

    def fake_llm(db, cfg):
        return LLMGateway(FakeLLMClient(["# readme", "## plan"]), db, cfg)

    monkeypatch.setattr(cli, "_build_gateway", fake_gateway)
    monkeypatch.setattr(cli, "_build_llm", fake_llm)

    from outreach_agent.persistence import Database as Db
    seed = Db(db_path)
    seed.set_meta("github_login", LOGIN)
    seed.close()

    rc = cli.main(["--db-path", str(db_path), "profile"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "profile-readme-proposal" in out
    assert "pinned-repo-recommendation" in out
    assert "repo-cadence-plan" in out
    assert "approval + publisher path" in out
