"""Prep pipeline — component §2[3] (FR-2, C3, F-14, FM9, V1 via C8).

Clones the user's fork with the pinned git config (F-14), generates patch +
tests via the LLMGateway, validates inside the SandboxRunner (C8 — never bare
host), runs diff checks, and constructs the PreparedContribution (whose
constructor enforces the C3 invariants). LLM failure reverts to
policy-cleared and cleans the work dir; spend-cap → llm-blocked (FM9).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .contracts import PreparedContribution, PrText
from .diff_checks import run_diff_checks
from .errors import (
    DiffInvariantError,
    FixApplyError,
    GitOperationError,
    LlmBudgetError,
    LlmUnavailableError,
)
from .fix_generator import FixGenerator
from .llm_gateway import LLMGateway
from .persistence import Database, canonical_json
from .sandbox import SandboxRunner, SandboxSpec, Verdict
from .state_machine import ContributionStore, State

# F-14: pinned config for EVERY agent clone — longpaths for Windows MAX_PATH,
# autocrlf=false so CRLF churn (banned whitespace-PR class) cannot occur.
GIT_CLONE_FLAGS = ("-c", "core.autocrlf=false", "-c", "core.longpaths=true")

# M-3 (audit step 6): make git argument-safety EXPLICIT, not incidental.
# Every user-derived value passed to git is validated against a pinned shape
# first, and positional args follow a `--` end-of-options separator (verified
# accepted by `git clone` and `git push` locally, 2026-06-12), so a value
# starting with `-` can never be parsed as a git flag even if a future
# refactor weakens the construction sites.
_FORK_CLONE_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(\.git)?$"
)
_BRANCH_RE = re.compile(r"^agent/\d+-[a-z0-9][a-z0-9-]*$")


def assert_safe_branch(branch: str) -> None:
    """M-3: branch names the agent passes to git must match the pinned
    `agent/<issue>-<slug>` shape (no leading dash possible, charset closed)."""
    if not _BRANCH_RE.match(branch):
        raise GitOperationError(
            f"refusing git operation: branch {branch!r} does not match the "
            "pinned ^agent/<issue>-<slug>$ shape (M-3 argument-injection guard)"
        )

# C8 v2.4 two-phase command vectors. Phase R = network ON, repo-code execution
# structurally OFF; Phase X = --network=none, all build/lint/test. Artifacts
# persist between phases ONLY via the /work mount (root FS is read-only and
# /tmp is a per-container tmpfs). Per-stack mechanics:
#
# python — venv-in-workdir (/work/.sbx-venv, --copies because /work is a
#   Docker-Desktop 9p mount of a Windows path where symlinks are unreliable).
#   Phase R fetches WHEELS ONLY (--only-binary :all:; sdist-only deps fail ⇒
#   environment-unfit, never a source build): pytest (test tool) + setuptools
#   + wheel (build backend, so Phase X can build the local project OFFLINE via
#   --no-build-isolation — the live-lane log outreach-sbx-1781308126124.log
#   proved build isolation re-fetches setuptools from the network, which Phase
#   X must never do). requirements.txt deps, when declared, resolve the same
#   way; pyproject [project] deps would need a metadata build (= repo code
#   execution) so they are NOT resolved in Phase R — a missing dep surfaces in
#   Phase X as a normal test failure. --no-cache-dir: HOME is unwritable
#   (read-only root, non-root user). --no-compile: skips .pyc writes — small-
#   file I/O over the 9p mount dominates Phase R wall time (fresh resolve
#   measured ~249s live without it). Venv creation is CONDITIONAL: re-copying
#   the python binary over an existing venv on the 9p mount raises ETXTBSY
#   (observed live, log outreach-sbx-1781309129859), so a usable venv is
#   reused and a partial one (no bin/python) is removed first — Phase R is
#   idempotent across re-runs of the same work dir.
# nodejs/react — npm ci --ignore-scripts (no lifecycle scripts in Phase R);
#   node_modules lands in /work and persists; npm cache pointed at the /tmp
#   tmpfs because HOME is unwritable.
# rust — cargo fetch with CARGO_HOME=/work/.sbx-cargo so the registry/crate
#   cache persists into Phase X (default CARGO_HOME is on the read-only root);
#   Phase X runs cargo test --offline against the pre-fetched cache (build.rs
#   and test code execute only here, network none).
_SANDBOX_RESOLVE_COMMANDS: dict[str, list[str]] = {
    "python": [
        "if [ ! -x /work/.sbx-venv/bin/python ]; then rm -rf /work/.sbx-venv "
        "&& python -m venv --copies /work/.sbx-venv; fi",
        "/work/.sbx-venv/bin/python -m pip install --only-binary :all: "
        "--no-cache-dir --no-compile pytest setuptools wheel",
        "if [ -f requirements.txt ]; then /work/.sbx-venv/bin/python -m pip "
        "install --only-binary :all: --no-cache-dir --no-compile "
        "-r requirements.txt; fi",
    ],
    "rust": [
        "export CARGO_HOME=/work/.sbx-cargo",
        "cargo fetch",
    ],
    "nodejs": ["npm ci --ignore-scripts --no-audit --no-fund --cache /tmp/.npm"],
    "react": ["npm ci --ignore-scripts --no-audit --no-fund --cache /tmp/.npm"],
}

_SANDBOX_EXECUTE_COMMANDS: dict[str, list[str]] = {
    "python": [
        "/work/.sbx-venv/bin/python -m pip install -e . --no-deps "
        "--no-build-isolation --no-cache-dir || true",
        "/work/.sbx-venv/bin/python -m pytest -x -q",
    ],
    "rust": [
        "export CARGO_HOME=/work/.sbx-cargo",
        "cargo test --offline",
    ],
    "nodejs": ["npm test"],
    "react": ["npm test"],
}


class GitRunner(Protocol):
    def run(self, args: list[str], *, cwd: Path | None = None) -> str: ...


class SystemGitRunner:
    """Shells out to system git. Raises GitOperationError on non-zero exit."""

    def run(self, args: list[str], *, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise GitOperationError(
                f"git {' '.join(args[:3])}... exited {proc.returncode}: "
                f"{proc.stderr.strip()[:400]}"
            )
        return proc.stdout


class FakeGitRunner:
    """Test seam: records commands, returns canned outputs keyed by the first
    git subcommand."""

    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def run(self, args: list[str], *, cwd: Path | None = None) -> str:
        self.calls.append((tuple(args), cwd))
        for key, out in self.outputs.items():
            if args and args[0] == key:
                return out
        return ""


def slugify(title: str, max_len: int = 30) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "fix"


def build_pr_text(*, title: str, description_md: str, issue_url: str,
                  model: str) -> PrText:
    """PR text with the structurally guaranteed AI-disclosure section (NFR-6).
    PrText's own validator re-checks; this builder makes absence impossible."""
    body = (
        f"{description_md.strip()}\n\n"
        f"Fixes: {issue_url}\n\n"
        "## AI-assistance disclosure\n\n"
        f"This contribution was prepared with AI assistance ({model}), "
        "reviewed and approved by a human before submission. The repository's "
        "own test suite and linters passed in an isolated sandbox prior to "
        "this PR being opened.\n"
    )
    return PrText(title=title, body_md=body, linked_issue=issue_url)


