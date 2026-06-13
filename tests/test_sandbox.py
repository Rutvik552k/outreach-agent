from __future__ import annotations

from pathlib import Path

import pytest

from outreach_agent.errors import SandboxUnavailableError
from outreach_agent.sandbox import (
    DockerSandboxRunner,
    FakeSandboxRunner,
    SandboxResult,
    SandboxSpec,
    Verdict,
)


def _spec(tmp_path: Path) -> SandboxSpec:
    return SandboxSpec(work_dir=tmp_path, stack="python",
                       commands=["pip install -e .", "pytest", "ruff check ."],
                       resolve_commands=["pip install --only-binary :all: pytest"])


def test_fake_runner_returns_canned_results(tmp_path: Path) -> None:
    canned = SandboxResult(test_exit=1, lint_exit=0, wall_seconds=5,
                           log_path="l.log", verdict=Verdict.FAILED)
    fake = FakeSandboxRunner(results=[canned])
    result = fake.run(_spec(tmp_path))
    assert result is canned
    assert fake.calls[0].stack == "python"
    assert fake.run(_spec(tmp_path)).verdict is Verdict.GREEN  # default after queue


def test_spec_default_timeouts(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    assert spec.wall_timeout_s == 900          # Phase X (FM8)
    assert spec.resolve_timeout_s == 300       # Phase R (C8 v2.4)


def test_spec_default_resolve_commands_empty_skips_phase_r(tmp_path: Path) -> None:
    """No resolve vector ⇒ Phase R skipped: no networked container is created
    for a stack with nothing to resolve."""
    spec = SandboxSpec(work_dir=tmp_path, stack="python", commands=["pytest"])
    assert spec.resolve_commands == []


def test_docker_execute_command_hardening(tmp_path: Path) -> None:
    """C8 mandatory hardening flags present in the Phase X command."""
    runner = DockerSandboxRunner(image="outreach-agent-sandbox:latest")
    cmd = runner.build_execute_command(_spec(tmp_path), "sbx-1")
    joined = " ".join(cmd)
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--rm" in cmd
    assert "--pids-limit" in cmd
    assert "no-new-privileges" in joined
    assert f"{tmp_path}:/work:rw" in cmd
    assert "--user" in cmd
    assert cmd[-3:] == ["sh", "-c", "pip install -e . && pytest && ruff check ."]
    # No credential or host-profile paths are ever mounted.
    assert not any("AppData" in part and "/work" not in part
                   for part in cmd if ":" in part and part != tmp_path.drive)


def test_docker_resolve_command_hardening_network_on(tmp_path: Path) -> None:
    """Phase R: identical hardening, network ON, resolve vector threaded."""
    runner = DockerSandboxRunner(image="outreach-agent-sandbox:latest")
    cmd = runner.build_resolve_command(_spec(tmp_path), "sbx-1-r")
    assert "--network=none" not in cmd
    assert "--read-only" in cmd
    assert "--cap-drop=ALL" in cmd
    assert "--rm" in cmd
    assert cmd[-3:] == ["sh", "-c", "pip install --only-binary :all: pytest"]


def test_docker_unavailable_raises_refusal(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """C8 refusal rule: no docker → raise, never bare-host execution."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    runner = DockerSandboxRunner(image="img")
    with pytest.raises(SandboxUnavailableError):
        runner.run(_spec(tmp_path))


def test_docker_daemon_down_raises_refusal(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "C:/docker/docker.exe")

    class _Proc:
        returncode = 1
        stderr = "error during connect: docker daemon is not running"
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    runner = DockerSandboxRunner(image="img")
    with pytest.raises(SandboxUnavailableError):
        runner.run(_spec(tmp_path))
