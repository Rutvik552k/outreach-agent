"""DockerSandboxRunner command-line construction asserts (ADR C8 v2.4, two-phase).

Pure string assertions — NO Docker daemon needed, runs in the default lane.
Pins the C8 mandatory flags for BOTH phases so a regression that weakens
isolation (drops --network=none from Phase X, re-enables capabilities, mounts
something extra, runs as root, leaks a test/build command into Phase R) fails
CI on every commit per ADR §12 mocked-lane obligations.

Phase R (resolve): network ON, execution structurally OFF — asserted here as:
no test/build invocation in the vector, --only-binary/--ignore-scripts/
cargo-fetch present per stack, all non-network hardening identical to Phase X.
Phase X (execute): --network=none MUST be present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from outreach_agent.prep import _SANDBOX_EXECUTE_COMMANDS, _SANDBOX_RESOLVE_COMMANDS
from outreach_agent.sandbox import DockerSandboxRunner, SandboxSpec


def _runner(**kw) -> DockerSandboxRunner:
    return DockerSandboxRunner(image=kw.pop("image", "outreach-agent-sandbox:latest"), **kw)


def _spec(tmp_path: Path, **kw) -> SandboxSpec:
    return SandboxSpec(
        work_dir=kw.pop("work_dir", tmp_path),
        stack=kw.pop("stack", "python"),
        commands=kw.pop("commands", ["pytest -q"]),
        resolve_commands=kw.pop("resolve_commands", ["pip download x"]),
        **kw,
    )


def _both_phase_commands(tmp_path: Path, **kw) -> dict[str, list[str]]:
    runner = _runner()
    spec = _spec(tmp_path, **kw)
    return {
        "R": runner.build_resolve_command(spec, "c1"),
        "X": runner.build_execute_command(spec, "c1"),
    }


# --- network split: THE v2.4 point ---------------------------------------

def test_phase_x_has_network_none(tmp_path: Path) -> None:
    cmd = _runner().build_execute_command(_spec(tmp_path), "c1")
    assert "--network=none" in cmd


def test_phase_r_has_network_on(tmp_path: Path) -> None:
    """Phase R must NOT carry --network=none (dependency fetch needs DNS —
    live-lane log outreach-sbx-1781308126124.log is the failure evidence) and
    must not pick any other explicit network either (default bridge only)."""
    cmd = _runner().build_resolve_command(_spec(tmp_path), "c1")
    assert "--network=none" not in cmd
    assert not any(a.startswith("--network") for a in cmd)


def test_phase_r_runs_resolve_vector_phase_x_runs_execute_vector(tmp_path: Path) -> None:
    spec = _spec(tmp_path, commands=["pytest -q"], resolve_commands=["pip download x"])
    r = _runner().build_resolve_command(spec, "c1")
    x = _runner().build_execute_command(spec, "c1")
    assert r[-1] == "pip download x"
    assert x[-1] == "pytest -q"


# --- hardening identical in BOTH phases -----------------------------------

@pytest.mark.parametrize("phase", ["R", "X"])
def test_cap_drop_all_present(tmp_path: Path, phase: str) -> None:
    assert "--cap-drop=ALL" in _both_phase_commands(tmp_path)[phase]


@pytest.mark.parametrize("phase", ["R", "X"])
def test_read_only_root_fs_present(tmp_path: Path, phase: str) -> None:
    assert "--read-only" in _both_phase_commands(tmp_path)[phase]


@pytest.mark.parametrize("phase", ["R", "X"])
def test_no_new_privileges_present(tmp_path: Path, phase: str) -> None:
    cmd = _both_phase_commands(tmp_path)[phase]
    i = cmd.index("--security-opt")
    assert cmd[i + 1] == "no-new-privileges"


@pytest.mark.parametrize("phase", ["R", "X"])
def test_container_is_disposable_rm_present(tmp_path: Path, phase: str) -> None:
    assert "--rm" in _both_phase_commands(tmp_path)[phase]


@pytest.mark.parametrize("phase", ["R", "X"])
def test_runs_as_non_root_user(tmp_path: Path, phase: str) -> None:
    cmd = _both_phase_commands(tmp_path)[phase]
    user = cmd[cmd.index("--user") + 1]
    assert user == "1000:1000"
    assert not user.startswith("0:")  # never uid 0 (root)
    assert user != "root"


@pytest.mark.parametrize("phase", ["R", "X"])
def test_resource_limits_present_cpu_memory_pids(tmp_path: Path, phase: str) -> None:
    cmd = _both_phase_commands(tmp_path)[phase]
    assert cmd[cmd.index("--cpus") + 1] == "2"
    assert cmd[cmd.index("--memory") + 1] == "2g"
    assert cmd[cmd.index("--pids-limit") + 1] == "256"


@pytest.mark.parametrize("phase", ["R", "X"])
def test_work_dir_mounted_rw_and_workdir_set(tmp_path: Path, phase: str) -> None:
    cmd = _both_phase_commands(tmp_path, work_dir=tmp_path)[phase]
    assert "-v" in cmd
    assert f"{tmp_path}:/work:rw" in cmd
    assert cmd[cmd.index("--workdir") + 1] == "/work"


@pytest.mark.parametrize("phase", ["R", "X"])
def test_tmpfs_is_the_only_writable_surface_besides_work(tmp_path: Path, phase: str) -> None:
    """Read-only root + tmpfs /tmp + the work mount are the writable surfaces.
    No other -v mounts exist (no credential/host-profile paths). The work
    mount is also the ONLY artifact channel between Phase R and Phase X."""
    cmd = _both_phase_commands(tmp_path, work_dir=tmp_path)[phase]
    mount_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert mount_args == [f"{tmp_path}:/work:rw"]  # exactly one bind mount
    assert "--tmpfs" in cmd
    assert cmd[cmd.index("--tmpfs") + 1] == "/tmp:rw,size=512m"


@pytest.mark.parametrize("phase", ["R", "X"])
def test_no_credential_or_secret_paths_are_mounted(phase: str) -> None:
    """C8 / threat-model AC2: secrets live on the host, never in the sandbox —
    in EITHER phase (Phase R has network: a mounted secret there would be the
    exact exfiltration channel AC2 exists to close). Uses a fixed work_dir
    (not pytest's tmp_path, which sits under AppData\\Local\\Temp and would be
    a false positive)."""
    work = Path("/srv/work/contrib-1")
    cmd = _both_phase_commands(Path("/unused"), work_dir=work)[phase]
    bind_targets = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert bind_targets == [f"{work}:/work:rw"], "only the work dir may be bind-mounted"
    forbidden = (".aws", ".ssh", "keyring", "credential",
                 ".netrc", ".docker", ".gitconfig", "AppData")
    for token in forbidden:
        assert token not in bind_targets[0], f"secret-bearing path mounted: {bind_targets[0]}"


# --- Phase R command vectors: execution structurally OFF ------------------

def test_python_resolve_vector_is_wheels_only_no_test_or_build() -> None:
    joined = " ".join(_SANDBOX_RESOLVE_COMMANDS["python"])
    assert "--only-binary :all:" in joined          # sdist build scripts impossible
    assert "python -m pytest" not in joined         # no test invocation
    assert "pip install -e" not in joined           # no local-project build
    assert " . " not in joined and not joined.endswith(" .")  # never installs the repo itself


def test_nodejs_and_react_resolve_vectors_ignore_scripts_no_test() -> None:
    for stack in ("nodejs", "react"):
        joined = " ".join(_SANDBOX_RESOLVE_COMMANDS[stack])
        assert "--ignore-scripts" in joined          # no lifecycle scripts
        assert "npm test" not in joined
        assert "npm run" not in joined


def test_rust_resolve_vector_is_fetch_only() -> None:
    joined = " ".join(_SANDBOX_RESOLVE_COMMANDS["rust"])
    assert "cargo fetch" in joined                   # no build.rs execution
    assert "cargo test" not in joined
    assert "cargo build" not in joined
    assert "cargo run" not in joined


def test_resolve_artifacts_land_in_work_mount_per_stack() -> None:
    """Dependency artifacts must persist between phases — the /work mount is
    the only surviving surface (root FS read-only, /tmp per-container)."""
    assert any("/work/.sbx-venv" in c for c in _SANDBOX_RESOLVE_COMMANDS["python"])
    assert any("CARGO_HOME=/work/.sbx-cargo" in c for c in _SANDBOX_RESOLVE_COMMANDS["rust"])
    # npm ci writes node_modules into the cwd (/work); only its throwaway
    # cache goes to /tmp.
    assert "--cache /tmp/.npm" in " ".join(_SANDBOX_RESOLVE_COMMANDS["nodejs"])


# --- Phase X command vectors: offline execution ---------------------------

def test_python_execute_vector_runs_offline_against_phase_r_venv() -> None:
    joined = " ".join(_SANDBOX_EXECUTE_COMMANDS["python"])
    assert "/work/.sbx-venv/bin/python" in joined
    # build isolation would re-fetch setuptools from the network — the exact
    # live-lane failure (log outreach-sbx-1781308126124.log); must be off.
    assert "--no-build-isolation" in joined
    assert "python -m pytest" in joined


def test_rust_execute_vector_is_offline_with_phase_r_cargo_home() -> None:
    cmds = _SANDBOX_EXECUTE_COMMANDS["rust"]
    assert "export CARGO_HOME=/work/.sbx-cargo" in cmds
    assert any("cargo test --offline" in c for c in cmds)


def test_every_resolve_stack_has_an_execute_vector_and_vice_versa() -> None:
    assert set(_SANDBOX_RESOLVE_COMMANDS) == set(_SANDBOX_EXECUTE_COMMANDS)


# --- construction mechanics ------------------------------------------------

def test_commands_threaded_into_sh_c_verbatim(tmp_path: Path) -> None:
    cmds = ["python -m pip install -e . --no-deps || true", "python -m pytest -x -q"]
    cmd = _runner().build_execute_command(_spec(tmp_path, commands=cmds), "c1")
    assert cmd[-3:] == ["sh", "-c", " && ".join(cmds)]


def test_container_name_is_threaded(tmp_path: Path) -> None:
    cmd = _runner().build_execute_command(_spec(tmp_path), "outreach-sbx-12345")
    assert cmd[cmd.index("--name") + 1] == "outreach-sbx-12345"


@pytest.mark.parametrize("phase", ["R", "X"])
def test_image_is_the_penultimate_positional_before_sh(tmp_path: Path, phase: str) -> None:
    runner = _runner(image="myimg:tag")
    spec = _spec(tmp_path)
    cmd = (runner.build_resolve_command if phase == "R"
           else runner.build_execute_command)(spec, "c1")
    # ... <image> sh -c <script>
    assert cmd[-4] == "myimg:tag"


def test_single_command_has_no_spurious_andand(tmp_path: Path) -> None:
    cmd = _runner().build_execute_command(_spec(tmp_path, commands=["cargo test"]), "c1")
    assert cmd[-1] == "cargo test"
    assert "&&" not in cmd[-1]


def test_build_commands_are_pure_no_daemon_touch(tmp_path: Path, monkeypatch) -> None:
    """build_*_command must not probe docker (no _assert_docker_available):
    if they did, this would raise on a host with no daemon. Prove purity."""
    def _boom(*a, **k):
        raise AssertionError("build_*_command must not invoke subprocess")
    monkeypatch.setattr("subprocess.run", _boom)
    spec = _spec(tmp_path)
    for builder in (_runner().build_resolve_command, _runner().build_execute_command):
        cmd = builder(spec, "c1")
        assert cmd[0:2] == [_runner().docker_bin, "run"]
