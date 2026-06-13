"""Data contracts C1–C3 (ADR-001 §11). PreparedContribution enforces its
construction invariants — the object cannot exist if they fail."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .diff_checks import DiffChecks, DiffStat
from .errors import DiffInvariantError, WorkflowFileTouchError
from .sandbox import SandboxResult, Verdict

Stack = Literal["python", "rust", "nodejs", "react"]
ContributionType = Literal[
    "bugfix-static-analysis", "test-addition", "issue-triage", "dependency-bump"
]


@dataclass(frozen=True)
class Score:
    repo_health: float
    difficulty_fit: float
    visibility_payoff: float
    attribution_history: float
    total: float


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    repo_full_name: str
    issue_number: int
    issue_url: str
    stack: Stack
    contribution_type: ContributionType
    score: Score
    discovered_at: str


@dataclass(frozen=True)
class PolicyVerdict:
    candidate_id: str
    verdict: Literal["cleared", "blocked"]
    reasons: tuple[str, ...]
    sources_checked: tuple[str, ...]
    checked_at: str
    ttl_expires_at: str  # ignored at publish: always re-checked in the F-05 gate


@dataclass(frozen=True)
class PrText:
    title: str
    body_md: str
    linked_issue: str

    AI_DISCLOSURE_MARKER = "AI-assistance disclosure"

    def __post_init__(self) -> None:
        if self.AI_DISCLOSURE_MARKER.lower() not in self.body_md.lower():
            raise DiffInvariantError(
                "PR body lacks the mandatory AI-assistance disclosure section (NFR-6)"
            )
        if not self.linked_issue:
            raise DiffInvariantError("PR text must link the issue")


@dataclass(frozen=True)
class PreparedContribution:
    """Contract C3. Construction invariants (ADR §11):
    - sandbox green via C8 (test_exit == lint_exit == 0)
    - touches_workflow_files == False (V3)
    - pure_line_ending_changes == False (F-14)
    - diff within size cap unless explicit override (V5)
    """

    contribution_id: str
    branch: str
    base_sha: str
    diff_stat: DiffStat
    diff_checks: DiffChecks
    sandbox_run: SandboxResult
    pr_text: PrText
    risk_notes: tuple[str, ...] = ()
    size_cap_changed_lines: int = 400
    size_cap_override: bool = False

    def __post_init__(self) -> None:
        if self.diff_checks.touches_workflow_files:
            raise WorkflowFileTouchError(
                f"{self.contribution_id}: diff touches .github/workflows/** — "
                "terminal workflow-file-touch-unsupported (V3/FM11); scope is never broadened"
            )
        if self.diff_checks.pure_line_ending_changes:
            raise DiffInvariantError(
                f"{self.contribution_id}: diff contains pure line-ending changes (F-14) — "
                "banned whitespace-PR class"
            )
        if self.sandbox_run.verdict is not Verdict.GREEN or (
            self.sandbox_run.test_exit != 0 or self.sandbox_run.lint_exit != 0
        ):
            raise DiffInvariantError(
                f"{self.contribution_id}: sandbox run not CI-green "
                f"(verdict={self.sandbox_run.verdict}, test_exit={self.sandbox_run.test_exit}, "
                f"lint_exit={self.sandbox_run.lint_exit})"
            )
        if (self.diff_stat.changed_lines > self.size_cap_changed_lines
                and not self.size_cap_override):
            raise DiffInvariantError(
                f"{self.contribution_id}: diff has {self.diff_stat.changed_lines} changed lines "
                f"> approval cap {self.size_cap_changed_lines} (V5); explicit override required"
            )
