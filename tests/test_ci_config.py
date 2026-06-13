"""H-2 (audit step 6) — executable-enforcement artifacts exist and are sound.

The sign-off's C-1 requires the scanner to be CI-enforced and build-breaking,
and C-5 requires a hash-verified install path. These tests pin the artifacts
that implement both so a refactor cannot silently drop them.

YAML-validity note: this venv does not ship pyyaml, so the full-parse test
skips locally with a reason (string-level structural checks below still run;
GitHub validates workflow YAML at push). If pyyaml is ever added to the dev
extra, the parse test activates automatically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CHECK_PS1 = REPO_ROOT / "scripts" / "check.ps1"
VERIFY_LOCK = REPO_ROOT / "scripts" / "verify_lock.py"


def test_ci_workflow_exists_with_required_gates() -> None:
    text = CI_YML.read_text(encoding="utf-8")
    # C-5: the ONLY install path is hash-verified.
    assert "--require-hashes" in text
    assert "requirements.lock" in text
    # C-1: the scanner is its own explicitly-named job (cannot be deselected
    # silently — pytest exits non-zero on a missing or fully-deselected file).
    assert "c1-structural-incapability" in text
    assert "tests/test_no_client_outside_gateway.py" in text
    # The default lane runs in full.
    assert "python -m pytest -q" in text
    # YAML forbids tabs for indentation.
    assert "\t" not in text


def test_ci_workflow_parses_as_yaml() -> None:
    yaml = pytest.importorskip(
        "yaml",
        reason="pyyaml not in this venv — structural string checks still ran; "
               "GitHub validates the workflow YAML at first push",
    )
    data = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    assert set(data["jobs"]) == {"test", "c1-structural-incapability"}
    for job in data["jobs"].values():
        assert job["runs-on"] == "windows-latest"
        assert any("--require-hashes" in str(s.get("run", "")) for s in job["steps"])


def test_local_pre_push_gate_exists_and_mirrors_ci() -> None:
    ps1 = CHECK_PS1.read_text(encoding="utf-8")
    assert "verify_lock.py" in ps1
    assert "pytest" in ps1
    assert "test_no_client_outside_gateway.py" in ps1
    assert VERIFY_LOCK.exists()


def test_verify_lock_documents_the_offline_hash_gap() -> None:
    """The C-5 offline limitation must stay documented, not hand-waved:
    archive hashes are only verifiable at install time (--require-hashes)."""
    src = VERIFY_LOCK.read_text(encoding="utf-8")
    assert "--require-hashes" in src
    assert "CANNOT VERIFY" in src
