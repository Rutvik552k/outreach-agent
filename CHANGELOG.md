# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-06-12 — Mocked-Lane MVP

> Scope boundary: this release covers the mocked-lane vertical slice only.
> AC-1 (live discovery count), AC-4 (live upstream publish + graph credit),
> and AC-5 (live review-comment fetch) have genuine live-only residuals that
> remain BLOCKED-ON-ENV until Docker, a registered OAuth App, and a git remote
> are provisioned. See `docs/release/go-no-go-v0.1.0.md` for the full
> go/no-go record.

### Added

**Opportunity discovery (FR-1 / AC-1)**
- GitHub advanced-issue-search discovery across Python, Rust, Node.js, and
  React target stacks.
- Scored candidate ranking: repo health, external-PR merge rate, maintainer
  responsiveness, difficulty fit.
- Policy pre-flight engine: parses CONTRIBUTING.md and repo AI-policy files;
  hard-skips repos on the maintained restrictive-repo seed list.
- Banned-contribution-type enforcement via SQLite CHECK constraint (typo-only,
  whitespace, image-optimization, drive-by docs are structurally
  unrepresentable in the candidates table).

**Contribution preparation (FR-2 / AC-2)**
- Full two-step preparation pipeline: fork creation, branch push, patch
  generation via Claude, diff validation.
- DockerSandboxRunner (C8): repo test suite and linters executed in a
  network-isolated, non-root, read-only, capability-dropped container with
  per-container CPU/memory/PID limits. `CI-green` is a hard precondition for
  the approval queue; sandbox-unfit is a distinct, non-penalizing outcome.
- Windows-safe git configuration: `core.autocrlf=false`, `core.longpaths=true`
  enforced for every clone.
- Pure-line-ending-change detection (CRLF bomb guard, F-14): diffs whose only
  changes are line endings are structurally blocked before the approval queue.
- Workflow-file diff detection (V3): any diff touching `.github/workflows/**`
  is hard-skipped with a terminal `workflow-file-touch-unsupported` state.
- PR-text template with mandatory first-class AI-assistance disclosure.

**Draft-PR-on-fork approval gate (FR-3 / AC-3)**
- Two-PR lifecycle: intra-fork draft PR (base = fork default branch, not
  upstream) is the human-review surface; a distinct second PR is opened
  upstream only after explicit approval. Intra-fork invariant enforced by
  `create_draft_pr_on_fork` contract with `base.repo == head.repo == fork`
  assertion and dedicated test.
- Actor-bound approval protocol (ADR v2.3 / C4): approval counted only if the
  approving actor is the fork owner; both label (`agent:approve-upstream`) and
  `/approve` comment paths verified with identical actor-binding.
- Structural incapability layer (ADR v2.3 / C1): `GitHubGateway` has no method
  that can add approval labels or post `/approve`-class comments on a fork
  draft PR. Verified by AST-based C-1 scanner that bans dynamic-import
  primitives and stdlib HTTP/network roots outside the two allowed files.
- Audit cross-check (C2): every gateway mutation stores the GitHub-returned
  object id in a hash-covered `confirmed` event. Pre-publish gate rejects any
  draft whose approval signal id appears in agent-originated confirmed
  mutations (exact id-set membership for comments; coarse fail-closed rule for
  labels). Any ambiguity aborts the publish fail-closed.
- Atomic pre-publish gate: re-validates signal freshness, actor identity,
  draft-open state, and policy in a single SQLite transaction immediately
  before upstream publish. No TOCTOU window.

**Two-PR publisher and graph-verify (FR-4 / AC-4)**
- Upstream PR publisher: `POST /repos/{upstream}/pulls` with `head="user:branch"`;
  fork draft PR is closed with an audit event on upstream open.
- `merged -> graph-verify (pending >=24h) -> graph-credited | graph-missing`
  state machine extension: squash-merge attribution stripping detected and
  surfaced as `graph-missing` rather than silently counting as a KPI success.
- Git argument safety: clone URL and branch validated by regex before use;
  `--` end-of-options separator inserted in `git clone` and `git push`.

**Review monitor (FR-4 / AC-5)**
- Polling-based review-comment fetcher for upstream PRs in `review-loop` state.
- Claude-drafted substantive review responses surfaced to approval queue.
- `/approve-reply <comment_id>` protocol: exact actor-bound verification
  identical to the draft-approval path; agent-originated reply cross-check
  uses exact object-id set membership.
- `reject-reply` signal: response is discarded and never posted upstream.

**Budget and integrity (AC-6)**
- Exactly-once 1-upstream-PR/day budget ledger: transactional, hash-chained,
  tamper-evident. Daily guard is clock-safe across midnight boundaries.
- Secondary-rate-limit kill condition: 2 hits in 24h triggers a global pause.
- Merge-rate auto-pause: sustained rate below 35% over trailing 10 decided PRs
  (minimum 5 outcomes) pauses discovery automatically.
- Hash-chained audit log (`audit_log`) and budget ledger (`rate_budget`):
  chain verified at every DB open; any break sets a global pause and blocks
  all operations.
