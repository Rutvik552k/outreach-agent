"""Diff invariant checks — contract C3 (V3/FM11, F-14, V5).

Operates on unified diff text (`git diff` output). The lockfile/dependency and
network-call detectors are documented MVP regex heuristics: they are risk-note
*flags* surfaced to the human reviewer (V5), not security boundaries — the C8
sandbox and the human gate are the actual controls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_WORKFLOW_PATH = re.compile(r"^\.github/workflows/.+")

_LOCKFILE_OR_DEP_FILES = re.compile(
    r"(^|/)("
    r"package-lock\.json|yarn\.lock|pnpm-lock\.yaml|npm-shrinkwrap\.json|"
    r"poetry\.lock|Pipfile\.lock|Pipfile|uv\.lock|requirements[^/]*\.txt|"
    r"pyproject\.toml|setup\.py|setup\.cfg|"
    r"Cargo\.lock|Cargo\.toml|"
    r"package\.json|go\.mod|go\.sum|Gemfile|Gemfile\.lock"
    r")$"
)

# MVP heuristic: added lines that introduce HTTP/socket client usage.
_NETWORK_CALL = re.compile(
    r"\b("
    r"requests\.(get|post|put|delete|patch|head|request|Session)|"
    r"urllib\.request|urlopen\s*\(|http\.client|aiohttp\.|httpx\.|"
    r"socket\.(socket|create_connection)|"
    r"fetch\s*\(|axios[.(]|XMLHttpRequest|WebSocket\s*\(|"
    r"reqwest::|hyper::|TcpStream::connect|std::net::"
    r")"
)


@dataclass(frozen=True)
class DiffStat:
    files: int
    insertions: int
    deletions: int

    @property
    def changed_lines(self) -> int:
        return self.insertions + self.deletions


@dataclass(frozen=True)
class DiffChecks:
    touches_workflow_files: bool
    pure_line_ending_changes: bool
    lockfile_or_dependency_changes: bool
    new_network_calls: bool


@dataclass(frozen=True)
class DiffReport:
    stat: DiffStat
    checks: DiffChecks
    workflow_files: tuple[str, ...]
    flagged_dependency_files: tuple[str, ...]
    over_size_cap: bool


@dataclass
class _FileDiff:
    path: str
    minus: list[str]
    plus: list[str]


def _parse_files(diff_text: str) -> list[_FileDiff]:
    files: list[_FileDiff] = []
    current: _FileDiff | None = None
    # Split on \n only: splitlines() would swallow \r\n and erase the CR
    # marker that the F-14 pure-line-ending check depends on.
    for line in diff_text.split("\n"):
        header = _DIFF_HEADER.match(line)
        if header:
            current = _FileDiff(path=header.group("b"), minus=[], plus=[])
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current.plus.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            current.minus.append(line[1:])
    return files


def _is_pure_line_ending_change(file: _FileDiff) -> bool:
    """True when every removed line equals its added counterpart modulo a
    trailing carriage return (F-14: CRLF churn = banned whitespace-PR class)."""
    if not file.minus or not file.plus or len(file.minus) != len(file.plus):
        return False
    any_difference = False
    for old, new in zip(file.minus, file.plus):
        old_n, new_n = old.rstrip("\r"), new.rstrip("\r")
        if old_n != new_n:
            return False
        if old != new:
            any_difference = True
    return any_difference


def run_diff_checks(diff_text: str, *, size_cap_changed_lines: int = 400) -> DiffReport:
    files = _parse_files(diff_text)
    insertions = sum(len(f.plus) for f in files)
    deletions = sum(len(f.minus) for f in files)
    stat = DiffStat(files=len(files), insertions=insertions, deletions=deletions)

    workflow_files = tuple(f.path for f in files if _WORKFLOW_PATH.match(f.path))
    dep_files = tuple(f.path for f in files if _LOCKFILE_OR_DEP_FILES.search(f.path))
    pure_le = bool(files) and any(_is_pure_line_ending_change(f) for f in files)
    network = any(_NETWORK_CALL.search(line) for f in files for line in f.plus)

    checks = DiffChecks(
        touches_workflow_files=bool(workflow_files),
        pure_line_ending_changes=pure_le,
        lockfile_or_dependency_changes=bool(dep_files),
        new_network_calls=network,
    )
    return DiffReport(
        stat=stat,
        checks=checks,
        workflow_files=workflow_files,
        flagged_dependency_files=dep_files,
        over_size_cap=stat.changed_lines > size_cap_changed_lines,
    )
