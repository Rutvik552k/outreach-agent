"""SandboxRunner — contract C8 (V1, F-08, F-10).

All execution of repo-authored code happens ONLY through this interface.
Real implementation: hardened Docker container (WSL2 backend). If Docker is
unavailable, the runner raises — never bare-host execution.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .errors import SandboxUnavailableError


class Verdict(StrEnum):
    GREEN = "green"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ENVIRONMENT_UNFIT = "environment-unfit"


@dataclass(frozen=True)
class SandboxSpec:
    """C8 v2.4 two-phase spec.

    `resolve_commands` (Phase R): dependency *fetch only*, network ON, repo
    code execution structurally OFF (--only-binary / --ignore-scripts /
    cargo fetch). Empty list ⇒ Phase R is skipped entirely (no networked
    container is ever created for a stack with nothing to resolve).
    `commands` (Phase X): build/lint/test, network NONE — all arbitrary code
    runs only here (threat-model AC2 exfiltration control).
    """

    work_dir: Path
    stack: str
    commands: list[str]
    wall_timeout_s: int = 900
    resolve_commands: list[str] = field(default_factory=list)
    resolve_timeout_s: int = 300


@dataclass(frozen=True)
class SandboxResult:
    test_exit: int
    lint_exit: int
    wall_seconds: int
    log_path: str
    verdict: Verdict


class SandboxRunner(ABC):
    @abstractmethod
    def run(self, spec: SandboxSpec) -> SandboxResult: ...


class FakeSandboxRunner(SandboxRunner):
    """Test seam (F-08): returns canned results; records specs it receives."""

    def __init__(self, results: list[SandboxResult] | None = None,
                 default: SandboxResult | None = None) -> None:
        self._results = list(results or [])
        self._default = default or SandboxResult(
            test_exit=0, lint_exit=0, wall_seconds=1,
            log_path="fake.log", verdict=Verdict.GREEN,
        )
        self.calls: list[SandboxSpec] = []

    def run(self, spec: SandboxSpec) -> SandboxResult:
        self.calls.append(spec)
        return self._results.pop(0) if self._results else self._default


# Signatures indicating the *environment* (not the patch) is unfit — F-10.
_ENV_UNFIT_MARKERS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "network is unreachable",
    "connection refused",
    "docker: not found",
    "cannot connect to the docker daemon",
    "permission denied while trying to connect",
    "no space left on device",
)


class DockerSandboxRunner(SandboxRunner):
    """Hardened, disposable Docker execution — C8 v2.4 TWO-PHASE.

    Phase R (resolve): network ON, execution structurally OFF — dependency
    fetch only. Any Phase R failure (incl. sdist-only python deps rejected by
    --only-binary, registry outages, timeout) ⇒ verdict environment-unfit:
    the *environment* could not be assembled; the patch is never blamed.
    Phase X (execute): --network=none, build/lint/test of the now-vendored
    tree. Dependency artifacts persist between phases via the /work mount
    (venv-in-workdir for python, node_modules for nodejs, CARGO_HOME in
    workdir for rust — see prep._SANDBOX_RESOLVE_COMMANDS).

    Hardening mandatory in BOTH phases: non-root user, read-only root FS +
    writable work mount + /tmp tmpfs, --cap-drop=ALL, no-new-privileges,
    cpu/mem/pids limits, per-phase wall-clock timeout kill, --rm disposable
    container. No credential paths are ever mounted."""

    def __init__(
        self,
        *,
        image: str,
        cpus: str = "2",
        memory: str = "2g",
        pids_limit: int = 256,
        user: str = "1000:1000",
        docker_bin: str = "docker",
        log_dir: Path | None = None,
    ) -> None:
        self.image = image
        self.cpus = cpus
        self.memory = memory
        self.pids_limit = pids_limit
        self.user = user
        self.docker_bin = docker_bin
        self.log_dir = log_dir or Path.cwd() / "sandbox-logs"

    def _assert_docker_available(self) -> None:
        if shutil.which(self.docker_bin) is None:
            raise SandboxUnavailableError(
                "docker CLI not found on PATH. Docker Desktop (WSL2 backend) is an "
                "MVP host prerequisite (ADR §3); the agent refuses bare-host execution."
            )
        probe = subprocess.run(
            [self.docker_bin, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            raise SandboxUnavailableError(
                "`docker info` failed — daemon not running or virtualization "
                f"unavailable: {probe.stderr.strip()[:300]}. Refusing to prepare "
                "contributions (C8 refusal rule, never bare-host)."
            )

    def _build_phase_command(
        self, spec: SandboxSpec, container_name: str,
        *, commands: list[str], network_none: bool,
    ) -> list[str]:
        script = " && ".join(commands)
        cmd = [
            self.docker_bin, "run",
            "--rm",
            "--name", container_name,
        ]
        if network_none:
            cmd.append("--network=none")
        cmd += [
            "--user", self.user,
            "--read-only",
            "--tmpfs", "/tmp:rw,size=512m",
            "-v", f"{spec.work_dir}:/work:rw",
            "--workdir", "/work",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", self.cpus,
            "--memory", self.memory,
            "--pids-limit", str(self.pids_limit),
            self.image,
            "sh", "-c", script,
        ]
        return cmd

    def build_resolve_command(self, spec: SandboxSpec, container_name: str) -> list[str]:
        """Phase R: network ON (dependency fetch), execution structurally OFF —
        the command vector itself carries --only-binary/--ignore-scripts/
        cargo-fetch. All other C8 hardening identical to Phase X."""
        return self._build_phase_command(
            spec, container_name, commands=spec.resolve_commands, network_none=False,
        )

    def build_execute_command(self, spec: SandboxSpec, container_name: str) -> list[str]:
        """Phase X: --network=none — the only place arbitrary repo code runs."""
        return self._build_phase_command(
            spec, container_name, commands=spec.commands, network_none=True,
        )

    @staticmethod
    def _append_log(log_path: Path, header: str, body: str) -> None:
        with log_path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n=== {header} ===\n{body}\n")

    def _run_phase(
        self, cmd: list[str], container_name: str, timeout_s: int,
        log_path: Path, phase: str,
    ) -> tuple[int, str, bool]:
        """Run one phase container. Returns (exit_code, combined_output,
        timed_out). On timeout the container is killed (FM8) — disposable."""
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
                # docker output is UTF-8 (image-pull progress can carry bytes
                # invalid in the Windows locale codec) — never decode as cp1252.
                encoding="utf-8", errors="replace",
            )
            self._append_log(
                log_path, f"PHASE {phase}",
                proc.stdout + "\n--- stderr ---\n" + proc.stderr,
            )
            return proc.returncode, proc.stdout + proc.stderr, False
        except subprocess.TimeoutExpired as exc:
            subprocess.run(
                [self.docker_bin, "kill", container_name],
                capture_output=True, timeout=30,
            )

            def _txt(x: bytes | str | None) -> str:
                if x is None:
                    return ""
                return x.decode(errors="replace") if isinstance(x, bytes) else x

            self._append_log(
                log_path, f"PHASE {phase} — TIMEOUT (FM8, {timeout_s}s)",
                _txt(exc.stdout) + "\n--- stderr ---\n" + _txt(exc.stderr),
            )
            return -1, "", True

    def run(self, spec: SandboxSpec) -> SandboxResult:
        self._assert_docker_available()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        base_name = f"outreach-sbx-{int(time.time() * 1000)}"
        log_path = self.log_dir / f"{base_name}.log"
        log_path.write_text("", encoding="utf-8")
        start = time.monotonic()

        # --- Phase R (resolve): network ON, execution OFF. Any failure here —
        # nonzero exit (incl. sdist-only deps rejected by --only-binary),
        # timeout, registry outage — is environment-unfit (F-10): the patch is
        # never penalized for an environment that could not be assembled.
        if spec.resolve_commands:
            r_name = f"{base_name}-r"
            r_cmd = self.build_resolve_command(spec, r_name)
            r_exit, _r_out, r_timed_out = self._run_phase(
                r_cmd, r_name, spec.resolve_timeout_s, log_path, "R (resolve)",
            )
            if r_timed_out or r_exit != 0:
                self._append_log(
                    log_path, "VERDICT",
                    "environment-unfit: Phase R (dependency resolve) failed — "
                    "sdist-only dep, registry unreachable, or resolve timeout. "
                    "Patch quality is NOT implicated (F-10).",
                )
                return SandboxResult(
                    test_exit=r_exit, lint_exit=r_exit,
                    wall_seconds=int(time.monotonic() - start),
                    log_path=str(log_path), verdict=Verdict.ENVIRONMENT_UNFIT,
                )

        # --- Phase X (execute): --network=none; build/lint/test of the
        # now-vendored tree. Verdict classification per existing C8 logic.
        x_name = f"{base_name}-x"
        x_cmd = self.build_execute_command(spec, x_name)
        x_exit, x_out, x_timed_out = self._run_phase(
            x_cmd, x_name, spec.wall_timeout_s, log_path, "X (execute)",
        )
        wall = int(time.monotonic() - start)
        if x_timed_out:
            return SandboxResult(
                test_exit=-1, lint_exit=-1, wall_seconds=wall,
                log_path=str(log_path), verdict=Verdict.TIMEOUT,
            )
        if x_exit != 0 and any(m in x_out.lower() for m in _ENV_UNFIT_MARKERS):
            verdict = Verdict.ENVIRONMENT_UNFIT
        elif x_exit == 0:
            verdict = Verdict.GREEN
        else:
            verdict = Verdict.FAILED
        return SandboxResult(
            test_exit=x_exit, lint_exit=x_exit, wall_seconds=wall,
            log_path=str(log_path), verdict=verdict,
        )