- DB path mandated under `%LOCALAPPDATA%\outreach-agent\` with startup
  invariant check rejecting OneDrive-synced / cloud-sync roots (F-07; WAL
  correctness).

**Profile-growth engine (FR-5 / AC-7)**
- Profile README improvement proposal generator.
- Pinned-repo recommendation engine (deterministic ranking; GraphQL-only
  endpoint verified; produces recommendation text, not auto-applies).
- Own-repo cadence plan generator.
- All proposals routed through the normal approval + publisher path; engine is
  read-only (no auto-mutations).

**CLI (7 commands)**
- `auth login` / `auth status` — PKCE OAuth loopback flow; credentials in
  Windows Credential Manager.
- `discover`, `prepare`, `approve-sync`, `profile`, `status`, `report`,
  `resume`.
- `python -m outreach_agent` entry point via `__main__.py`.
- `status` and `report` permitted under global pause with a pause-reason
  banner; mutating commands blocked.
- Sanitized one-line error messages for every missing-credential path
  (no traceback exposure to users).

**Observability and reporting (FR-6)**
- Structured `report` command covering: pending candidates, prepared
  contributions, approval queue, budget KPIs, review-monitor queue,
  profile-growth proposals, graph-verify outcomes.

### Security hardening

- V1 (hostile-repo RCE): DockerSandboxRunner with `--network=none`,
  `--cap-drop=ALL`, `--read-only`, non-root user, PID/memory/CPU limits,
  `--security-opt=no-new-privileges`, `--rm`, tmpfs for writes.
- V2 / ADR v2.1–v2.3 (confused-deputy / approval self-emission): capability
  removal + audit cross-check + actor binding replacing the provably
  unsatisfiable `actor != agent_login` check (agent acts AS user via OAuth).
- V3 (workflow-file push): hard-skip on any diff touching `.github/workflows`.
- V4 (audit/budget tampering): SHA-256 hash-chained rows; startup verification
  halts on any break.
- V5 (diff-backdoor): repo test suite + linter as sole trusted validator of
  LLM output (C3 unconstructible otherwise).
- V6 (OAuth loopback): PKCE S256, single-use state, 127.0.0.1-only bind,
  configurable listener timeout.
- LLM outbound safety: NFKC-normalised + zero-width-stripped deny-regex
  covering token prefixes and exact-value match against every in-process
  credential (client secret included); fail-closed without echoing the secret.
- AST-based C-1 scanner: bans `import`/`from` of forbidden HTTP/network roots
  AND dynamic-import primitives (`importlib`, `__import__`,
  `.import_module()`/`.spec_from_file_location()`/`.module_from_spec()`)
  outside the two allowed gateway files. Wired into `scripts/check.ps1`
  (pre-push gate) and `.github/workflows/ci.yml` (non-deselectable CI job).
- `requirements.lock` with 355 SHA-256 hashes; `--require-hashes` in all
  executable install paths; offline conformance verified by
  `scripts/verify_lock.py`.
- `scripts/check.ps1`: single-command pre-push gate running lockfile
  conformance, full pytest lane, and the explicit C-1 job fail-closed.

### Fixed

- DEF-001: missing-credential paths raised bare `LookupError` (traceback
  exposed to user); replaced with typed `CredentialError` subclasses with
  per-credential remediation guidance. `LookupError` caught at `main()` as
  a safety backstop.
- DEF-002: `auth login` failure message gave circular guidance ("run
  `auth login`"); now points at OAuth App registration steps.
- DEF-003: `report` output mojibake under Windows cp1252 console; stdout
  forced to UTF-8 with `errors="replace"`.
- DEF-004: AC numbering diverged between `docs/requirements.md` and
  `tests/README.md`; `docs/requirements.md` is now the single canonical
  source; README updated to match.
- DEF-005: `python -m outreach_agent` failed with no `__main__.py`; module
  entry point added delegating to `cli:main`.
- DEF-006: `status`/`report` were blocked under any global pause including
  operational pauses; both commands are now allowed under pause with a
  visible pause-reason banner; chain-break pauses continue to block
  everything including read-only commands.

### Known open items (non-blocking for mocked-lane milestone)

- I-1: `upstream_base_branch` hardcoded to `"main"` in `cli.py:253`; repos
  whose default branch is not `main` will mis-target the upstream PR base.
  Correctness follow-up; no security impact.
- I-2: `sync_outcome` classifies upstream-unavailable by substring-matching
  `"403"/"404"/"422"` in `str(exc)`; prefer structured status-code matching.
- I-3: `get_repo_file` base64-decodes with `errors="replace"` feeding the
  policy regex; policy regex is a heuristic, not a security boundary (by
  design; hard-skip list is the real control).
- L-1: OAuth loopback one-shot handler; a racing bad request consumes the
  single slot (local DoS, retry self-corrects).
- L-2: sandbox image pinned as `:latest`; should be pinned by digest.
  Log dir defaults under CWD rather than `%LOCALAPPDATA%`.
- L-3: timeline actor binding trusts field presence without object-type
  discrimination; failure direction is fail-closed (empty actor rejected).
- R-1 (first-push activation condition): replace major-version action tags in
  `.github/workflows/ci.yml` with full 40-hex commit SHAs; mark both CI jobs
  as required branch-protection status checks with no-force-push. Cannot be
  satisfied before a git remote exists.
- Sandbox lane: `DockerSandboxRunner` implemented and command-construction
  verified; 5 sandbox-lane tests are deselected in the default run because
  `docker info` is unavailable on this host (Docker Desktop not installed).
- Live-smoke lane: not yet built; needed for AC-1 live count, AC-4 real
  publish + graph credit, AC-5 live review-comment fetch.

---

[0.1.0]: https://github.com/rutviksavaliya141/outreach-agent/releases/tag/v0.1.0
