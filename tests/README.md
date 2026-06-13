# Test lanes (ADR-001 §12, F-09)

This suite is split into two lanes to reconcile the delivery-plan invariant
("tests mock GitHub — never call it for real in CI") with the acceptance
criteria that are written against *live* GitHub data.

| Lane | Marker | Runs | GitHub | Third-party code | Default? |
|---|---|---|---|---|---|
| **Mocked CI** | (none) | every commit | recorded fixtures / fakes at the C5 + C8 seams | never executed | **yes** |
| **Live-sandbox** | `sandbox` | manual / opt-in | n/a (no GitHub) | real DockerSandboxRunner against project-owned fixture repos only | no (deselected) |
| **Live-smoke** | `live` *(future)* | manual / scheduled, real token | real API, real budget | fixture repos only | no |

The default-lane exclusion is enforced in `pyproject.toml`:
`addopts = "-m 'not sandbox'"`. The `sandbox` (and future `live`) marker is
registered there too.

## How to run each lane

```bash
# Mocked CI lane (default — hermetic, no Docker, no network, no token):
pytest

# Live-sandbox lane (opt-in; needs a running Docker daemon — see below):
pytest -m sandbox
# show skip reasons when Docker is absent:
pytest -m sandbox -rs

# Everything including the sandbox lane:
pytest -m "sandbox or not sandbox"
```

> The **live-smoke lane** (AC-1/4/5 against the real GitHub API) is run by the
> single user off CI with a real token. Its marker (`live`) and tests are not yet
> built — this README reserves the slot per ADR §12 so the AC→lane mapping below
> is complete.

## Live-sandbox lane prerequisites

`pytest -m sandbox` drives the **real** `DockerSandboxRunner` and therefore needs
a reachable Docker daemon (Docker Desktop / WSL2 — an MVP host prerequisite,
ADR §3). When no daemon is reachable the lane **skips cleanly** with a message
telling you to start Docker; it never fails and never falls back to bare-host
execution (C8 refusal rule). The skip guard probes `docker info`, **not** just
`which docker`, because the CLI is frequently on PATH while the daemon is down.

Per-stack images default to official toolchain tags (`python:3.12-slim`,
`node:20-slim`, `rust:1-slim`); override with `OUTREACH_SBX_IMAGE_PYTHON`,
`OUTREACH_SBX_IMAGE_NODEJS`, `OUTREACH_SBX_IMAGE_RUST` if your local tags differ.

Fixture repos and the CRLF-bomb diff live in `tests/fixtures/` — see
`tests/fixtures/repos/README.md`.

## Acceptance-criterion → lane mapping (ADR §12)

> AC numbering follows `docs/requirements.md` §"Acceptance criteria (MVP)" —
> that document is the **single canonical AC list** (DEF-004). Do not renumber
> or repurpose AC ids here.

| AC | What it asserts (per `docs/requirements.md`) | Lane | Notes |
|---|---|---|---|
| **AC-1** | ≥10 scored candidates from live GitHub | live-smoke | not CI-verifiable — needs real search API |
| **AC-2** | complete contribution prepared (branch, diff, PR text w/ AI disclosure), repo tests pass | **mocked CI** | `test_state_machine*.py`, `test_pipeline_e2e.py` |
| **AC-3** | human approval proof (hash-chained audit) | **mocked CI** | `test_persistence*.py`, `test_approval.py` |
| **AC-4** | contribution-graph credit (verifiable ≥24h post-merge) | live-smoke | CI asserts the *state machine* reaches `graph-verify` and resolves (`test_state_machine*.py`); the live graph itself is live-smoke only |
| **AC-5** | review comments surfaced | live-smoke (surfacing) / **mocked CI** (parsing) | parsing logic is mocked-lane; the live fetch is live-smoke |
| **AC-6** | exactly-once budget / 1-PR-per-day | **mocked CI** | `test_budget*.py` incl. clock-edge rollover |
| **AC-7** | profile-growth engine: README proposal + pinned-repo recommendation + cadence plan | **mocked CI** | `test_profile_growth.py` |

Non-AC regression obligations (no requirements-doc AC number — never list
these under an AC id):

| Item | What it asserts | Lane | Notes |
|---|---|---|---|
| crash-replay / resume | transactional rollback + chain re-verify after interruption | **mocked CI** | previously mislabeled "AC-7" in this file (DEF-004) |

CI-verifiable: AC-2, AC-3, AC-6, AC-7 (+ the testable portions of AC-4/AC-5).
Live-smoke-only: AC-1, and the live portions of AC-4/AC-5.

## Test file → obligation map (testing-engineer chain step 5a additions)

| File | Marker | Closes |
|---|---|---|
| `test_state_machine_matrix.py` | mocked | exhaustive illegal-transition matrix (every state pair vs the declared table) |
| `test_budget_clock_edges.py` | mocked | day-rollover, sliding per-min/per-hr windows, 24h secondary-hit window edges |
| `test_persistence_migrations.py` | mocked | chain verification across v1→v2 migration; C-2 `github_object_id` mirror mismatch |
| `test_diff_checks_edges.py` | mocked | binary files, mixed-EOL, lockfile rename, nested lockfile, workflow-path boundary, CRLF-bomb fixture |
| `test_sandbox_command.py` | mocked | C8 command-line hardening (network=none, cap-drop, read-only, limits, no secret mounts) — pure string asserts, no Docker |
| `test_fixture_repos.py` | mocked | fixture repos well-formed; prep command vectors match each fixture; react==nodejs vector |
| `test_sandbox_live.py` | **sandbox** | real DockerSandboxRunner: green fixtures, hang→timeout (F-10), network-none proof |

Fixtures: `tests/fixtures/repos/{python-pass,nodejs-pass,rust-pass,hang}` and
`tests/fixtures/diffs/crlf_bomb.diff`.
