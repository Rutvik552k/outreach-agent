"""FixGenerator — ADR-002 fix-generation redesign (§3/§5) + security sign-off
conditions C-1…C-8.

A new, prep-facing capability that MUTATES files in `work_dir` (the clone) and
returns nothing; prep captures the change with `git diff` afterwards. The
generic text `LLMClient` protocol is unchanged — this is the separate seam so
the diff-vs-edit difference never leaks into text generation.

Two backends, mirroring `build_llm_client`:

- **Approach B — `ClaudeCodeFixGenerator` (claude-code backend, agentic-in-clone).**
  Invokes the Claude Code CLI with cwd = the clone, a file-edit-only tool set,
  and the hardening flags verified on the LOCAL install (`claude` 2.1.176,
  `claude --help`, 2026-06-12, zero web). The model reads the repo and edits
  files in place. `--safe-mode` is MANDATORY and structurally non-removable
  (sign-off C-1, PROBE-4b: `--setting-sources user` alone leaks user-level
  MCP/LSP; only `--safe-mode` closes them).

- **Approach A — `AnthropicFixGenerator` (anthropic backend, context-injection).**
  Reads the issue body + the repo files most relevant to the issue, asks the
  LLMGateway for a deterministic anchored search/replace edit set, and applies
  it into `work_dir`. Fails closed if any `search` block does not match exactly
  once (no fuzzy matching — the [SMOKE] context-line fragility cannot recur).

Verified `claude --help` flags (v2.1.176, [HELP]):
- `-p, --print` ............... "Print response and exit (useful for pipes)."
- `--output-format json` ...... "json (single result)" (only works with --print)
- `--model <model>` ........... "Model for the current session."
- `--system-prompt <prompt>` .. "System prompt to use for the session"
- `--tools <tools...>` ........ 'specify tool names (e.g. "Bash,Edit,Read").' → Read,Edit,Write
- `--disallowedTools <...>` .... "list of tool names to deny" → Bash WebFetch WebSearch
- `--permission-mode acceptEdits` "Permission mode" choices include "acceptEdits"
- `--safe-mode` ............... "all customizations (CLAUDE.md, skills, plugins,
                                 hooks, MCP servers ...) disabled ... Auth ...
                                 work normally." (C-1 primary containment)
- `--setting-sources user` .... "setting sources to load (user, project, local)."
- `--disable-slash-commands` .. "Disable all skills" (C-7)
- `--no-session-persistence` .. "Disable session persistence" (C-7, --print only)
Deliberately NEVER in the argv (sign-off re-review-void triggers, C-3):
`--add-dir`, `--mcp-config`, `--settings`, `--dangerously-skip-permissions`,
`--allow-dangerously-skip-permissions`, `bypassPermissions`, and any Bash /
WebFetch / WebSearch in the `--tools` allowlist.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from .config import Config
from .errors import (
    FixApplyError,
    LlmBackendError,
    LlmUnavailableError,
)
from .llm_gateway import LLMGateway
from .outbound_safety import loaded_secret_values

# -- C-2 diff-neutral pre-strip deny list (sign-off §3) -----------------------
#
# Removed from the clone cwd BEFORE the Approach-B run, then restored
# diff-neutrally afterwards (see `_strip_agent_config` / `_restore_stripped`).
# These are agent-config / auto-load surfaces an untrusted repo could use to
# steer the agent. `--safe-mode` already suppresses auto-load (PROBE-6); the
# strip is mandatory, correctly-scoped, diff-neutral defence-in-depth.
#
# Two kinds:
#  - exact paths (relative to work_dir): files/dirs at the repo root.
#  - nested glob basenames: `**/CLAUDE.md`, `**/AGENTS.md` anywhere in the tree.
_STRIP_EXACT = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    ".claude",
    ".mcp.json",
    ".cursorrules",
    ".cursor",
    ".windsurfrules",
    Path(".github") / "copilot-instructions.md",
)
_STRIP_NESTED_BASENAMES = ("CLAUDE.md", "AGENTS.md")
# Also catch .github/copilot-instructions*.md variants at the repo root.
_STRIP_GITHUB_COPILOT_GLOB = "copilot-instructions*"


class FixGenerator(Protocol):
    """Prep-facing fix-generation capability (ADR-002 §5).

    MUTATES files in `work_dir`; returns nothing. Prep captures `git diff`
    after. Raises LlmUnavailableError (retriable → policy-cleared), FixApplyError
    (non-retriable → error), or LlmBudgetError (→ llm-blocked) — all already
    handled by the prep state machine.
    """

    def generate_fix(self, *, work_dir: Path, branch: str, issue_title: str,
                     issue_body: str, stack: str) -> None: ...


# -- C-4 cwd-confinement helper ------------------------------------------------


def assert_within(work_dir: Path, candidate: Path) -> Path:
    """Sign-off C-4: structurally confine a path to work_dir. Reject absolute
    paths, `..` traversal, and anything resolving outside the clone — NEVER
    rely on model judgment. Returns the resolved, confined absolute path."""
    work_root = work_dir.resolve()
    target = (work_dir / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        target.relative_to(work_root)
    except ValueError as exc:
        raise FixApplyError(
            f"refusing edit to {candidate!s}: resolves outside the clone "
            f"({target} not under {work_root}) — cwd-confinement (C-4)"
        ) from exc
    return target


# -- C-6 no-host-secrets-in-cwd guard -----------------------------------------


def assert_no_secrets_in_tree(work_dir: Path) -> None:
    """Sign-off C-6: no host secret value may be resolvable from the clone cwd.
    Scans the work_dir tree against the in-process loaded-credential registry
    (the same registry the outbound guard uses). Fails closed if any registered
    secret value appears in a file under work_dir. The error never echoes the
    value itself."""
    secret_values = [v for v in loaded_secret_values() if v]
    if not secret_values:
        return
    for path in work_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for value in secret_values:
            if value in text:
                raise FixApplyError(
                    f"refusing Approach-B run: a loaded host credential value is "
                    f"present under the clone cwd ({path.relative_to(work_dir)!s}) "
                    "— no host secrets in the work_dir (C-6)"
                )


# -- C-2 diff-neutral strip / restore -----------------------------------------


def _strip_targets(work_dir: Path) -> list[Path]:
    """Resolve the deny list to concrete existing paths under work_dir."""
    targets: list[Path] = []
    for rel in _STRIP_EXACT:
        p = work_dir / rel
        if p.exists() or p.is_symlink():
            targets.append(p)
    for base in _STRIP_NESTED_BASENAMES:
        for p in work_dir.rglob(base):
            if p not in targets:
                targets.append(p)
    github_dir = work_dir / ".github"
    if github_dir.is_dir():
        for p in github_dir.glob(_STRIP_GITHUB_COPILOT_GLOB):
            if p.is_file() and p not in targets:
                targets.append(p)
    return targets


def _strip_agent_config(work_dir: Path) -> list[Path]:
    """C-2: remove the agent-config deny list from the clone cwd before the B
    run so an untrusted repo cannot steer the agent through them. Returns the
    list of removed paths (relative) so they can be restored diff-neutrally."""
    removed: list[Path] = []
    for target in _strip_targets(work_dir):
        rel = target.relative_to(work_dir)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            try:
                target.unlink()
            except OSError:
                continue
        removed.append(rel)
    return removed


def _restore_stripped(git, work_dir: Path, removed: list[Path]) -> None:
    """C-2 diff-neutrality: the strip must NEVER appear as a deletion in the
    captured `git diff`. For every stripped path that was TRACKED, restore it
    to its committed state with `git checkout -- <path>` (erases the working-
    tree deletion). Untracked stripped paths were never in git, so they cannot
    appear in the diff and need no restore.

    Done AFTER the agent's substantive edits and BEFORE `git diff`, so the
    PR diff contains only the source fix — a repo whose tracked `CLAUDE.md`
    was stripped produces a diff with no CLAUDE.md deletion."""
    if not removed:
        return
    tracked = set(
        line for line in git.run(["ls-files"], cwd=work_dir).splitlines() if line
    )
    # A stripped path may be a FILE (exact match in ls-files) or a DIRECTORY
    # (e.g. `.claude/` — ls-files lists its files, never the dir itself), so a
    # tracked file is restorable when its path equals a removed path OR is
    # nested under a removed directory path. `git checkout` on a dir path also
    # works, but matching the concrete tracked files keeps the set exact.
    removed_posix = [rel.as_posix() for rel in removed]
    to_restore = sorted({
        t for t in tracked
        if t in removed_posix or any(t.startswith(r + "/") for r in removed_posix)
    })
    if to_restore:
        # `--` end-of-options so a path starting with `-` can never be a flag.
        git.run(["checkout", "--", *to_restore], cwd=work_dir)


# -- C-4 post-run confinement assertion ---------------------------------------


def assert_no_escape(git, work_dir: Path, work_root: Path,
                     pre_siblings: set[str]) -> None:
    """C-4: structurally verify the agent did not write outside the clone.

    Two checks:
    1. Inside the clone: `git status --porcelain` lists no path that, resolved,
       escapes work_dir (defence-in-depth; git already scopes to its tree).
    2. Above the clone: compare the work_root sibling set before/after the run;
       any NEW entry beside the clone means an escape write landed and the
       contribution is rejected."""
    status = git.run(["status", "--porcelain"], cwd=work_dir)
    for line in status.splitlines():
        # porcelain: "XY <path>" (rename shows "orig -> new"); take the last token
        raw = line[3:].strip() if len(line) > 3 else ""
        if not raw:
            continue
        path_part = raw.split(" -> ")[-1].strip().strip('"')
        candidate = Path(path_part)
        target = (work_dir / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            target.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise FixApplyError(
                f"refusing contribution: change touches {path_part!r} outside the "
                "clone — cwd-confinement breach (C-4)"
            ) from exc
    post_siblings = {p.name for p in work_root.iterdir()} if work_root.exists() else set()
    new_siblings = post_siblings - pre_siblings
    if new_siblings:
        raise FixApplyError(
            f"refusing contribution: {sorted(new_siblings)!r} appeared beside the "
            "clone during the run — escape write detected (C-4)"
        )


# =============================================================================
# Approach B — ClaudeCodeFixGenerator (agentic-in-clone)
# =============================================================================

_B_FIX_SYSTEM = (
    "You are fixing a single open-source issue in the repository at the current "
    "working directory. Read only the files you need, make the MINIMAL change "
    "that fixes the issue, and add or extend a regression test that fails "
    "without the fix and passes with it. Do not touch CI/workflow files, "
    "lockfiles, or dependency manifests unless the issue explicitly requires "
    "it. Do not add new dependencies or network calls. Edit files in place; do "
    "not print a diff."
)


def build_b_argv(executable: str, *, model: str, system: str) -> list[str]:
    """Sign-off C-1/C-3/C-7: the Approach-B argv builder.

    `--safe-mode` is appended as a CONSTANT (C-1: there is no parameter or
    config toggle that can drop it — `_HARDENING_FLAGS` is a module constant and
    this function takes no flag-controlling argument). The tool set is the
    positive allowlist `Read,Edit,Write` with `Bash WebFetch WebSearch`
    explicitly denied; no `--add-dir`/`--mcp-config`/`--settings`/bypass flags
    are ever emitted. Every flag verified against `claude --help` v2.1.176."""
    return [
        executable, "-p",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", system,
        "--tools", "Read,Edit,Write",
        "--disallowedTools", "Bash", "WebFetch", "WebSearch",
        "--permission-mode", "acceptEdits",
        *_HARDENING_FLAGS,
    ]


# C-1 + C-7: structurally non-removable hardening flags. Kept as a module
# constant spread into EVERY build_b_argv result — there is intentionally no
# code path or config that can omit `--safe-mode` (C-1), `--setting-sources
# user`, `--disable-slash-commands`, or `--no-session-persistence` (C-7).
_HARDENING_FLAGS: tuple[str, ...] = (
    "--safe-mode",
    "--setting-sources", "user",
    "--disable-slash-commands",
    "--no-session-persistence",
)


class ClaudeCodeFixGenerator:
    """Approach B: run Claude Code agentically inside the clone, capture via
    `git diff` (done by prep). Hardening per sign-off C-1…C-8."""

    def __init__(self, executable: str, git, config: Config) -> None:
        self._executable = executable
        self._git = git
        self._config = config

    def _build_prompt(self, *, issue_title: str, issue_body: str) -> str:
        return (
            f"Issue title: {issue_title}\n\n"
            f"Issue body:\n{issue_body or '(no body provided)'}\n\n"
            "Fix this issue minimally and add a regression test."
        )

    def generate_fix(self, *, work_dir: Path, branch: str, issue_title: str,
                     issue_body: str, stack: str) -> None:
        work_root = work_dir.parent
        pre_siblings = {p.name for p in work_root.iterdir()} if work_root.exists() else set()

        # C-6: no host secrets resolvable from the cwd before we launch the CLI.
        assert_no_secrets_in_tree(work_dir)
        # C-2: diff-neutral pre-strip of the agent-config deny list.
        removed = _strip_agent_config(work_dir)

        argv = build_b_argv(
            self._executable, model=self._config.model, system=_B_FIX_SYSTEM,
        )
        prompt = self._build_prompt(issue_title=issue_title, issue_body=issue_body)
        # C-6: the child env carries NO host secret values. Start from the
        # current env (the CLI's own subscription auth via keychain is fine —
        # that is the accepted model round-trip), but strip any var whose VALUE
        # is a registered credential so a leaked secret can never ride env.
        child_env = _sanitized_child_env()
        try:
            proc = subprocess.run(
                argv,
                input=prompt,                       # stdin — never argv
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=self._config.claude_cli_timeout_s,
                cwd=str(work_dir),                  # agentic-in-clone
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            # C-8 / FM9: timeout → retriable. Restore the strip so a re-run is
            # clean, then surface as LlmUnavailableError; prep reverts to
            # policy-cleared and discards the work_dir (no partial prepared).
            _restore_stripped(self._git, work_dir, removed)
            raise LlmUnavailableError(
                f"claude CLI fix-generation timed out after "
                f"{self._config.claude_cli_timeout_s}s (retriable)"
            ) from exc
        except OSError as exc:
            _restore_stripped(self._git, work_dir, removed)
            raise LlmBackendError(
                f"failed to launch claude CLI at {self._executable!r}: {exc}"
            ) from exc

        # Restore the stripped agent-config files diff-neutrally BEFORE any diff
        # capture downstream (C-2): a tracked CLAUDE.md must not show as deleted.
        _restore_stripped(self._git, work_dir, removed)

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise LlmUnavailableError(
                f"claude CLI fix-generation exited {proc.returncode}: "
                f"{detail[0][:300] if detail else '<no output>'} (retriable)"
            )

        # C-4: structurally verify nothing escaped the clone.
        assert_no_escape(self._git, work_dir, work_root, pre_siblings)


def _sanitized_child_env() -> dict[str, str]:
    """C-6: a copy of the process env with any variable whose VALUE is a
    registered host credential removed, so no secret rides into the CLI
    subprocess environment."""
    secret_values = {v for v in loaded_secret_values() if v}
    if not secret_values:
        return dict(os.environ)
    return {k: v for k, v in os.environ.items() if v not in secret_values}


# =============================================================================
# Approach A — AnthropicFixGenerator (context-injection, anchored edits)
# =============================================================================

_A_FIX_SYSTEM = (
    "You are fixing a single open-source issue. You are given the issue and the "
    "contents of the most relevant repository files. Respond with ONLY a JSON "
    "object (no prose, no code fences) of the form:\n"
    '{"edits": [{"path": "<relative/posix/path>", "search": "<exact contiguous '
    'snippet that occurs EXACTLY ONCE in that file>", "replace": "<replacement '
    'snippet>"}], "new_files": [{"path": "<relative/posix/path>", "content": '
    '"<full file body>"}]}\n'
    "Make the minimal change that fixes the issue and add a regression test as "
    "a new file or an edit. The `search` text must be copied byte-for-byte from "
    "the provided file content and must be unique within that file. Do not add "
    "new dependencies or network calls. Do not touch CI/workflow files or "
    "lockfiles unless the issue explicitly requires it."
)

# Bound the context we inject: file count and per-file byte budget (the prompt
# must stay well within the model window; these are MVP heuristics).
_A_MAX_FILES = 8
_A_MAX_FILE_BYTES = 20_000
_A_MAX_WALK_FILES = 400
_A_SOURCE_SUFFIXES = (
    ".py", ".rs", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java",
    ".md", ".txt", ".toml", ".cfg", ".json",
)
_A_SKIP_DIRS = {".git", "node_modules", "target", ".venv", "dist", "build",
                "__pycache__", ".sbx-venv", ".sbx-cargo"}


def _relevant_files(work_dir: Path, issue_title: str, issue_body: str) -> list[Path]:
    """Heuristic file selection (ADR-002 §3, Approach A): prefer files whose
    relative path is named in the issue text; else a bounded walk of source
    files, ranked by how many issue tokens appear in their content."""
    issue_text = f"{issue_title}\n{issue_body}".lower()
    all_files: list[Path] = []
    for path in work_dir.rglob("*"):
        if any(part in _A_SKIP_DIRS for part in path.relative_to(work_dir).parts):
            continue
        if path.is_file() and path.suffix.lower() in _A_SOURCE_SUFFIXES:
            all_files.append(path)
        if len(all_files) >= _A_MAX_WALK_FILES:
            break

    # 1) Files explicitly named (by relative posix path or basename) in the issue.
    named: list[Path] = []
    for path in all_files:
        rel = path.relative_to(work_dir).as_posix().lower()
        if rel in issue_text or path.name.lower() in issue_text:
            named.append(path)
    if named:
        return named[:_A_MAX_FILES]

    # 2) Fallback: rank by issue-token overlap with file content.
    tokens = {t for t in re.split(r"[^a-z0-9_]+", issue_text) if len(t) >= 4}
    scored: list[tuple[int, Path]] = []
    for path in all_files:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        score = sum(1 for t in tokens if t in content)
        if score:
            scored.append((score, path))
    scored.sort(key=lambda sp: sp[0], reverse=True)
    return [p for _, p in scored[:_A_MAX_FILES]]


class AnthropicFixGenerator:
    """Approach A: inject the issue + relevant file contents, ask the gateway
    for an anchored search/replace edit set, apply deterministically into
    work_dir. Fails closed if any `search` does not match exactly once."""

    def __init__(self, llm: LLMGateway, config: Config) -> None:
        self._llm = llm
        self._config = config

    def generate_fix(self, *, work_dir: Path, branch: str, issue_title: str,
                     issue_body: str, stack: str) -> None:
        files = _relevant_files(work_dir, issue_title, issue_body)
        context_parts: list[str] = []
        for path in files:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(work_dir).as_posix()
            context_parts.append(
                f"=== FILE: {rel} ===\n{content[:_A_MAX_FILE_BYTES]}"
            )
        prompt = (
            f"Issue title: {issue_title}\n\n"
            f"Issue body:\n{issue_body or '(no body provided)'}\n\n"
            f"Relevant repository files:\n\n" + "\n\n".join(context_parts)
        )
        # Routes through the gateway: NFR-6 outbound secret guard + spend cap.
        raw = self._llm.generate(
            purpose="fix-generation", system=_A_FIX_SYSTEM, prompt=prompt,
        )
        edit_set = _parse_edit_set(raw)
        _apply_edit_set(work_dir, edit_set)


def _parse_edit_set(raw: str) -> dict:
    """Parse the model's JSON edit set, tolerating a stray code fence. Fail
    closed (FixApplyError) on anything malformed — no fuzzy recovery."""
    text = raw.strip()
    if text.startswith("```"):
        # strip a leading ```json / ``` fence and trailing ```
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise FixApplyError(
            f"fix-generation returned non-JSON edit set (first 200 chars: "
            f"{raw[:200]!r})"
        ) from exc
    if not isinstance(data, dict):
        raise FixApplyError("fix-generation edit set is not a JSON object")
    edits = data.get("edits") or []
    new_files = data.get("new_files") or []
    if not isinstance(edits, list) or not isinstance(new_files, list):
        raise FixApplyError("fix-generation 'edits'/'new_files' must be lists")
    if not edits and not new_files:
        raise FixApplyError(
            "fix-generation produced an empty edit set (no edits, no new files)"
        )
    return {"edits": edits, "new_files": new_files}


def _apply_edit_set(work_dir: Path, edit_set: dict) -> None:
    """Apply anchored search/replace edits + new files deterministically.

    - Each `search` must match EXACTLY ONCE in the current file bytes; 0 or >1
      matches → reject the whole set (FixApplyError). No line-number context.
    - Every path is confined to work_dir (C-4 `assert_within`).
    """
    for edit in edit_set["edits"]:
        if not isinstance(edit, dict):
            raise FixApplyError("each edit must be an object")
        rel = edit.get("path")
        search = edit.get("search")
        replace = edit.get("replace")
        if not rel or search is None or replace is None:
            raise FixApplyError(
                "each edit needs non-empty 'path', 'search', and 'replace'"
            )
        target = assert_within(work_dir, Path(rel))
        if not target.is_file():
            raise FixApplyError(f"edit target {rel!r} does not exist in the clone")
        content = target.read_text(encoding="utf-8")
        count = content.count(search)
        if count != 1:
            raise FixApplyError(
                f"edit 'search' for {rel!r} matched {count} times (need exactly "
                "1) — failing closed, no fuzzy matching (ADR-002 §3.1)"
            )
        target.write_text(content.replace(search, replace, 1), encoding="utf-8",
                          newline="\n")

    for nf in edit_set["new_files"]:
        if not isinstance(nf, dict):
            raise FixApplyError("each new_file must be an object")
        rel = nf.get("path")
        body = nf.get("content")
        if not rel or body is None:
            raise FixApplyError("each new_file needs non-empty 'path' and 'content'")
        target = assert_within(work_dir, Path(rel))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")


# =============================================================================
# Factory — mirrors build_llm_client (ADR-002 §5)
# =============================================================================


class FakeFixGenerator:
    """Test seam (mirrors FakeLLMClient / FakeGitRunner): records every call;
    by default a no-op success so the diff comes from the FakeGitRunner's
    canned `diff` output. `fail_next` injects an exception on the next call
    (e.g. LlmBudgetError / LlmUnavailableError / FixApplyError) to exercise the
    prep failure branches. `writes` lets a test simulate files the generator
    creates in work_dir."""

    def __init__(self, *, writes: dict[str, str] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_next: Exception | None = None
        self.writes = writes or {}

    def generate_fix(self, *, work_dir: Path, branch: str, issue_title: str,
                     issue_body: str, stack: str) -> None:
        self.calls.append(dict(work_dir=work_dir, branch=branch,
                               issue_title=issue_title, issue_body=issue_body,
                               stack=stack))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        for rel, content in self.writes.items():
            target = assert_within(work_dir, Path(rel))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")


def build_fix_generator(config: Config, git, *,
                         llm: LLMGateway) -> FixGenerator:
    """Select the fix generator by the SAME backend switch as build_llm_client:
    claude-code → Approach B (agentic-in-clone), anthropic → Approach A
    (context-injection). `git` is the prep GitRunner seam; `llm` is the gateway
    Approach A uses (and is unused by B)."""
    backend = config.llm_backend
    if backend == "claude-code":
        exe = shutil.which(config.claude_cli_executable)
        if exe is None:
            raise LlmBackendError(
                f"llm_backend=claude-code but {config.claude_cli_executable!r} "
                "was not found on PATH; install Claude Code or set "
                "llm_backend=anthropic"
            )
        return ClaudeCodeFixGenerator(exe, git, config)
    if backend == "anthropic":
        return AnthropicFixGenerator(llm, config)
    raise LlmBackendError(
        f"unknown llm_backend {backend!r}; expected 'claude-code' or 'anthropic'"
    )
