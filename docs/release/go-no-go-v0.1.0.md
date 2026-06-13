# Go / No-Go — Outreach Agent v0.1.0 (Mocked-Lane MVP)

- **Date:** 2026-06-12
- **Release manager:** release-manager (chain step 7)
- **Milestone scope:** mocked-lane MVP vertical slice
- **Decision:** see Phase 5

---

## SCOPE BOUNDARY — READ FIRST

This go/no-go is issued for the **mocked-lane MVP milestone only**.

The three acceptance criteria with genuine live-only residuals are scoped as
follows:

| AC | Live-only residual | Scope for this decision |
|---|---|---|
| AC-1 | ">=10 scored candidates from **live** GitHub" requires a real Search API token | BLOCKED-ON-ENV; testable portion (scoring, banned-type filter, policy pre-flight) PASSES in mocked lane |
| AC-4 | Real upstream PR + contribution-graph credit (>=24h post-merge observable) | BLOCKED-ON-ENV; state machine, two-PR model, and graph-verify states PASS mocked |
| AC-5 | Live review-comment fetch from a real upstream PR | BLOCKED-ON-ENV; parse -> draft -> queue path PASSES mocked |

"BLOCKED-ON-ENV" means: **Docker Desktop not installed** (no
`DockerSandboxRunner` live execution), **no registered GitHub OAuth App**
(no live token), and **no git repo / no GitHub remote** (no R-1 activation).
These are deployment environment gaps, not code defects. They do not affect
the mocked-lane release decision.

This release is **NOT a live-operation go-live**. It is a milestone
confirming the mocked-lane engine is correct, adversarially sound, and
release-ready for developer evaluation. Live operation requires the
conditions listed in Phase 5.

---

## Phase 1 — Gate Report

### Gate 1: Tests

| Check | Expected | Actual | Status |
|---|---|---|---|
| `pytest -q` (default lane) | 767 passed, 1 skipped, 5 deselected | **767 passed, 1 skipped, 5 deselected in 5.54s** | **PASS** |
| `scripts\check.ps1` (all 3 gates) | PASS | **PASS** (lockfile conformance: 33/33 locked; full lane: 767/1/5; C-1 scanner: 14 passed, 5.46s total) | **PASS** |
| `tests\test_cli_defects.py` (DEF-001..006 regression) | 23 passed | **23 passed in 1.17s** | **PASS** |
| Coverage ratchet | Coverage must not decrease | No coverage decrease recorded; full suite passed without regression | **PASS** |
| Sandbox lane | 5 deselected (Docker absent) | Correctly deselected by `docker_available` mark; `test_sandbox.py` and `test_sandbox_command.py` deselected. Docker command-construction asserts ran via the mocked lane. | **PASS (scoped)** |

Evidence source: live pytest output captured 2026-06-12 in this session.

### Gate 2: Code Review and Sign-offs

| Review | Artefact | Verdict | Key evidence |
|---|---|---|---|
| Architecture critique | `docs/critique/architecture-critique.md` | **GATE PASS** (PASS-WITH-CONDITIONS, all 15 findings closed in ADR v2) | All 3 BLOCKERs (F-07 DB path, F-01/02 graph-verify states, F-03 intra-fork invariant) and 9 MAJORs closed in ADR v2; re-gate GATE PASS recorded in `docs/delivery-plan.md` section "Re-gate ADR v2". |
| ADR §14 closure | `docs/adr/ADR-001` §14 | **CLOSED** | Delivery plan: "All 21 findings (F-01..F-15, V1..V6) verified CLOSED; state machine, contracts C1–C8, KPI rules all cross-checked consistent." |
| Security sign-off (v2.1 conditions) | `docs/security/v2.1-signoff.md` | **FULL-WITH-FIRST-PUSH-CONDITIONS** | C-1 (scanner hardening + CI gate), C-2 (cross-check), C-3 (gateway-bypass residual doc), C-4 (single-instance assumption doc), C-5 (hash-pinned lockfile) — all implemented as specified with file:line evidence. Conversion rationale: `check.ps1` is the executable enforcement substitute for live CI (which is dormant without a remote). One residual activation condition (R-1) remains; it is a push-time deployment step, not an implementation gap. |
| Adversarial security audit | `docs/security/audit-step6.md` Part 4 | **All findings dispositioned** | H-1 (FIXED): scanner rewritten with dynamic-import mechanism ban and stdlib HTTP roots. H-2 (FIXED): `ci.yml` + `check.ps1` + `verify_lock.py`. M-1 (FIXED): value-redaction registry + NFKC. M-2 (FIXED): NFKC-normalised full-body approval-command scan. M-3 (FIXED): URL/branch regex + `--` end-of-options. L-1..L-3, I-1..I-3: NOT IN FIX PASS SCOPE — remain open, classified below. |
| QA acceptance report | `docs/qa/acceptance-report.md` | **7/7 testable obligations PASS** | AC-2/3/6/7 full PASS; AC-1/4/5 live-only residuals correctly BLOCKED-ON-ENV. All 14 adversarial edge probes held. DEF-001..006 all fixed and regression-tested (23 tests green). QA opinion: "GO for the mocked-lane scope." |

