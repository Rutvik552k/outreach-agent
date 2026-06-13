from __future__ import annotations

from pathlib import Path

import pytest

from outreach_agent.config import Config
from outreach_agent.errors import LlmBudgetError, LlmUnavailableError
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.persistence import Database
from outreach_agent.prep import (
    FakeGitRunner,
    build_pr_text,
    prepare_contribution,
    slugify,
)
from outreach_agent.sandbox import FakeSandboxRunner, SandboxResult, Verdict
from outreach_agent.state_machine import ContributionStore, State

GOOD_DIFF = """diff --git a/src/lib.py b/src/lib.py
--- a/src/lib.py
+++ b/src/lib.py
@@ -1,2 +1,3 @@
 def parse(x):
-    return x.split(',')
+    if not x:
+        return []
"""

WORKFLOW_DIFF = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1,2 @@
 on: push
+  evil: true
"""


@pytest.fixture
def store(db: Database) -> ContributionStore:
    return ContributionStore(db)


def _make_contribution(store: ContributionStore) -> str:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    store.transition(cid, State.SCORED)
    store.transition(cid, State.POLICY_CLEARED)
    return cid


def _prep_kwargs(db: Database, store: ContributionStore, config: Config,
                 tmp_path: Path, *, llm: LLMGateway,
                 sandbox: FakeSandboxRunner, git: FakeGitRunner,
                 cid: str) -> dict:
    return dict(
        db=db, store=store, llm=llm, sandbox=sandbox, git=git, config=config,
        contribution_id=cid,
        fork_clone_url="https://github.com/rutvik/some-lib.git",
        issue_title="Crash when parsing empty input",
        issue_body="parse('') raises IndexError",
        issue_number=12,
        issue_url="https://github.com/acme/some-lib/issues/12",
        stack="python",
        work_root=tmp_path / "work",
    )


def test_slugify_and_branch_naming() -> None:
    assert slugify("Crash when parsing: empty input!") == "crash-when-parsing-empty-input"


# -- M-3 (audit step 6): explicit git argument-injection guards ----------------


def test_clone_uses_end_of_options_separator(db: Database, store: ContributionStore,
                                             config: Config, tmp_path: Path) -> None:
    """M-3: positional clone args follow `--` so a user-derived value can
    never be parsed as a git flag (verified locally that git accepts it)."""
    cid = _make_contribution(store)
    llm = LLMGateway(FakeLLMClient([GOOD_DIFF, "t\nd"]), db, config)
    git = FakeGitRunner({"rev-parse": "abc\n", "diff": GOOD_DIFF})
    prepare_contribution(
        **_prep_kwargs(db, store, config, tmp_path, llm=llm,
                       sandbox=FakeSandboxRunner(), git=git, cid=cid))
    clone = next(args for args, _ in git.calls if args[0] == "clone")
    sep = clone.index("--")
    assert clone[sep + 1].startswith("https://github.com/")  # url is positional


@pytest.mark.parametrize("evil_url", [
    "--upload-pack=evil https://github.com/rutvik/some-lib.git",
    "ext::sh -c whoami",
    "https://github.com/rutvik/some-lib.git --config=x",
    "file:///c/secrets",
])
def test_malformed_clone_url_refused_before_git_runs(
        db: Database, store: ContributionStore, config: Config,
        tmp_path: Path, evil_url: str) -> None:
    """M-3: the clone URL must match the pinned https://github.com/<o>/<r>
    shape; anything else is refused before git is ever invoked."""
    cid = _make_contribution(store)
    llm = LLMGateway(FakeLLMClient([GOOD_DIFF]), db, config)
    git = FakeGitRunner({"rev-parse": "abc\n", "diff": GOOD_DIFF})
    kwargs = _prep_kwargs(db, store, config, tmp_path, llm=llm,
                          sandbox=FakeSandboxRunner(), git=git, cid=cid)
    kwargs["fork_clone_url"] = evil_url
    result = prepare_contribution(**kwargs)
    assert not any(args[0] == "clone" for args, _ in git.calls)
    assert result.state != State.CI_GREEN


def test_pr_text_always_contains_ai_disclosure() -> None:
    text = build_pr_text(title="t", description_md="d",
                         issue_url="https://x/1", model="claude-opus-4-8")
    assert "AI-assistance disclosure" in text.body_md
    assert "https://x/1" in text.body_md


def test_happy_path_reaches_ci_green(db: Database, store: ContributionStore,
                                     config: Config, tmp_path: Path) -> None:
    cid = _make_contribution(store)
    llm = LLMGateway(FakeLLMClient([GOOD_DIFF, "Fix empty-input crash\nHandles ''."]),
                     db, config)
    git = FakeGitRunner({"rev-parse": "abc123\n", "diff": GOOD_DIFF})
    sandbox = FakeSandboxRunner()
    result = prepare_contribution(
        **_prep_kwargs(db, store, config, tmp_path, llm=llm, sandbox=sandbox,
                       git=git, cid=cid))
    assert result.state == State.CI_GREEN
    assert result.prepared is not None
    assert result.prepared.branch == "agent/12-crash-when-parsing-empty-input"
    assert "AI-assistance disclosure" in result.prepared.pr_text.body_md
    assert store.get_state(cid) == State.CI_GREEN
    # F-14 pinned clone config
    clone = next(args for args, _ in git.calls if args[0] == "clone")
    assert "core.autocrlf=false" in clone and "core.longpaths=true" in clone
    # sandbox executed via C8 fake, never bare host
    assert sandbox.calls and sandbox.calls[0].stack == "python"


def test_workflow_touch_is_terminal_skip(db: Database, store: ContributionStore,
                                         config: Config, tmp_path: Path) -> None:
    """V3/FM11: diff creating/updating .github/workflows/** → terminal skip."""
    cid = _make_contribution(store)
    llm = LLMGateway(FakeLLMClient([WORKFLOW_DIFF]), db, config)
    git = FakeGitRunner({"rev-parse": "abc\n", "diff": WORKFLOW_DIFF})
    result = prepare_contribution(
        **_prep_kwargs(db, store, config, tmp_path,
                       llm=llm, sandbox=FakeSandboxRunner(), git=git, cid=cid))
    assert result.state == State.WORKFLOW_FILE_TOUCH_UNSUPPORTED
    assert store.get_state(cid) == State.WORKFLOW_FILE_TOUCH_UNSUPPORTED


def test_sandbox_failure_is_ci_failed(db: Database, store: ContributionStore,
                                      config: Config, tmp_path: Path) -> None:
    cid = _make_contribution(store)
    llm = LLMGateway(FakeLLMClient([GOOD_DIFF]), db, config)
    sandbox = FakeSandboxRunner([SandboxResult(1, 1, 5, "x.log", Verdict.FAILED)])
    result = prepare_contribution(
        **_prep_kwargs(db, store, config, tmp_path, llm=llm, sandbox=sandbox,
                       git=FakeGitRunner({"rev-parse": "abc\n", "diff": GOOD_DIFF}),
                       cid=cid))
    assert result.state == State.CI_FAILED
    assert store.get_state(cid) == State.CI_FAILED


def test_sandbox_timeout_is_sandbox_unfit_not_ci_failed(
        db: Database, store: ContributionStore, config: Config,
        tmp_path: Path) -> None:
    """F-10: environment failure ≠ patch failure."""
    cid = _make_contribution(store)
    llm = LLMGateway(FakeLLMClient([GOOD_DIFF]), db, config)
    sandbox = FakeSandboxRunner([SandboxResult(-1, -1, 900, "x.log", Verdict.TIMEOUT)])
    result = prepare_contribution(
        **_prep_kwargs(db, store, config, tmp_path, llm=llm, sandbox=sandbox,
                       git=FakeGitRunner({"rev-parse": "abc\n", "diff": GOOD_DIFF}),
                       cid=cid))
    assert result.state == State.SANDBOX_UNFIT
    assert store.get_state(cid) == State.SANDBOX_UNFIT


def test_llm_spend_cap_mid_prep_is_llm_blocked(db: Database,
                                               store: ContributionStore,
                                               config: Config,
                                               tmp_path: Path) -> None:
    """FM9/F-13: spend cap → llm-blocked, no partial prepared."""
    import dataclasses

    cid = _make_contribution(store)
    tight = dataclasses.replace(config, llm_monthly_spend_cap_usd=0.0)
    llm = LLMGateway(FakeLLMClient([GOOD_DIFF]), db, tight)
    result = prepare_contribution(
        **_prep_kwargs(db, store, tight, tmp_path, llm=llm,
                       sandbox=FakeSandboxRunner(),
                       git=FakeGitRunner({"rev-parse": "abc\n"}), cid=cid))
    assert result.state == State.LLM_BLOCKED
    assert store.get_state(cid) == State.LLM_BLOCKED
    assert result.prepared is None


def test_llm_unavailable_reverts_to_policy_cleared(db: Database,
                                                   store: ContributionStore,
                                                   config: Config,
                                                   tmp_path: Path) -> None:
    """FM9: outage mid-prep reverts to re-enterable policy-cleared."""
    cid = _make_contribution(store)
    client = FakeLLMClient()
    client.fail_next = LlmUnavailableError("503 after retries")
    llm = LLMGateway(client, db, config)
    result = prepare_contribution(
        **_prep_kwargs(db, store, config, tmp_path, llm=llm,
                       sandbox=FakeSandboxRunner(),
                       git=FakeGitRunner({"rev-parse": "abc\n"}), cid=cid))
    assert result.state == State.POLICY_CLEARED
    assert store.get_state(cid) == State.POLICY_CLEARED  # never transitioned away
