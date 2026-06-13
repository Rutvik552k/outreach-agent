"""Edge cases for run_diff_checks (ADR C3, F-14, V3, V5).

The existing test_diff_checks.py covers the main invariants. This file closes
the awkward-input gaps a real `git diff` produces: binary files, mixed
EOL within one file, lockfile RENAMES (not just content edits), CRLF read via
the fixture, and the workflow-path boundary (paths that merely contain the
substring must NOT trip the workflow guard).
"""

from __future__ import annotations

from outreach_agent.diff_checks import run_diff_checks

from conftest import read_diff_fixture


def test_crlf_bomb_fixture_flagged_pure_line_ending() -> None:
    """F-14 via the shared fixture. Read through read_diff_fixture(), which reads
    raw bytes — Path.read_text() would translate CRLF→LF and hide the change."""
    report = run_diff_checks(read_diff_fixture("crlf_bomb.diff"))
    assert report.checks.pure_line_ending_changes
    assert not report.checks.touches_workflow_files


def test_binary_file_diff_does_not_crash_and_counts_no_text_lines() -> None:
    """git emits 'Binary files a/x and b/x differ' with no +/- body. The parser
    must register the file but add zero insertions/deletions and not flag it."""
    diff = (
        "diff --git a/assets/logo.png b/assets/logo.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/assets/logo.png and b/assets/logo.png differ\n"
    )
    report = run_diff_checks(diff)
    assert report.stat.files == 1
    assert report.stat.insertions == 0
    assert report.stat.deletions == 0
    assert not report.checks.pure_line_ending_changes
    assert not report.checks.new_network_calls


def test_mixed_eol_within_one_file_is_not_pure_when_real_change_present() -> None:
    """A file with BOTH a genuine content change and CRLF noise must NOT be
    classified pure-line-ending (it's a real change, not banned whitespace spam)."""
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-import os\r\n"            # CRLF noise line
        "+import os\n"
        "-x = 1\r\n"               # genuine change line (content differs)
        "+x = 2\n"
        " unchanged\n"
    )
    report = run_diff_checks(diff)
    # len(minus) == len(plus) but one pair differs in content → not pure.
    assert not report.checks.pure_line_ending_changes


def test_pure_eol_when_counts_unequal_is_not_pure() -> None:
    """Guard the F-14 detector's len-equality precondition: unequal +/- counts
    can't be a pure line-ending swap."""
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,2 +1,1 @@\n"
        "-line one\r\n"
        "-line two\r\n"
        "+line one\n"
    )
    assert not run_diff_checks(diff).checks.pure_line_ending_changes


def test_lockfile_rename_is_flagged_on_the_new_path() -> None:
    """A rename of a lockfile (git rename header) must still flag the dependency
    risk. The detector keys on the b/ (new) path of the diff header."""
    diff = (
        "diff --git a/old-lock.json b/package-lock.json\n"
        "similarity index 100%\n"
        "rename from old-lock.json\n"
        "rename to package-lock.json\n"
    )
    report = run_diff_checks(diff)
    assert report.checks.lockfile_or_dependency_changes
    assert "package-lock.json" in report.flagged_dependency_files


def test_nested_lockfile_path_is_flagged() -> None:
    """Lockfile in a subdirectory (monorepo) must match — the detector anchors on
    a path segment boundary, not only repo root."""
    diff = (
        "diff --git a/packages/web/yarn.lock b/packages/web/yarn.lock\n"
        "--- a/packages/web/yarn.lock\n"
        "+++ b/packages/web/yarn.lock\n"
        "@@ -1 +1 @@\n"
        '-left-pad@1.0.0\n'
        '+left-pad@1.0.1\n'
    )
    report = run_diff_checks(diff)
    assert report.checks.lockfile_or_dependency_changes
    assert "packages/web/yarn.lock" in report.flagged_dependency_files


def test_path_merely_containing_workflows_substring_is_not_flagged() -> None:
    """Boundary: '.github/workflows' must anchor at path start. A source file
    named docs/workflows.md or src/github_workflows.py must NOT trip V3."""
    diff = (
        "diff --git a/docs/workflows.md b/docs/workflows.md\n"
        "--- a/docs/workflows.md\n"
        "+++ b/docs/workflows.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert not run_diff_checks(diff).checks.touches_workflow_files


def test_workflow_file_in_subpath_is_flagged() -> None:
    diff = (
        "diff --git a/.github/workflows/release.yml b/.github/workflows/release.yml\n"
        "--- a/.github/workflows/release.yml\n"
        "+++ b/.github/workflows/release.yml\n"
        "@@ -1 +1,2 @@\n"
        " name: release\n"
        "+  on: push\n"
    )
    report = run_diff_checks(diff)
    assert report.checks.touches_workflow_files
    assert report.workflow_files == (".github/workflows/release.yml",)


def test_empty_diff_is_safe_all_false() -> None:
    report = run_diff_checks("")
    assert report.stat.files == 0
    assert not report.checks.touches_workflow_files
    assert not report.checks.pure_line_ending_changes
    assert not report.checks.lockfile_or_dependency_changes
    assert not report.checks.new_network_calls
    assert not report.over_size_cap


def test_size_cap_boundary_exact_and_over() -> None:
    """over_size_cap is strict-greater-than the cap, not >=."""
    at_cap = "\n".join(f"+line {i}" for i in range(10))
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,10 @@\n" + at_cap + "\n")
    assert not run_diff_checks(diff, size_cap_changed_lines=10).over_size_cap
    assert run_diff_checks(diff, size_cap_changed_lines=9).over_size_cap