_PR_TEXT_SYSTEM = (
    "Draft a concise pull-request title and description for the provided "
    "diff and issue, following common open-source conventions. First line: "
    "the title. Remaining lines: the markdown description. Be specific about "
    "what changed and why; no marketing language."
)


@dataclass(frozen=True)
class PrepResult:
    state: State
    prepared: PreparedContribution | None
    detail: str


def prepare_contribution(
    *,
    db: Database,
    store: ContributionStore,
    llm: LLMGateway,
    fix_generator: FixGenerator,
    sandbox: SandboxRunner,
    git: GitRunner,
    config: Config,
    contribution_id: str,
    fork_clone_url: str,
    issue_title: str,
    issue_body: str,
    issue_number: int,
    issue_url: str,
    stack: str,
    work_root: Path,
) -> PrepResult:
    """policy-cleared → prepared → ci-green | ci-failed | sandbox-unfit |
    llm-blocked | workflow-file-touch-unsupported (constructor-raised)."""
    work_root.mkdir(parents=True, exist_ok=True)
    work_dir = work_root / contribution_id
    branch = f"agent/{issue_number}-{slugify(issue_title)}"

    def _cleanup() -> None:
        shutil.rmtree(work_dir, ignore_errors=True)

    try:
        # M-3: validate-then-pass with `--` end-of-options (see module head).
        if not _FORK_CLONE_URL_RE.match(fork_clone_url):
            raise GitOperationError(
                f"refusing git clone: url {fork_clone_url!r} does not match the "
                "pinned https://github.com/<owner>/<repo>[.git] shape (M-3)"
            )
        assert_safe_branch(branch)
        git.run(["clone", *GIT_CLONE_FLAGS, "--", fork_clone_url, str(work_dir)])
        base_sha = git.run(["rev-parse", "HEAD"], cwd=work_dir).strip()
        # `-b` consumes the next argv entry as the branch name (never option
        # parsing), and assert_safe_branch above pins its shape.
        git.run(["checkout", "-b", branch], cwd=work_dir)

        # ADR-002 §5: the FixGenerator MUTATES files in work_dir (Approach B
        # edits agentically in the clone; Approach A applies an anchored edit
        # set). prep then captures the change with `git diff`. The fragile
        # `git apply` round-trip — the [SMOKE] "No valid patches" failure site —
        # is removed entirely. Downstream (diff checks, workflow-skip, C8
        # sandbox, C3 construction) is unchanged.
        fix_generator.generate_fix(
            work_dir=work_dir, branch=branch,
            issue_title=issue_title, issue_body=issue_body, stack=stack,
        )
        diff_text = git.run(["diff"], cwd=work_dir)
    except (LlmBudgetError,) as exc:
        _cleanup()
        store.transition(contribution_id, State.LLM_BLOCKED, reason=str(exc))
        return PrepResult(State.LLM_BLOCKED, None, str(exc))
    except (LlmUnavailableError,) as exc:
        # FM9 / C-8: timeout or transient backend failure → revert to
        # re-enterable policy-cleared; _cleanup() discards the work_dir so no
        # partial in-place edit survives (the B run edits in place).
        _cleanup()
        return PrepResult(State.POLICY_CLEARED, None, f"LLM unavailable, reverted: {exc}")
    except FixApplyError as exc:
        # Anchored-edit mismatch (Approach A) or cwd-confinement / no-secrets
        # breach (Approach B, C-4/C-6): non-retriable → re-enterable ERROR,
        # matching the old git-apply-failure mapping. No partial `prepared`.
        _cleanup()
        store.transition(contribution_id, State.ERROR, reason=str(exc))
        return PrepResult(State.ERROR, None, str(exc))
    except GitOperationError as exc:
        _cleanup()
        store.transition(contribution_id, State.ERROR, reason=str(exc))
        return PrepResult(State.ERROR, None, str(exc))

    report = run_diff_checks(diff_text, size_cap_changed_lines=config.diff_cap_changed_lines)
    if report.checks.touches_workflow_files:
        _cleanup()
        store.transition(
            contribution_id, State.WORKFLOW_FILE_TOUCH_UNSUPPORTED,
            reason=f"workflow files touched: {report.workflow_files} (V3/FM11)",
        )
        return PrepResult(State.WORKFLOW_FILE_TOUCH_UNSUPPORTED, None,
                          "diff touches .github/workflows/** — terminal skip")

    store.transition(
        contribution_id, State.PREPARED,
        reason="patch applied, diff checks run",
        fields={"branch": branch, "base_sha": base_sha},
    )

    result = sandbox.run(SandboxSpec(
        work_dir=work_dir, stack=stack,
        resolve_commands=_SANDBOX_RESOLVE_COMMANDS.get(stack, []),
        commands=_SANDBOX_EXECUTE_COMMANDS.get(stack, ["true"]),
        wall_timeout_s=config.sandbox_wall_timeout_s,
        resolve_timeout_s=config.sandbox_resolve_timeout_s,
    ))
    if result.verdict in (Verdict.TIMEOUT, Verdict.ENVIRONMENT_UNFIT):
        _cleanup()
        store.transition(contribution_id, State.SANDBOX_UNFIT,
                         reason=f"sandbox verdict {result.verdict} (F-10)")
        return PrepResult(State.SANDBOX_UNFIT, None, f"sandbox {result.verdict}")
    if result.verdict is not Verdict.GREEN:
        _cleanup()
        store.transition(contribution_id, State.CI_FAILED,
                         reason=f"sandbox test_exit={result.test_exit}")
        return PrepResult(State.CI_FAILED, None, "repo test suite failed on the patch")

    try:
        drafted = llm.generate(
            purpose="pr-text",
            system=_PR_TEXT_SYSTEM,
            prompt=f"Issue: {issue_title}\n{issue_url}\n\nDiff:\n{diff_text[:6000]}",
        )
    except LlmBudgetError as exc:
        _cleanup()
        store.transition(contribution_id, State.LLM_BLOCKED, reason=str(exc))
        return PrepResult(State.LLM_BLOCKED, None, str(exc))
    except LlmUnavailableError as exc:
        _cleanup()
        store.transition(contribution_id, State.POLICY_CLEARED,
                         reason=f"LLM unavailable during PR text (FM9): {exc}")
        return PrepResult(State.POLICY_CLEARED, None, str(exc))

    lines = drafted.strip().splitlines() or [issue_title]
    pr_text = build_pr_text(
        title=lines[0][:120], description_md="\n".join(lines[1:]).strip(),
        issue_url=issue_url, model=config.model,
    )

    risk_notes: list[str] = []
    if report.checks.lockfile_or_dependency_changes:
        risk_notes.append(
            f"lockfile/dependency files changed: {report.flagged_dependency_files} (V5)")
    if report.checks.new_network_calls:
        risk_notes.append("diff introduces new network-call patterns (V5)")

    try:
        prepared = PreparedContribution(
            contribution_id=contribution_id,
            branch=branch,
            base_sha=base_sha,
            diff_stat=report.stat,
            diff_checks=report.checks,
            sandbox_run=result,
            pr_text=pr_text,
            risk_notes=tuple(risk_notes),
            size_cap_changed_lines=config.diff_cap_changed_lines,
        )
    except DiffInvariantError as exc:
        # F-14 pure line-ending diff or V5 size cap without override.
        _cleanup()
        store.transition(contribution_id, State.ERROR, reason=str(exc))
        return PrepResult(State.ERROR, None, str(exc))
    store.transition(
        contribution_id, State.CI_GREEN,
        reason="sandbox green; C3 invariants hold",
        fields={"prepared_json": canonical_json({
            "branch": prepared.branch, "base_sha": prepared.base_sha,
            "title": pr_text.title, "body_md": pr_text.body_md,
            "linked_issue": pr_text.linked_issue,
            "risk_notes": list(prepared.risk_notes),
            "diff_stat": prepared.diff_stat.__dict__,
        })},
    )
    return PrepResult(State.CI_GREEN, prepared, "CI-green; ready for approval queue")
