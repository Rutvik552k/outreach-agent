# Sandbox fixture repos (F-08)

Minimal, project-owned repositories the **live-sandbox lane** (`pytest -m sandbox`)
runs through `DockerSandboxRunner` to exercise C8 end-to-end. They are NEVER
arbitrary upstream repos — only fixtures the project owns (ADR §12, C8 test seam).

Each fixture is shaped so that the **exact** per-stack command vectors the prep
pipeline builds (`prep._SANDBOX_RESOLVE_COMMANDS` for Phase R and
`prep._SANDBOX_EXECUTE_COMMANDS` for Phase X — C8 v2.4 two-phase) run it
unmodified:

| Fixture | Stack | Phase R (network ON, execution OFF) | Phase X (`--network=none`) | Expected verdict |
|---|---|---|---|---|
| `python-pass/` | python | venv in `/work/.sbx-venv` + `pip install --only-binary :all: pytest setuptools wheel` | `pip install -e . --no-deps --no-build-isolation \|\| true && python -m pytest -x -q` (venv python) | green |
| `nodejs-pass/` | nodejs | `npm ci --ignore-scripts --no-audit --no-fund --cache /tmp/.npm` | `npm test` | green |
| `rust-pass/`   | rust   | `cargo fetch` (`CARGO_HOME=/work/.sbx-cargo`) | `cargo test --offline` (same CARGO_HOME) | green |
| `hang/`        | python | same as python-pass | same as python-pass | **timeout** (F-10) — Phase R succeeds, Phase X hangs |

`python-pass` (and `hang`) exercise a **real Phase R**: pytest plus the
setuptools/wheel build backend are fetched as wheels from PyPI in the networked
resolve container, then Phase X builds and tests fully offline. Live-lane runs
leave resolve artifacts in the fixture dirs (`.sbx-venv/`, `node_modules/`,
`.sbx-cargo/`, `target/`) — disposable, safe to delete.

`react` is intentionally **skipped as a separate fixture**: the `react` and
`nodejs` stacks share *identical* resolve and execute vectors. The fixture
exercises the **runner's command construction + execution path**, which is
byte-identical for both stacks, so `nodejs-pass/` covers the react toolchain.
(If the react vectors ever diverge from nodejs, add a react fixture —
`test_fixture_repos.py` asserts this equality and will fail loudly if it drifts.)

## CRLF bomb (F-14)

`tests/fixtures/diffs/crlf_bomb.diff` is a unified diff whose only change is
`LF → CRLF` on every line — the banned whitespace-PR class. It is consumed by the
mocked lane (`run_diff_checks`), not the container, because F-14 is a pure
diff-text invariant with no execution component.

## Toolchain images (live lane only)

The live lane needs images whose `sh -c` can run the commands above. Defaults
assume tags that bundle the toolchain (`python:3.12-slim` already has pip+venv;
node/rust need their official images). Override per fixture via the
`OUTREACH_SBX_IMAGE_<STACK>` env var if your local tags differ. The lane skips
cleanly when Docker is absent, so these are only consulted when a daemon exists.
