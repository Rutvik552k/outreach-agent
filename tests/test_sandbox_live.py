"""Live-sandbox lane (ADR §12, F-08/F-10) — OPT-IN, marker: `sandbox`.

Deselected by default (pyproject `addopts = -m 'not sandbox'`). Run explicitly:

    pytest -m sandbox

Every test here drives the REAL DockerSandboxRunner against the project-owned
fixture repos in tests/fixtures/repos/. When no Docker daemon is reachable (the
common case on a dev box without Docker Desktop running, or any CI host), the
whole lane SKIPS with a clear message — it never fails and never falls back to
bare-host execution. The skip guard probes `docker info`, NOT just `which
docker`: the docker CLI is frequently on PATH while the daemon is down, which is
exactly the state DockerSandboxRunner._assert_docker_available() treats as
unavailable.

These tests are written to run later, unmodified, once Docker is installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from outreach_agent.prep import _SANDBOX_EXECUTE_COMMANDS, _SANDBOX_RESOLVE_COMMANDS
from outreach_agent.sandbox import DockerSandboxRunner, SandboxSpec, Verdict

from conftest import FIXTURE_REPOS_DIR

pytestmark = pytest.mark.sandbox


def _docker_reason_unavailable() -> str | None:
    """Return a human-readable reason Docker can't be used, or None if it can."""
    if shutil.which("docker") is None:
        return "docker CLI not found on PATH"
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"`docker info` could not be executed: {exc}"
    if probe.returncode != 0:
        return f"`docker info` failed (daemon not running?): {probe.stderr.strip()[:200]}"
    return None


@pytest.fixture(scope="session", autouse=True)
def _require_docker() -> None:
    reason = _docker_reason_unavailable()
    if reason is not None:
        pytest.skip(
            f"live-sandbox lane skipped — Docker unavailable: {reason}. "
            "Install/start Docker Desktop (WSL2) and re-run `pytest -m sandbox`.",
            allow_module_level=True,
        )


# Per-stack image. Defaults assume official toolchain-bearing tags; override via
# OUTREACH_SBX_IMAGE_<STACK> when local tags differ. Only consulted when a daemon
# exists (otherwise the session skip above fires first).
_DEFAULT_IMAGES = {
    "python": "python:3.12-slim",
    "nodejs": "node:20-slim",
    "rust": "rust:1-slim",
}


def _image_for(stack: str) -> str:
    return os.environ.get(f"OUTREACH_SBX_IMAGE_{stack.upper()}", _DEFAULT_IMAGES[stack])


@pytest.mark.parametrize("fixture, stack", [
    ("python-pass", "python"),
    ("nodejs-pass", "nodejs"),
    ("rust-pass", "rust"),
])
def test_fixture_repo_runs_green_in_real_sandbox(fixture: str, stack: str) -> None:
    """C8 v2.4 end-to-end: a passing fixture repo yields verdict green through
    the real two-phase hardened containers (Phase R fetches deps with network
    ON / execution OFF; Phase X runs the suite with --network=none)."""
    runner = DockerSandboxRunner(image=_image_for(stack))
    result = runner.run(SandboxSpec(
        work_dir=FIXTURE_REPOS_DIR / fixture,
        stack=stack,
        resolve_commands=_SANDBOX_RESOLVE_COMMANDS[stack],
        commands=_SANDBOX_EXECUTE_COMMANDS[stack],
        wall_timeout_s=600,
    ))
    assert result.verdict is Verdict.GREEN, f"{fixture}: exit={result.test_exit}"
    assert result.test_exit == 0


def test_hang_fixture_hits_wall_clock_timeout(tmp_path) -> None:
    """F-10/FM8: a non-terminating suite must be killed at the Phase X
    wall-clock timeout and return verdict timeout — never block the pipeline.
    v2.4: Phase R (pytest wheel fetch) must SUCCEED first so the hang fixture
    actually reaches its hang in Phase X (the single-phase design died in pip
    before ever hanging — log outreach-sbx-1781308126124)."""
    runner = DockerSandboxRunner(image=_image_for("python"))
    result = runner.run(SandboxSpec(
        work_dir=FIXTURE_REPOS_DIR / "hang",
        stack="python",
        resolve_commands=_SANDBOX_RESOLVE_COMMANDS["python"],
        commands=_SANDBOX_EXECUTE_COMMANDS["python"],
        wall_timeout_s=20,  # short on purpose; the fixture loops for an hour
        resolve_timeout_s=300,  # config default; fresh resolve over the 9p
                                # mount measured ~249s live before --no-compile
    ))
    assert result.verdict is Verdict.TIMEOUT  # NOT environment-unfit: R passed, X hung
    # wall_seconds spans both phases; Phase X itself was killed at ~20s, so the
    # total stays well under resolve budget + X timeout + kill slack.
    assert result.wall_seconds <= 300 + 20 + 30


def test_network_is_disabled_inside_the_execute_sandbox() -> None:
    """C8 hardening proof (AC2): --network=none on Phase X means outbound
    DNS/HTTP fails where arbitrary code runs. Phase R is intentionally skipped
    (empty resolve vector) so this asserts the EXECUTE container's isolation."""
    runner = DockerSandboxRunner(image=_image_for("python"))
    result = runner.run(SandboxSpec(
        work_dir=FIXTURE_REPOS_DIR / "python-pass",
        stack="python",
        resolve_commands=[],  # skip Phase R: prove Phase X isolation alone
        # python is in the image; opening a socket to a public host must fail
        # because the network namespace is empty (--network=none).
        commands=[
            "python -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)\""
        ],
        wall_timeout_s=60,
    ))
    assert result.verdict in (Verdict.FAILED, Verdict.ENVIRONMENT_UNFIT)
    assert result.test_exit != 0
