from __future__ import annotations

import os
from pathlib import Path

import pytest

from outreach_agent.config import Config
from outreach_agent.errors import LlmBudgetError, LlmUnavailableError
from outreach_agent.fix_generator import FakeFixGenerator
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.persistence import Database
from outreach_agent.prep import (
    FakeGitRunner,
    SystemGitRunner,
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
                 cid: str,
                 fix_generator: FakeFixGenerator | None = None) -> dict:
    return dict(
        db=db, store=store, llm=llm,
        fix_generator=fix_generator or FakeFixGenerator(),
        sandbox=sandbox, git=git, config=config,
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
    """FM9/F-13: spend cap → llm-blocked, no partial prepared. The budget hard
    stop fires at the fix-generation seam (Approach A routes its LLM call
    through the gateway); the FixGenerator surfaces LlmBudgetError, which prep
    maps to llm-blocked."""
    import dataclasses

    cid = _make_contribution(store)
    tight = dataclasses.replace(config, llm_monthly_spend_cap_usd=0.0)
    llm = LLMGateway(FakeLLMClient([GOOD_DIFF]), db, tight)
    fix_gen = FakeFixGenerator()
    fix_gen.fail_next = LlmBudgetError("monthly LLM spend cap reached (F-13)")
    result = prepare_contribution(
        **_prep_kwargs(db, store, tight, tmp_path, llm=llm,
                       sandbox=FakeSandboxRunner(),
                       git=FakeGitRunner({"rev-parse": "abc\n"}), cid=cid,
                       fix_generator=fix_gen))
    assert result.state == State.LLM_BLOCKED
    assert store.get_state(cid) == State.LLM_BLOCKED
    assert result.prepared is None


def test_llm_unavailable_reverts_to_policy_cleared(db: Database,
                                                   store: ContributionStore,
                                                   config: Config,
                                                   tmp_path: Path) -> None:
    """FM9/C-8: outage or timeout at fix-generation reverts to re-enterable
    policy-cleared; the work_dir is discarded (no partial prepared)."""
    cid = _make_contribution(store)
    llm = LLMGateway(FakeLLMClient(), db, config)
    fix_gen = FakeFixGenerator()
    fix_gen.fail_next = LlmUnavailableError("claude CLI timed out (retriable)")
    result = prepare_contribution(
        **_prep_kwargs(db, store, config, tmp_path, llm=llm,
                       sandbox=FakeSandboxRunner(),
                       git=FakeGitRunner({"rev-parse": "abc\n"}), cid=cid,
                       fix_generator=fix_gen))
    assert result.state == State.POLICY_CLEARED
    assert store.get_state(cid) == State.POLICY_CLEARED  # never transitioned away


# -- NFR-3: SystemGitRunner token-leak-safe github.com auth --------------------
#
# The push must authenticate with the keyring OAuth token via an env-var-fed
# inline credential helper. These tests assert the token NEVER appears in argv
# and IS passed via the subprocess env. subprocess.run is patched, so they run
# in the default lane (no network, no real git). The `x-access-token` username
# convention itself was verified empirically against a real push (see
# test_systemgitrunner_real_push_authenticates, marked `local`).

_FAKE_TOKEN = "gho_FAKEtoken0123456789abcdefghijklmnop"


class _CapturedRun:
    """Records the args/env subprocess.run was called with; returns rc=0."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "env": kwargs.get("env")})

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()


def test_systemgitrunner_push_injects_credential_helper_never_token_in_argv(
        monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _CapturedRun()
    monkeypatch.setattr("outreach_agent.prep.subprocess.run", captured)

    runner = SystemGitRunner(token_provider=lambda: _FAKE_TOKEN)
    runner.run(["push", "origin", "--", "agent/12-fix"], cwd=Path("."))

    argv = captured.calls[0]["argv"]
    env = captured.calls[0]["env"]
    flat = " ".join(argv)
    # 1. credential-helper `-c` form is present: empty reset + inline helper.
    assert "credential.helper=" in flat
    assert "x-access-token" in flat  # helper SCRIPT (username), not the token
    assert "$OUTREACH_GIT_TOKEN" in flat  # helper references env by NAME
    # 2. the literal token is NEVER in argv (process-listing safe).
    assert all(_FAKE_TOKEN not in part for part in argv)
    # 3. the token is passed via the SUBPROCESS env only.
    assert env is not None and env["OUTREACH_GIT_TOKEN"] == _FAKE_TOKEN
    # 4. the original positional refspec survives after the injected -c flags.
    assert argv[-2:] == ["--", "agent/12-fix"]


def test_systemgitrunner_token_not_persisted_to_parent_env(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """NFR-3: the token lives in the subprocess env ONLY — os.environ (the
    parent process) is never mutated."""
    import os

    captured = _CapturedRun()
    monkeypatch.setattr("outreach_agent.prep.subprocess.run", captured)
    assert "OUTREACH_GIT_TOKEN" not in os.environ
    SystemGitRunner(token_provider=lambda: _FAKE_TOKEN).run(
        ["push", "origin", "--", "agent/12-fix"])
    assert "OUTREACH_GIT_TOKEN" not in os.environ  # parent env untouched


def test_systemgitrunner_non_push_is_not_authenticated(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Lazy + scoped: clone/rev-parse/diff never call the provider and never get
    the credential-helper flags (public-fork reads are unauthenticated)."""
    captured = _CapturedRun()
    monkeypatch.setattr("outreach_agent.prep.subprocess.run", captured)
    provider_called = False

    def _provider() -> str:
        nonlocal provider_called
        provider_called = True
        return _FAKE_TOKEN

    runner = SystemGitRunner(token_provider=_provider)
    runner.run(["clone", "--", "https://github.com/x/y.git", "/tmp/y"])

    argv = captured.calls[0]["argv"]
    assert provider_called is False
    assert "credential.helper=" not in " ".join(argv)
    assert captured.calls[0]["env"] is None  # default env inherited


def test_systemgitrunner_default_no_provider_is_bare_git(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """FakeGitRunner path / existing behavior: a runner with NO provider runs
    bare git even on push — no credential flags, no env injection."""
    captured = _CapturedRun()
    monkeypatch.setattr("outreach_agent.prep.subprocess.run", captured)
    SystemGitRunner().run(["push", "origin", "--", "agent/12-fix"])
    argv = captured.calls[0]["argv"]
    assert argv == ["git", "push", "origin", "--", "agent/12-fix"]
    assert captured.calls[0]["env"] is None


def test_systemgitrunner_missing_token_on_push_raises_credential_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A push whose provider cannot find the token raises the typed
    CredentialError (not a traceback) BEFORE git is invoked."""
    from outreach_agent.errors import CredentialError

    captured = _CapturedRun()
    monkeypatch.setattr("outreach_agent.prep.subprocess.run", captured)

    def _missing() -> str:
        raise CredentialError("credential 'github_oauth_token' not found; "
                              "run `outreach-agent auth login`")

    runner = SystemGitRunner(token_provider=_missing)
    with pytest.raises(CredentialError):
        runner.run(["push", "origin", "--", "agent/12-fix"])
    assert captured.calls == []  # git never ran


@pytest.mark.local
def test_systemgitrunner_real_push_authenticates(tmp_path: Path) -> None:
    """NFR-3 ground-truth lane (opt-in, `pytest -m local`): proves the
    `username=x-access-token` + env-fed-token helper authenticates a real
    `gho_` OAuth token against GitHub. Clones the throwaway smoke-target,
    pushes an empty-commit branch, asserts success, then deletes the branch.
    NOT in the default lane (hits the network); requires the keyring token.

    Verified manually 2026-06-12: branch agent/9999-credhelper-verify pushed
    and deleted on Rutvik552k/outreach-smoke-target; .git/config token-free.
    """
    import keyring

    token = keyring.get_password("outreach-agent", "github_oauth_token")
    if not token:
        pytest.skip("no keyring github_oauth_token; run `outreach-agent auth login`")

    repo = tmp_path / "smoke"
    runner = SystemGitRunner(token_provider=lambda: token)
    # clone is unauthenticated (public); use bare runner semantics via the same
    # object — clone does not need a token and the runner won't inject one.
    runner.run(["clone", "--",
                "https://github.com/Rutvik552k/outreach-smoke-target.git",
                str(repo)])
    branch = f"agent/9999-credhelper-{os.getpid()}"
    runner.run(["checkout", "-b", branch], cwd=repo)
    runner.run(["commit", "--allow-empty", "-m", "verify: cred-helper (throwaway)"],
               cwd=repo)
    try:
        runner.run(["push", "origin", "--", branch], cwd=repo)  # AUTH path
        cfg = (repo / ".git" / "config").read_text(encoding="utf-8")
        assert token not in cfg  # leak-safety: token never lands on disk
    finally:
        runner.run(["push", "origin", "--delete", branch], cwd=repo)
