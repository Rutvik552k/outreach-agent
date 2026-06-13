"""Regression tests for the four production gaps (live-smoke + I-1 call-site
audit, 2026-06-12).

GAP 1 — transient read timeout: C5 reads wrap githubkit transport errors into
        the typed retriable GitHubReadError with ONE retry for the timeout
        class; cmd_discover skips the candidate, continues the run, and never
        poisons the 7-day policy TTL cache.
GAP 2 — auth login stores github_login via tokens.fetch_login (GET /user).
GAP 3 — cmd_prepare flows ci-green → submit_for_approval → draft-on-fork with
        the FORK's actual default branch as base (non-"main" case).
GAP 4 — cmd_approve_sync executes run_graph_verification for due merged/
        graph-verify contributions with the UPSTREAM default branch.

Mocked CI lane: fakes injected at the same seams production wires (C5 gateway,
NFR-7 LLM factory, prep/sandbox/git constructors). No keyring, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from githubkit.exception import RequestTimeout

from conftest import FORK, FORK_OWNER, UPSTREAM, FakeGitHubClient, make_pull_ref
from outreach_agent import cli, github_gateway, oauth, tokens
from outreach_agent.config import Config
from outreach_agent.contracts import PreparedContribution
from outreach_agent.diff_checks import DiffChecks, DiffStat
from outreach_agent.errors import GitHubReadError, OAuthError
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.persistence import Database
from outreach_agent.prep import FakeGitRunner, PrepResult, build_pr_text
from outreach_agent.sandbox import SandboxResult, Verdict
from outreach_agent.state_machine import ContributionStore, State


def _timeout_exc(url: str = "https://api.github.com/repos/acme/some-lib/contents/CONTRIBUTING.md") -> RequestTimeout:
    """A real githubkit RequestTimeout, as raised by core.py:347-348 when
    httpx.TimeoutException escapes the transport."""
    return RequestTimeout(
        httpx.ReadTimeout("read timed out", request=httpx.Request("GET", url))
    )


@pytest.fixture(autouse=True)
def _zero_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_gateway, "_READ_RETRY_BACKOFF_S", 0.0)


@pytest.fixture
def cli_seams(monkeypatch: pytest.MonkeyPatch, gateway: GitHubGateway,
              db: Database) -> None:
    db.set_meta("github_login", FORK_OWNER)
    monkeypatch.setattr(cli, "_build_gateway", lambda db_, config_: gateway)
    monkeypatch.setattr(
        cli, "_build_llm",
        lambda db_, config_: LLMGateway(FakeLLMClient([]), db_, config_),
    )


# ---------------------------------------------------------------------------
# GAP 1 — typed retriable read errors + retry-once + discover continues
# ---------------------------------------------------------------------------


def test_read_timeout_retried_once_then_succeeds(
        gateway: GitHubGateway, fake_client: FakeGitHubClient) -> None:
    """One RequestTimeout → one short-backoff retry → the read succeeds.
    Reads are idempotent; exactly one retry, never a storm."""
    fake_client.repo_files["acme/some-lib/CONTRIBUTING.md"] = "welcome"
    fake_client.fail_next = _timeout_exc()  # single-shot failure

    content = gateway.get_repo_file("acme", "some-lib", "CONTRIBUTING.md")

    assert content == "welcome"
    calls = [c for c in fake_client.calls if c[0] == "get_repo_file"]
    assert len(calls) == 2  # initial + exactly one retry


def test_read_timeout_twice_raises_typed_retriable_error(
        gateway: GitHubGateway, fake_client: FakeGitHubClient,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent timeout → GitHubReadError (retriable=True), never a raw
    githubkit exception, and never more than two attempts."""
    attempts: list[str] = []

    def _always_timeout(owner: str, repo: str, path: str) -> str:
        attempts.append(path)
        raise _timeout_exc()

    monkeypatch.setattr(fake_client, "get_repo_file", _always_timeout)

    with pytest.raises(GitHubReadError) as excinfo:
        gateway.get_repo_file("acme", "some-lib", "CONTRIBUTING.md")

    assert excinfo.value.problem.retriable is True
    assert "after one retry" in excinfo.value.problem.detail
    assert len(attempts) == 2