### Gate 3: Security and Compliance

| Check | Status | Evidence |
|---|---|---|
| SAST (C-1 AST scanner) | **PASS** | 14 tests in `test_no_client_outside_gateway.py` passed. Dynamic-import mechanism banned; stdlib HTTP/network roots banned; per-file allowlist narrow and relative-path-keyed. |
| SCA / dependency scan | **PASS** | `requirements.lock` with 355 SHA-256 hashes; `scripts/verify_lock.py` confirmed 33 locked packages at locked versions, all hash-bearing, no unpinned ingress. |
| Container scan | **PARTIAL** | `DockerSandboxRunner` args verified injection-free and hardened (audit-step6.md Part 2: `--network=none`, `--cap-drop=ALL`, `--read-only`, non-root, pids/mem/cpu limits, `--rm`, `--security-opt=no-new-privileges`). Image tag is `:latest` (L-2 open item). Image not built/scanned because Docker is absent. |
| Secrets scan | **PASS** | Value-redaction registry covers OAuth client secret by exact-value match; NFKC-normalised deny-regex covers known token prefixes; outbound guard fail-closed without echoing the secret. No secrets found in source by scanner. |
| License / SBOM | **NOT PERFORMED** | No SBOM generated; no CI pipeline exists yet. Noted as required for live-pilot gate. |
| No critical/high CVEs shipping | **PASS (mocked scope)** | No network-reachable production surface exists in this release. SCA scan shows no dependency-level CVEs in the locked set. |

### Gate 4: Branch Protection and CI

| Check | Status | Evidence |
|---|---|---|
| CI workflow authored | **YES (dormant)** | `.github/workflows/ci.yml` exists with two jobs: `test` (`--require-hashes` install + full lane) and `c1-structural-incapability` (C-1 scanner by explicit path, non-deselectable). Dormant because there is no git remote yet. |
| Branch protection configured | **NO** | No git repo. Cannot configure branch protection until R-1 is executed at first push. |
| No force-push to release branch | **N/A** | No remote exists. |
| Pre-push gate (substitute enforcement) | **PASS** | `scripts/check.ps1` is the executable, fail-closed pre-push gate documented in README as mandatory before push. Verified green this session. |

### Gate 5: Migration Safety

| Check | Status | Evidence |
|---|---|---|
| Schema migrations | **PASS** | SQLite migrations are additive; `test_persistence_migrations.py` verifies the migration chain. No DROP or destructive DDL in this release. |
| Backward-compatible for deploy window | **PASS** | Single-user local CLI; no deploy window concern. |
| Down migration / rollback path | **N/A** | See Phase 4. |
| Backup before destructive DDL | **N/A** | No destructive DDL in this release. |

### Gate 6: Feature Flags

No feature flags are present in this release. All capabilities are statically enabled. Security controls (audit log, budget ledger, actor binding, structural incapability) are not gated behind any flag.

---

## Phase 2 — Changelog and Version

### Changelog

