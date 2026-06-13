"""Mocked-lane validation of the sandbox fixture repos (F-08).

These tests run in the DEFAULT (hermetic) lane — no Docker. They assert the
fixture repos exist, are well-formed, and that the per-stack command vector the
prep pipeline builds is exactly what each fixture is shaped to run. If
a prep command vector (`_SANDBOX_RESOLVE_COMMANDS` / `_SANDBOX_EXECUTE_COMMANDS`)
drifts, these fail loudly so the live-lane fixtures get updated in lockstep.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from outreach_agent.prep import _SANDBOX_EXECUTE_COMMANDS, _SANDBOX_RESOLVE_COMMANDS
from outreach_agent.sandbox import DockerSandboxRunner, SandboxSpec

from conftest import FIXTURE_REPOS_DIR


def test_fixture_repos_directory_exists() -> None:
    assert FIXTURE_REPOS_DIR.is_dir()


def test_python_fixture_is_installable_and_has_a_test() -> None:
    repo = FIXTURE_REPOS_DIR / "python-pass"
    assert (repo / "pyproject.toml").is_file()
    assert (repo / "sbx_python_pass" / "__init__.py").is_file()
    assert (repo / "test_add.py").is_file()


def test_nodejs_fixture_has_package_and_lockfile() -> None:
    repo = FIXTURE_REPOS_DIR / "nodejs-pass"
    pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    assert pkg["scripts"]["test"] == "node --test"
    # npm ci requires a lockfile in sync with package.json name+version.
    lock = json.loads((repo / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["name"] == pkg["name"]
    assert lock["version"] == pkg["version"]
    assert (repo / "test" / "add.test.js").is_file()


def test_rust_fixture_has_cargo_and_unit_test() -> None:
    repo = FIXTURE_REPOS_DIR / "rust-pass"
    assert (repo / "Cargo.toml").is_file()
    src = (repo / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "#[test]" in src


def test_hang_fixture_never_terminates_by_construction() -> None:
    """F-10 fixture: the test body must be an unbounded loop, not a quick fail."""
    body = (FIXTURE_REPOS_DIR / "hang" / "test_hang.py").read_text(encoding="utf-8")
    assert "while True" in body


@pytest.mark.parametrize("stack", ["python", "nodejs", "rust"])
def test_each_pass_fixture_matches_its_stack_command_vector(stack: str) -> None:
    """The fixture is shaped so the prep command vectors run it unmodified.
    We assert the Phase X vector still references the toolchain the fixture
    provides — a guard against the prep vectors drifting out from under the
    live-lane fixtures."""
    joined = " ".join(_SANDBOX_EXECUTE_COMMANDS[stack])
    expected_tool = {"python": "pytest", "nodejs": "npm test", "rust": "cargo test"}[stack]
    assert expected_tool in joined


@pytest.mark.parametrize("stack", ["python", "nodejs", "rust"])
def test_each_stack_has_a_resolve_vector_for_phase_r(stack: str) -> None:
    """C8 v2.4: every stack the prep pipeline supports must declare a Phase R
    resolve vector (the python fixture's pytest dependency is fetched here —
    the live lane proved Phase X alone cannot, log outreach-sbx-1781308126124)."""
    assert _SANDBOX_RESOLVE_COMMANDS[stack], f"{stack} has no Phase R vector"


def test_react_shares_nodejs_command_vectors_so_no_separate_fixture_needed() -> None:
    """Documented decision (fixtures README): react needs no separate fixture
    because its command vectors are byte-identical to nodejs. If this ever
    drifts, a react fixture must be added — this test fails loudly to force that."""
    assert _SANDBOX_EXECUTE_COMMANDS["react"] == _SANDBOX_EXECUTE_COMMANDS["nodejs"]
    assert _SANDBOX_RESOLVE_COMMANDS["react"] == _SANDBOX_RESOLVE_COMMANDS["nodejs"]


def test_build_commands_wrap_python_fixture_vectors_exactly() -> None:
    """Pure string assertion (no Docker): the runner threads the prep python
    vectors into each phase container's `sh -c` script verbatim."""
    runner = DockerSandboxRunner(image="img")
    spec = SandboxSpec(
        work_dir=FIXTURE_REPOS_DIR / "python-pass",
        stack="python",
        commands=_SANDBOX_EXECUTE_COMMANDS["python"],
        resolve_commands=_SANDBOX_RESOLVE_COMMANDS["python"],
    )
    x = runner.build_execute_command(spec, "sbx-fixture-x")
    r = runner.build_resolve_command(spec, "sbx-fixture-r")
    assert x[-3:] == ["sh", "-c", " && ".join(_SANDBOX_EXECUTE_COMMANDS["python"])]
    assert r[-3:] == ["sh", "-c", " && ".join(_SANDBOX_RESOLVE_COMMANDS["python"])]
