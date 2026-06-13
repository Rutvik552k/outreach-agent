from __future__ import annotations

import pytest

from outreach_agent.contracts import PreparedContribution, PrText
from outreach_agent.diff_checks import run_diff_checks
from outreach_agent.errors import DiffInvariantError, WorkflowFileTouchError
from outreach_agent.sandbox import SandboxResult, Verdict

GREEN_RUN = SandboxResult(test_exit=0, lint_exit=0, wall_seconds=42,
                          log_path="x.log", verdict=Verdict.GREEN)
PR_TEXT = PrText(
    title="Fix off-by-one in pagination",
    body_md="Fixes the bug.\n\n## AI-assistance disclosure\nPrepared with AI assistance.",
    linked_issue="acme/some-lib#123",
)

NORMAL_DIFF = """\
diff --git a/src/pager.py b/src/pager.py
index 1111111..2222222 100644
--- a/src/pager.py
+++ b/src/pager.py
@@ -10,3 +10,3 @@
-    return items[:limit + 1]
+    return items[:limit]
"""

WORKFLOW_DIFF = """\
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 1111111..2222222 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,3 @@
 name: CI
+  extra: step
"""

CRLF_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-line one\r\n"
    "-line two\r\n"
    "+line one\n"
    "+line two\n"
)


def _prepared(diff_text: str, **overrides) -> PreparedContribution:
    report = run_diff_checks(diff_text)
    kwargs = dict(
        contribution_id="c1", branch="agent/123-fix", base_sha="abc123",
        diff_stat=report.stat, diff_checks=report.checks,
        sandbox_run=GREEN_RUN, pr_text=PR_TEXT,
    )
    kwargs.update(overrides)
    return PreparedContribution(**kwargs)


def test_normal_diff_passes(  ) -> None:
    prepared = _prepared(NORMAL_DIFF)
    assert prepared.diff_stat.changed_lines == 2
    assert not prepared.diff_checks.touches_workflow_files


def test_workflow_file_diff_terminal_skip() -> None:
    """V3/FM11: created/modified .github/workflows/** → C3 cannot be
    constructed; caller routes to terminal workflow-file-touch-unsupported."""
    report = run_diff_checks(WORKFLOW_DIFF)
    assert report.checks.touches_workflow_files
    assert report.workflow_files == (".github/workflows/ci.yml",)
    with pytest.raises(WorkflowFileTouchError):
        _prepared(WORKFLOW_DIFF)


def test_crlf_fixture_pure_line_ending_diff_rejected() -> None:
    """F-14: a diff that only flips CRLF→LF is the banned whitespace-PR class."""
    report = run_diff_checks(CRLF_DIFF)
    assert report.checks.pure_line_ending_changes
    with pytest.raises(DiffInvariantError):
        _prepared(CRLF_DIFF)


def test_real_change_with_crlf_noise_not_flagged_pure() -> None:
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old logic\r\n"
        "+entirely new logic\n"
    )
    assert not run_diff_checks(diff).checks.pure_line_ending_changes


def test_lockfile_change_flagged_for_risk_notes() -> None:
    diff = (
        "diff --git a/package-lock.json b/package-lock.json\n"
        "--- a/package-lock.json\n"
        "+++ b/package-lock.json\n"
        "@@ -1 +1 @@\n"
        '-  "version": "1.0.0"\n'
        '+  "version": "1.0.1"\n'
    )
    report = run_diff_checks(diff)
    assert report.checks.lockfile_or_dependency_changes
    assert "package-lock.json" in report.flagged_dependency_files


def test_new_network_call_flagged(  ) -> None:
    diff = (
        "diff --git a/src/client.py b/src/client.py\n"
        "--- a/src/client.py\n"
        "+++ b/src/client.py\n"
        "@@ -1 +1,2 @@\n"
        " import requests\n"
        '+    resp = requests.get("https://evil.example/x")\n'
    )
    assert run_diff_checks(diff).checks.new_network_calls


def test_diff_size_cap_enforced_with_override(  ) -> None:
    lines = "\n".join(f"+new line {i}" for i in range(401))
    big_diff = (
        "diff --git a/src/big.py b/src/big.py\n"
        "--- a/src/big.py\n+++ b/src/big.py\n@@ -0,0 +1,401 @@\n" + lines + "\n"
    )
    report = run_diff_checks(big_diff)
    assert report.over_size_cap
    with pytest.raises(DiffInvariantError):
        _prepared(big_diff)
    prepared = _prepared(big_diff, size_cap_override=True)  # V5 explicit override
    assert prepared.size_cap_override


def test_non_green_sandbox_rejected(  ) -> None:
    failed = SandboxResult(test_exit=1, lint_exit=0, wall_seconds=10,
                           log_path="x.log", verdict=Verdict.FAILED)
    with pytest.raises(DiffInvariantError):
        _prepared(NORMAL_DIFF, sandbox_run=failed)


def test_missing_ai_disclosure_rejected(  ) -> None:
    with pytest.raises(DiffInvariantError):
        PrText(title="t", body_md="no disclosure here", linked_issue="a/b#1")