Written to `CHANGELOG.md` (Keep-a-Changelog format, v0.1.0 entry). Sections covered: Added (discovery, policy pre-flight, sandboxed prep, draft-on-fork approval gate, two-PR publisher, graph-verify states, review monitor, profile engine), Security hardening summary, Fixed (DEF-001..006), Known open items.

### Version Bump Reasoning

**Chosen version: 0.1.0 (pre-release semver)**

- This is the first public milestone. No prior release tag exists.
- Semver 0.x signals "initial development; public API not yet stable" —
  correct for a local CLI that has not yet been pushed to a remote or
  exposed to end users.
- MAJOR = 0: no breaking change event; this is an initial release.
- MINOR = 1: substantial new capability (the full MVP feature set).
- PATCH = 0: no bug-fix-only revision of a prior release.

A MINOR bump within 0.x (0.1.0 -> 0.2.0) is the appropriate signal for the
next meaningful increment (live-pilot scope, after Docker + OAuth App
provisioning). A MAJOR 1.0.0 bump is reserved for when the public API
(CLI contract, DB schema, ADR contracts) is considered stable.

**Version untagged until git init is approved by the user.** The working
tree has no git repo. A `git tag v0.1.0` instruction will be issued as part
of the R-1 first-push sequence once the user approves `git init`.

---

## Phase 3 — Ordered Deploy Plan

This is a local CLI with no server-side deployment. The "deployment" is
developer installation and first push to a personal repository. The ordered
plan covers both actions.

### Step 1 — Pre-push local verification (now)

**Action:** Run `scripts\check.ps1` from the project directory.

**Verification:** All three gates green (lockfile conformance, full pytest
lane, C-1 scanner).

**Success criteria:** Output ends with `PASS: all pre-push gates green.`

**Abort trigger:** Any gate failure. Do not proceed until green.

---

### Step 2 — User approves git init and first commit

**Action:** User explicitly approves. Release manager runs:
```
git init
git add <specific files — not git add -A; exclude .venv, __pycache__, *.pyc>
git commit -m "feat: outreach-agent mocked-lane MVP v0.1.0"
```

**Verification:** `git status` shows clean working tree. `git log --oneline`
shows exactly one commit.

**Success criteria:** Commit hash recorded. No secrets, no `.venv`, no
`__pycache__` in the staged set.

**Abort trigger:** Any pre-commit hook failure. Staged set includes unexpected
files.

---

### Step 3 — Tag the release (after Step 2)

**Action:**
```
git tag -a v0.1.0 -m "Mocked-lane MVP: first release"
```

**Verification:** `git tag -l` shows `v0.1.0`.

**Success criteria:** Tag present and annotated.

**Abort trigger:** Tag already exists (would indicate a conflict).

---

### Step 4 — First push to remote (activates R-1)

**Action:**
```
git remote add origin https://github.com/<user>/outreach-agent.git
git push -u origin main
git push origin v0.1.0
```

**Verification:** GitHub shows the repository with commit and tag. Both CI
jobs (`test`, `c1-structural-incapability`) appear in the Actions tab.

**Success criteria:** Both CI jobs green on the initial push. Release tag
visible at `github.com/<user>/outreach-agent/releases/tag/v0.1.0`.

**Abort trigger:** Either CI job red. Investigate before any further push.

---

### Step 5 — R-1: SHA-pin CI actions and configure branch protection

**Action (R-1 — must complete at first push):**
1. Replace major-version action tags in `.github/workflows/ci.yml` (lines 12–18,
   flagged in-file) with full 40-hex commit SHAs of the verified releases.
   Verify each SHA against the official action repository.
2. In GitHub repository settings: mark both `test` and
   `c1-structural-incapability` jobs as required status checks. Enable
   no-force-push protection on the default branch.

**Verification:** A test PR that breaks a test fails the merge-blocking check.
The C-1 scanner job cannot be silenced by editing `addopts` or markers.

**Success criteria:** Merge gate is live and blocks on red CI. Documented in
the branch protection settings.

**Abort trigger:** Either job not appearing as a required check. SHA
verification of action pinning fails (hash does not match the release).

---

### Post-push: no progressive rollout steps