def test_discover_skips_failed_candidate_and_does_not_poison_ttl_cache(
        cli_seams: None, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        db: Database, config: Config, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Live-smoke regression: one slow CONTRIBUTING.md fetch killed the whole
    discover run after the first candidate. Now: that candidate is reported
    blocked with reason preflight-read-failed (retriable), the loop CONTINUES,
    and the transient verdict is NOT written to the 7-day policy_verdicts
    cache (a later run re-evaluates it fresh)."""
    fake_client.search_results = [
        {"repository_url": "https://api.github.com/repos/flaky/repo-a",
         "number": 1, "title": "fix crash on empty input",
         "html_url": "https://github.com/flaky/repo-a/issues/1",
         "labels": [{"name": "good first issue"}], "comments": 3},
        {"repository_url": "https://api.github.com/repos/healthy/repo-b",
         "number": 2, "title": "fix error in parser",
         "html_url": "https://github.com/healthy/repo-b/issues/2",
         "labels": [{"name": "good first issue"}], "comments": 3},
    ]

    original = fake_client.get_repo_file

    def _flaky(owner: str, repo: str, path: str):
        if owner == "flaky":
            raise _timeout_exc()  # persistent: survives the single retry
        return original(owner, repo, path)

    monkeypatch.setattr(fake_client, "get_repo_file", _flaky)

    rc = cli.cmd_discover(db, config)

    assert rc == 0
    out = capsys.readouterr().out
    assert "preflight-read-failed (retriable)" in out
    assert "healthy/repo-b#2" in out  # the loop continued past the failure
    assert "1 policy-cleared" in out

    cached = {r["candidate_id"]: r["verdict"] for r in db.conn.execute(
        "SELECT candidate_id, verdict FROM policy_verdicts").fetchall()}
    repo_by_cand = {r["candidate_id"]: r["repo_full_name"] for r in db.conn.execute(
        "SELECT candidate_id, repo_full_name FROM candidates").fetchall()}
    cached_repos = {repo_by_cand[cid] for cid in cached}
    assert "flaky/repo-a" not in cached_repos      # no TTL-cache poisoning
    assert cached_repos == {"healthy/repo-b"}
    assert set(cached.values()) == {"cleared"}
    # the skip is auditable
    audit = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint=?",
        ("policy:preflight-read-failed",)).fetchall()
    assert len(audit) == 1 and "flaky/repo-a" in audit[0]["outcome_json"]


# ---------------------------------------------------------------------------
# GAP 2 — auth login resolves and stores github_login
# ---------------------------------------------------------------------------


def test_fetch_login_uses_bearer_get_user(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float):
        seen.update(url=url, headers=headers, timeout=timeout)
        return httpx.Response(200, json={"login": "octocat"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)

    assert tokens.fetch_login("tok-123") == "octocat"
    assert seen["url"] == "https://api.github.com/user"
    assert seen["headers"]["Authorization"] == "Bearer tok-123"
    assert seen["timeout"] == 30.0


@pytest.mark.parametrize("status,payload", [(401, {}), (200, {})])
def test_fetch_login_failure_is_typed(monkeypatch: pytest.MonkeyPatch,
                                      status: int, payload: dict) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        status, json=payload, request=httpx.Request("GET", url)))
    with pytest.raises(OAuthError):
        tokens.fetch_login("tok-123")


def test_auth_login_stores_login_meta_and_audit_overwriting_bootstrap(
        db: Database, config: Config, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """After a successful login flow, github_login is resolved from the minted
    token, upserted over the manual bootstrap row, audited in the same
    transaction, and printed."""
    db.set_meta("github_login", "manual-bootstrap")  # live-DB precondition

    class _FakeTokens:
        stored: list[str] = []

        def oauth_client_id(self) -> str:
            return "cid"

        def oauth_client_secret(self) -> str:
            return "csec"

        def store_github_token(self, token: str) -> None:
            self.stored.append(token)

    def _fake_login_flow(*, config, client_id, exchange, open_browser,
                         store_token) -> None:
        store_token("tok-live-123")

    monkeypatch.setattr(tokens, "KeyringTokenSource", _FakeTokens)
    monkeypatch.setattr(oauth, "run_login_flow", _fake_login_flow)
    monkeypatch.setattr(
        tokens, "fetch_login",
        lambda token, **kw: "octocat" if token == "tok-live-123" else "WRONG")

    rc = cli.cmd_auth_login(db, config)

    assert rc == 0
    assert db.get_meta("github_login") == "octocat"  # bootstrap overwritten
    assert _FakeTokens.stored == ["tok-live-123"]
    audit = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint='cli:auth-login'"
    ).fetchall()
    assert len(audit) == 1 and "octocat" in audit[0]["outcome_json"]
    assert "'octocat'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# GAP 3 — prepare flows to draft-on-fork with the fork's default branch
# ---------------------------------------------------------------------------


def _seed_cleared_candidate(db: Database, *, repo_full_name: str) -> str:
    candidate_id = "cand-prep-1"
    with db.transaction():
        db.conn.execute(
            "INSERT INTO candidates(candidate_id, repo_full_name, issue_number,"
            " issue_url, stack, contribution_type, score_json, discovered_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (candidate_id, repo_full_name, 12,
             f"https://github.com/{repo_full_name}/issues/12", "python",
             "bugfix-static-analysis", json.dumps({"total": 0.9}),
             "2026-06-12T00:00:00+00:00"),
        )
        db.conn.execute(
            "INSERT INTO policy_verdicts(candidate_id, verdict, reasons_json,"
            " sources_checked_json, checked_at, ttl_expires_at)"
            " VALUES(?,?,?,?,?,?)",
            (candidate_id, "cleared", "[]", "[]",
             "2026-06-12T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
        )
    return candidate_id


def _prepared(cid: str, branch: str = "agent/12-fix") -> PreparedContribution:
    return PreparedContribution(
        contribution_id=cid, branch=branch, base_sha="abc123",
        diff_stat=DiffStat(files=1, insertions=2, deletions=1),
        diff_checks=DiffChecks(False, False, False, False),
        sandbox_run=SandboxResult(0, 0, 30, "log", Verdict.GREEN),
        pr_text=build_pr_text(
            title="Fix crash", description_md="Handles ''.",
            issue_url=f"https://github.com/{UPSTREAM}/issues/12",
            model="claude-opus-4-8"),
    )


def test_prepare_ends_at_draft_on_fork_with_fork_default_branch(
        cli_seams: None, gateway: GitHubGateway, fake_client: FakeGitHubClient,
        db: Database, config: Config, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """GAP-3 end-to-end at the cli seam: ci-green prepare → branch pushed once
    → intra-fork draft PR based on the FORK's actual default branch ('trunk',
    the non-"main" case) → state draft-on-fork with the PR number persisted
    and printed."""
    from outreach_agent import prep, sandbox

    _seed_cleared_candidate(db, repo_full_name=UPSTREAM)
    # ATTRIBUTION: cmd_prepare resolves the commit-author email from this
    # config_meta key; the noreply entry is preferred for graph credit.
    db.set_meta("user_emails", "plain@example.com,77+rutvik@users.noreply.github.com")
    fake_client.default_branches[FORK] = "trunk"
    fake_client.pull_response = make_pull_ref(
        number=77, state="open", draft=True,
        base_repo_full_name=FORK, head_repo_full_name=FORK,
    )
    # rev-list returns ≥1 so submit_for_approval's empty-commit guard passes.
    fake_git = FakeGitRunner({"rev-list": "1\n"})
    # cli.py now constructs SystemGitRunner(token_provider=...) for NFR-3
    # github.com push auth; the stub accepts and ignores it (FakeGitRunner
    # needs no token — it records calls without shelling out).
    monkeypatch.setattr(prep, "SystemGitRunner", lambda **_: fake_git)
    monkeypatch.setattr(sandbox, "DockerSandboxRunner",
                        lambda **kw: object())  # never used by the fake prep

    def _fake_prepare(*, db, store, llm, sandbox, git, config,
                      contribution_id, **kw) -> PrepResult:
        prepared = _prepared(contribution_id)
        store.transition(contribution_id, State.PREPARED, reason="test",
                         fields={"branch": prepared.branch,
                                 "base_sha": prepared.base_sha})
        store.transition(contribution_id, State.CI_GREEN, reason="test")
        return PrepResult(State.CI_GREEN, prepared, "CI-green")

    monkeypatch.setattr(prep, "prepare_contribution", _fake_prepare)

    rc = cli.cmd_prepare(db, config)

    assert rc == 0
    # exactly ONE push, owned by submit_for_approval (no double-push)
    pushes = [c for c in fake_git.calls if c[0][0] == "push"]
    assert len(pushes) == 1
    assert pushes[0][0] == ("push", "origin", "--", "agent/12-fix")
    # COMMIT ran before push, authored with the NOREPLY email (preferred over
    # the plain address) — author email decides graph credit (ADR-001 §2[5]).
    seq = [c[0] for c in fake_git.calls]
    add_i = next(i for i, a in enumerate(seq) if a and a[0] == "add")
    commit_i = next(i for i, a in enumerate(seq) if "commit" in a)
    push_i = next(i for i, a in enumerate(seq) if a and a[0] == "push")
    assert add_i < commit_i < push_i
    assert "user.email=77+rutvik@users.noreply.github.com" in seq[commit_i]
    # commit subject derived from the generated PR title
    assert seq[commit_i][seq[commit_i].index("-m") + 1] == "Fix crash"
    # draft PR created on the FORK with its real default branch as base
    fork_pulls = [p for p in fake_client.created_pulls
                  if f"{p['owner']}/{p['repo']}" == FORK]
    assert len(fork_pulls) == 1
    assert fork_pulls[0]["base"] == "trunk"  # not the "main" assumption
    assert fork_pulls[0]["head"] == "agent/12-fix"
    assert fork_pulls[0]["draft"] is True
    # default branch resolved on the FORK, not the upstream
    lookups = [c for c in fake_client.calls if c[0] == "get_repo_default_branch"]
    assert [(c[1][0], c[1][1]) for c in lookups] == [tuple(FORK.split("/", 1))]
    # end state persisted + printed
    row = db.conn.execute(
        "SELECT state, fork_draft_pr_number FROM contributions").fetchone()
    assert row["state"] == State.DRAFT_ON_FORK.value
    assert row["fork_draft_pr_number"] == 77
    out = capsys.readouterr().out
    assert "draft-on-fork: PR #77" in out and "trunk" in out


# ---------------------------------------------------------------------------
# GAP 4 — graph-verify executes after due time with upstream default branch
# ---------------------------------------------------------------------------


def _seed_graph_verify(db: Database, *, state: str, merged_hours_ago: float,
                       pr_number: int = 991) -> str:
    store = ContributionStore(db)
    cid = store.create(candidate_id=None, repo_full_name=UPSTREAM)
    for s in (State.SCORED, State.POLICY_CLEARED, State.PREPARED,
              State.CI_GREEN, State.DRAFT_ON_FORK, State.APPROVED,
              State.UPSTREAM_OPEN, State.MERGED):
        store.transition(cid, s, reason="test seed")
    if state == "graph-verify":
        store.transition(cid, State.GRAPH_VERIFY, reason="test seed")
    merged_at = datetime.now(timezone.utc) - timedelta(hours=merged_hours_ago)
    with db.transaction():
        db.conn.execute(
            "UPDATE contributions SET merged_at=?, merge_commit_sha='deadbeef',"
            " upstream_pr_number=? WHERE contribution_id=?",
            (merged_at.isoformat(), pr_number, cid))
    return cid


def test_approve_sync_runs_due_graph_verification_with_upstream_default_branch(
        cli_seams: None, fake_client: FakeGitHubClient, db: Database,
        config: Config) -> None:
    """A graph-verify contribution whose ≥24h verify-after has passed is
    verified against the UPSTREAM repo's actual default branch ('develop')
    and lands graph-credited."""
    cid = _seed_graph_verify(db, state="graph-verify", merged_hours_ago=30)
    db.set_meta("user_emails", "Me@Example.com")
    fake_client.default_branches[UPSTREAM] = "develop"
    commit = {"sha": "deadbeef",
              "commit": {"author": {"email": "me@example.com"},
                         "message": "Fix crash (#991)"}}
    fake_client.commits["deadbeef"] = commit
    fake_client.branch_commits = [commit]

    rc = cli.cmd_approve_sync(db, config)

    assert rc == 0
    assert ContributionStore(db).get_state(cid) == State.GRAPH_CREDITED
    # default branch resolved on the UPSTREAM repo and used for the scan
    lookups = [c for c in fake_client.calls if c[0] == "get_repo_default_branch"]
    assert [(c[1][0], c[1][1]) for c in lookups] == [tuple(UPSTREAM.split("/", 1))]
    scans = [c for c in fake_client.calls if c[0] == "list_commits"]
    assert scans and all(c[2]["sha"] == "develop" for c in scans)
    kpi = db.conn.execute(
        "SELECT graph_credit FROM kpi_outcomes WHERE graph_credit IS NOT NULL"
    ).fetchone()
    assert kpi["graph_credit"] == "credited"


def test_approve_sync_graph_verify_not_due_stays_put(
        cli_seams: None, fake_client: FakeGitHubClient, db: Database,
        config: Config) -> None:
    """Verify-after not reached (merged 1h ago, delay 24h) → no commit reads,
    state stays graph-verify."""
    cid = _seed_graph_verify(db, state="graph-verify", merged_hours_ago=1)
    db.set_meta("user_emails", "me@example.com")

    rc = cli.cmd_approve_sync(db, config)

    assert rc == 0
    assert ContributionStore(db).get_state(cid) == State.GRAPH_VERIFY
    assert not any(c[0] in ("get_commit", "list_commits")
                   for c in fake_client.calls)


def test_approve_sync_normalizes_crashed_merged_state(
        cli_seams: None, fake_client: FakeGitHubClient, db: Database,
        config: Config) -> None:
    """A row stuck in 'merged' (crash between the merged→graph-verify
    transitions) is normalized to graph-verify and then verified."""
    cid = _seed_graph_verify(db, state="merged", merged_hours_ago=30)
    db.set_meta("user_emails", "me@example.com")
    commit = {"sha": "deadbeef",
              "commit": {"author": {"email": "me@example.com"},
                         "message": "Fix crash (#991)"}}
    fake_client.commits["deadbeef"] = commit
    fake_client.branch_commits = [commit]

    rc = cli.cmd_approve_sync(db, config)

    assert rc == 0
    assert ContributionStore(db).get_state(cid) == State.GRAPH_CREDITED


def test_approve_sync_graph_verify_skipped_without_user_emails(
        cli_seams: None, fake_client: FakeGitHubClient, db: Database,
        config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    """No configured user_emails → no verdict (a guessed email set could
    mis-record credit); skip is printed and audited, state unchanged."""
    cid = _seed_graph_verify(db, state="graph-verify", merged_hours_ago=30)

    rc = cli.cmd_approve_sync(db, config)

    assert rc == 0
    assert ContributionStore(db).get_state(cid) == State.GRAPH_VERIFY
    assert "user_emails" in capsys.readouterr().out
    audit = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint='graph-verify:skipped'"
    ).fetchall()
    assert len(audit) == 1
