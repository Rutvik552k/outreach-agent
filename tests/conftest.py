from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from outreach_agent.budget import BudgetTracker
from outreach_agent.config import Config
from outreach_agent.github_gateway import GitHubGateway, PullRef
from outreach_agent.outbound_safety import clear_loaded_secret_values
from outreach_agent.persistence import Database


@pytest.fixture(autouse=True)
def _isolate_secret_registry():
    """The M-1 loaded-credential registry is process-global by design (it
    must survive for the agent's lifetime in production). Tests clear it
    after each case so a value registered in one test can never make an
    unrelated test's prompt trip the value-redaction guard."""
    yield
    clear_loaded_secret_values()

FORK = "rutvik/some-lib"
UPSTREAM = "acme/some-lib"
FORK_OWNER = "rutvik"
# V2 v2.1: the agent's OAuth token acts AS the user, so agent_login == fork_owner
# in production. Not-agent-originated is enforced by structural incapability +
# the C-2 audit cross-check, never by login comparison.
AGENT_LOGIN = FORK_OWNER


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(db_path=tmp_path / "state.db", min_mutation_spacing_s=0.0)


@pytest.fixture
def db(config: Config) -> Database:
    database = Database(config.db_path)
    yield database
    database.close()


@pytest.fixture
def budget(db: Database, config: Config) -> BudgetTracker:
    return BudgetTracker(db, config)


def make_pull_ref(**overrides: Any) -> PullRef:
    defaults = dict(
        number=7, node_id="PR_node7", state="open", draft=True,
        base_repo_full_name=FORK, head_repo_full_name=FORK,
        merged=False, merge_commit_sha=None, html_url=f"https://github.com/{FORK}/pull/7",
    )
    defaults.update(overrides)
    return PullRef(**defaults)


class FakeGitHubClient:
    """Mock at the C5 seam (ADR §12 mocked CI lane)."""

    def __init__(self) -> None:
        self.created_pulls: list[dict[str, Any]] = []
        self.pull_response: PullRef = make_pull_ref()
        self.timeline: list[dict[str, Any]] = []
        self.commits: dict[str, dict[str, Any]] = {}
        self.branch_commits: list[dict[str, Any]] = []
        self.search_results: list[dict[str, Any]] = []
        self.repo_files: dict[str, str] = {}  # "owner/repo/path" -> content
        self.review_comments: list[dict[str, Any]] = []
        self.pr_reviews: list[dict[str, Any]] = []
        self.own_repos: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail_next: Exception | None = None
        self.next_comment_id: int = 9001

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def create_pull(self, owner, repo, *, title, head, base, body, draft) -> PullRef:
        self._record("create_pull", owner, repo, title=title, head=head,
                     base=base, body=body, draft=draft)
        self.created_pulls.append(dict(owner=owner, repo=repo, head=head,
                                       base=base, draft=draft))
        return self.pull_response

    def update_pull_state(self, owner, repo, pull_number, *, state) -> PullRef:
        self._record("update_pull_state", owner, repo, pull_number, state=state)
        return dataclasses.replace(self.pull_response, state=state)

    def get_pull(self, owner, repo, pull_number) -> PullRef:
        self._record("get_pull", owner, repo, pull_number)
        return self.pull_response

    def create_fork(self, owner, repo):
        self._record("create_fork", owner, repo)
        return {"full_name": FORK}

    def create_issue_comment(self, owner, repo, issue_number, *, body):
        self._record("create_issue_comment", owner, repo, issue_number, body=body)
        cid, self.next_comment_id = self.next_comment_id, self.next_comment_id + 1
        return {"id": cid}

    def create_review_comment_reply(self, owner, repo, pull_number, comment_id, *, body):
        self._record("create_review_comment_reply", owner, repo, pull_number,
                     comment_id, body=body)
        return {"id": 2}

    def list_review_comments(self, owner, repo, pull_number):
        self._record("list_review_comments", owner, repo, pull_number)
        return self.review_comments

    def list_pr_reviews(self, owner, repo, pull_number):
        self._record("list_pr_reviews", owner, repo, pull_number)
        return self.pr_reviews

    def list_repos_for_authenticated_user(self, *, type, sort, per_page):
        self._record("list_repos_for_authenticated_user", type=type, sort=sort,
                     per_page=per_page)
        return self.own_repos

    def list_timeline_events(self, owner, repo, issue_number):
        self._record("list_timeline_events", owner, repo, issue_number)
        return self.timeline

    def get_commit(self, owner, repo, ref):
        self._record("get_commit", owner, repo, ref)
        return self.commits[ref]

    def list_commits(self, owner, repo, *, sha, per_page=50):
        self._record("list_commits", owner, repo, sha=sha, per_page=per_page)
        return self.branch_commits

    def search_issues(self, query):
        self._record("search_issues", query)
        return self.search_results

    def get_repo_file(self, owner, repo, path):
        self._record("get_repo_file", owner, repo, path)
        return self.repo_files.get(f"{owner}/{repo}/{path}")

    def rate_headers(self):
        return (4999, 1760000000)


FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_REPOS_DIR = FIXTURES_DIR / "repos"
FIXTURE_DIFFS_DIR = FIXTURES_DIR / "diffs"


def read_diff_fixture(name: str) -> str:
    """Read a diff fixture WITHOUT universal-newline translation.

    `Path.read_text()` translates CRLF→LF and would silently erase the CR
    markers the F-14 pure-line-ending check depends on (verified: a CRLF-bomb
    read via read_text() is NOT flagged). Real `git diff` output is captured as
    bytes, so decoding raw bytes is also the production-accurate path.
    """
    return (FIXTURE_DIFFS_DIR / name).read_bytes().decode("utf-8")


@pytest.fixture
def fake_client() -> FakeGitHubClient:
    return FakeGitHubClient()


@pytest.fixture
def gateway(fake_client: FakeGitHubClient, db: Database, budget: BudgetTracker,
            config: Config) -> GitHubGateway:
    return GitHubGateway(
        fake_client, db, budget, config,
        agent_login=AGENT_LOGIN, fork_owner=FORK_OWNER,
    )