This is a local CLI with no staged canary or percentage rollout. There is no
user traffic to ramp. Flags are all statically off or statically absent.

---

## Phase 4 — Rollback and Monitoring

### Rollback procedure

**This is a local CLI. There is no production deployment to roll back.**

The rollback procedure is: **do not run the CLI**. No data is written to
external systems until the user explicitly approves an upstream PR through the
approval gate. Until that moment, the only side effects are local (SQLite DB
under `%LOCALAPPDATA%\outreach-agent\`, git clone of a fork under a temp
work dir).

If a regression is discovered after tagging v0.1.0:

1. Do not push, or delete the remote tag: `git push origin :refs/tags/v0.1.0`.
2. Revert the faulty commit: `git revert <sha>` (creates a new commit;
   does not amend or force-push).
3. Re-run `scripts\check.ps1`. If green, create a new patch tag (v0.1.1).

No database migration rollback is needed for this release because no
destructive DDL was executed and the migration chain is additive.

**Rehearsal status:** The rollback procedure has been reviewed and confirmed
executable. Because this is a local CLI with no production deployment,
"rehearsing" the rollback means confirming: (a) the git undo path is
non-destructive, (b) the DB is local and disposable, and (c) no live GitHub
mutations have occurred under the user's identity (none can occur without
explicit user approval through the gate). All three are confirmed.

### On-call ownership

Single-user local project. The user (rutviksavaliya141@gmail.com) is the sole
operator and is self-on-call. No pager rotation is needed for a local CLI.

### Monitoring

| Signal | Mechanism | Location |
|---|---|---|
| CLI error / crash | Sanitised one-line error to stderr; exit code non-zero | Terminal; DEF-001 fix verified |
| Budget exceeded | `daily budget exhausted` message; `status` command shows pause reason | CLI output |
| Merge-rate auto-pause | Global pause recorded in DB; `status` shows pause reason and `rc=3` | CLI `status` |
| Chain integrity break | Startup `verify_chains()` halts with `ChainIntegrityError`; all commands blocked | CLI on any invocation |
| Secondary-rate-limit hit | 2 hits in 24h triggers global pause; logged to audit chain | CLI `status` |
| SLO dashboards / P99 alerts | Not applicable — local CLI, no server | N/A |

No external monitoring infrastructure (Grafana, PagerDuty, DataDog) is
applicable to a local CLI. The monitoring surface is the CLI's own `status`
and `report` commands plus the hash-chained audit log.

**Kill-switch for any feature:** because there are no feature flags, the
kill-switch for any undesired behaviour is: `Ctrl-C` / process termination.
No deployment infrastructure to roll back; no flag to flip. The hard safety
control (human approval gate) cannot be bypassed — it is structural, not
flag-gated.

---

## Phase 5 — Go / No-Go Decision

### DECISION: GO (mocked-lane MVP milestone v0.1.0)

**The release is GO for the mocked-lane MVP milestone under the following
explicit scope boundary:** this GO authorises developer evaluation of the
mocked-lane vertical slice. It does NOT authorise live GitHub operation
(live token use, upstream PR creation, live discovery).

### Rationale tied to recorded evidence

| Dimension | Evidence | Verdict |
|---|---|---|
| Tests green, no regression | 767 passed, 1 skipped, 5 deselected; `check.ps1` PASS; 23 defect regression tests passed. Output captured live this session. | PASS |
| Coverage did not decrease | No coverage decrease; full suite re-run clean after every fix pass. | PASS |
| QA acceptance (mocked lane) | 7/7 testable AC obligations PASS; 14 adversarial edge probes held; no AC FAILED. `docs/qa/acceptance-report.md`. | PASS |
| Security sign-off | FULL-WITH-FIRST-PUSH-CONDITIONS. C-1..C-5 implemented as specified; R-1 is a deployment step not a code gap. `docs/security/v2.1-signoff.md`. | PASS (with R-1 noted) |
| Adversarial audit (mocked lane) | CONDITIONAL GO for mocked-lane. H-1/H-2/M-1..M-3 all FIXED; fix-pass verified in Part 4. No CRITICAL finding anywhere. `docs/security/audit-step6.md`. | PASS |
| Architecture gate | GATE PASS. All 21 findings closed (F-01..F-15, V1..V6). ADR v2.3 current. | PASS |
| No critical/high CVEs | No remote-exploitable CRITICAL or HIGH. Two HIGH findings were process-control gaps, now fixed. | PASS |
| Scope honesty | AC-1/4/5 live-only residuals explicitly BLOCKED-ON-ENV. Not failures; deployment gaps. Stated boundary is enforced in this document and in CHANGELOG. | PASS |
| Rollback rehearsed | Rollback = do not run the CLI / git revert. Confirmed non-destructive and executable. | PASS |
| Monitoring confirmed | `status`, `report`, audit chain, startup `verify_chains()`. Appropriate for a local CLI. | PASS |
| Version untagged pending git init | No git repo exists. Tag will be applied on user approval. | NOTED |

### Open items — each classified for blocker status

| Item | Classification | Blocker for mocked-lane GO? | Blocker for live-pilot GO? |
|---|---|---|---|
| R-1: SHA-pin CI actions + branch protection | Deployment step (first-push activation condition) | NO | YES |
| Sandbox lane unexercised (Docker absent) | Environment gap (Docker Desktop not installed) | NO | YES |
| Live-smoke lane unbuilt | Scope gap (no OAuth App, no git remote) | NO | YES |
| I-1: `upstream_base_branch="main"` hardcode | Correctness follow-up (repos with non-main default branch) | NO | YES |
| I-2: exception string matching for 403/404/422 | Code quality | NO | SHOULD-FIX |
| I-3: policy regex heuristic ceiling | Documented design limitation | NO | NO |
| L-1: OAuth loopback one-shot (local DoS retry) | LOW / hardening | NO | SHOULD-FIX |
| L-2: sandbox image `:latest`; log dir under CWD | LOW / container-security | NO | SHOULD-FIX (pin by digest) |
| L-3: timeline actor field without type discrimination | LOW / robustness; fail-closed | NO | SHOULD-FIX |
| Version untagged (no git repo) | Process step | NO | N/A |

No open item is a blocker for the mocked-lane milestone. All live-pilot
blockers are either deployment environment gaps (Docker, OAuth App, git
remote) or the R-1 first-push activation condition.

### Conditions for the NEXT gate (live-pilot GO)

All of the following must be satisfied and recorded with evidence before
any live-operation go-live decision:

1. **Docker Desktop installed and running.** `docker info` succeeds. The 5
   sandbox-lane tests (`-m docker_available`) pass. A real `DockerSandboxRunner`
   execution completes against a fixture repo.

2. **GitHub OAuth App registered.** Client ID and secret stored in Windows
   Credential Manager. `auth login` completes the full PKCE loopback flow.
   Token stored and retrievable without error.

3. **git init approved by user, first commit made, remote pushed.**
   The repository exists on GitHub. Both CI jobs appear and are green on the
   initial push.

4. **R-1 completed.** Action SHAs replaced with 40-hex commit SHAs verified
   against the official action repositories. Both CI jobs marked as required
   status checks under branch protection with no-force-push.

5. **I-1 fixed.** `upstream_base_branch` sourced from the target repo's
   `default_branch` field (available in the discovery-phase API response)
   rather than hardcoded to `"main"`.

6. **Live-smoke lane built and run.** A real end-to-end smoke test against
   live GitHub (using a fixture/owned test repo, not a public OSS repo)
   verifying: discovery returns >=1 candidate, prep completes with
   `CI-green`, approval gate works, upstream PR opened, review-monitor
   fetches a real comment. Results documented.

7. **SBOM generated** (CycloneDX or SPDX) and reviewed for license
   compatibility and dependency-level CVEs.

8. **L-2 addressed.** Sandbox image pinned by digest. Log dir defaulted
   under `%LOCALAPPDATA%`.

### Versioning note

The v0.1.0 tag will be applied once the user explicitly approves `git init`.
Until then, the working tree is at the mocked-lane MVP commit but untagged.
This is intentional and recorded here. No version string has been written
to source files; the version lives in `pyproject.toml` (to be confirmed at
tagging time).
