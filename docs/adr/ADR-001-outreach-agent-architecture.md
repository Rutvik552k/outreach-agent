# ADR-001 — Outreach Agent Architecture

- **Date:** 2026-06-12 (v2)
- **Status:** Revised — phase-4 gate findings closed; pending re-gate
- **Inputs:** `docs/requirements.md` v0.2 (locked user decisions), `docs/delivery-plan.md` v0.2, research baseline `~/.claude/agent-memory/research-agent/github-contribution-agent-sources.md` (cited below as [RB]), `docs/critique/architecture-critique.md` (findings F-01..F-15), `docs/security/threat-model.md` (V1–V6, S1–S7)
- **Supersedes:** ADR-001 v1 (2026-06-11)

Every load-bearing claim cites its source. Claims that could not be confirmed are marked **UNVERIFIED** with an implementation-time fallback.

## Revision history

| Version | Date | Change |
|---|---|---|
| v1 | 2026-06-11 | Initial architecture. Contracts C1–C7. |
| v2 | 2026-06-12 | Closes all phase-4 gate findings: critique F-01..F-15 + threat model V1–V6. Adds contract C8 (SandboxRunner), two-PR model, graph-verification states, hash-chained audit log, approval-actor binding, workflow-file hard-skip, DB relocation off sync roots. Closure table in §14. |
| v2.1 | 2026-06-12 | V2 actor-binding rule amended after implementation surfaced a paradox: agent's OAuth token acts AS the user, so `actor != agent_login` is unsatisfiable (agent-applied labels show the user's own login). Replaced with structural incapability + audit cross-check (C4). |
| v2.2 | 2026-06-12 | security-engineer re-review (`docs/security/v2.1-signoff.md`): SIGN-OFF-WITH-CONDITIONS. Closes C-1 (lint rule CI-enforced + named test), C-2 (audit cross-check matching rule: exact GitHub-object-id set membership, coarse fail-closed fallback), C-3/C-4 (gateway-bypass + single-instance residuals documented), C-5 (agent venv hash-pinned). |
| v2.3 | 2026-06-12 | Step-4 implementation feedback: comment-id correlation CONFIRMED (githubkit source, exact id-set primary key for comments); label-id correlation NOT confirmable → coarse fail-close rule governs labels; consequently the agent's awaiting-approval marker moved from label to draft-PR title (agent label capability removed entirely — strengthens C-1). C-1/C-2/C-5 implemented + tested (121 tests). |
| v2.4 | 2026-06-12 | C8 two-phase execution: first live Docker run (sandbox lane, 3/5 pass) proved single-phase `--network=none` fails any repo with dependencies. Phase R = network-on resolve with execution structurally off (`--only-binary :all:` / `--ignore-scripts` / `cargo fetch`); Phase X = network-none execution. AC2 exfiltration control preserved where arbitrary code runs. |

---

## 1. Context and forces in tension

A single-user, local-first agent that raises the user's GitHub visibility via genuine, human-approved contributions. Architecturally significant requirements (ASRs):

| ASR | Value | Source |
|---|---|---|
| Scale | Tiny by design: max 1 upstream PR/day, tens of API reads/hr | requirements.md user decisions |
| Latency | None user-facing; scheduled batch runs acceptable | NFR-4 |
| Consistency | Strong, single-writer; every GitHub mutation must be exactly-once from user's perspective | NFR-1, NFR-5 |
| Availability | Best-effort; runs resume after crash (replayable state machine) | acceptance criterion 6 |
| Security | OAuth token + Claude key in Windows Credential Manager; least scope; no secrets in logs/prompts; **untrusted repo code never executes on the bare host** (V1) | NFR-3, NFR-6, threat-model B1 |
| Compliance | GitHub AUP §4, secondary rate limits (80/min, 500/hr content creation), per-repo AI policies [RB] | NFR-2 |
| Cost | Local compute ≈ $0; Claude API spend is the only variable cost | NFR-4 |
| Trust | Nothing reaches upstream without explicit human approval **bound to the human actor** (V2); merge rate is the KPI with auto-pause | FR-3, NFR-1 |

Forces in tension: **automation throughput vs. anti-spam reputation** (resolved by quality gates + budget) and **agent convenience vs. identity/host safety** (resolved by the approval gate, the execution sandbox, and local-only secrets).

---

## 2. Component boundaries and data flow

Single local process (CLI), invoked manually or by Windows Task Scheduler. "Components" are modules with explicit contracts, all sharing one SQLite database (single service boundary — one service, one DB).

```
                       ┌─────────────────────────────────────────────────────────┐
                       │                 outreach-agent CLI (Python)             │
                       │                                                         │
 GitHub Search API ───▶│ [1] Discovery ──▶ [2] Policy Pre-flight ──▶ scoring     │
                       │        ▼                 ▼                              │
                       │   candidates(SQLite) ◀── policy verdicts                │
                       │        ▼                                                │
 Docker/WSL2 ─────────▶│ [3] Prep: clone fork, gen fix via Claude, then          │
 container (C8)        │     SandboxRunner executes repo tests/linters INSIDE    │
                       │     the container — CI-green gate (no bare-host exec)   │
                       │        ▼ pass                                           │
 GitHub API (fork) ◀───│ [4] Approval: push branch + INTRA-FORK draft PR         │
 user reviews on  ────▶│     (base.repo == head.repo == fork, F-03);             │
 github.com            │     poll approval label/comment; actor MUST be fork     │
                       │     owner + not-agent-originated per C4 (V2 v2.1)       │
                       │        ▼ approved (atomic re-check at publish, F-05)    │
 GitHub API (upstream)◀│ [5] Publisher: SECOND PR upstream (two-PR model, F-04); │
                       │     close fork draft; author email = connected/noreply  │
                       │        ▼                                                │
 GitHub API (reads) ──▶│ [6] Review Monitor: poll comments ──▶ Claude drafts     │
                       │     responses ──▶ approval queue (never auto-post)      │
                       │ [6b] Graph Verifier: ≥24h post-merge, confirm           │
                       │     contribution credit (F-01/F-02)                     │
 own repos only ──────▶│ [7] Profile-Growth Engine (proposals → same approval)   │
                       │ [8] Cross-cutting: hash-chained Audit Log (V4) +        │
                       │     Rate-Budget Tracker (gates EVERY GitHub mutation)   │
                       │ [9] Weekly Reporter (incl. graph-credit outcomes)       │
                       └─────────────────────────────────────────────────────────┘
```

Data ownership: SQLite is owned by the CLI process exclusively (single writer, WAL mode, **stored under `%LOCALAPPDATA%\outreach-agent\` — never a cloud-synced path**, F-07 / §6). GitHub is the system of record for PR/review state; SQLite caches it with `last_synced_at` and is reconciled on every run (GitHub wins on conflict).

### Component responsibilities

1. **Discovery** — query `GET /search/issues?q=...&advanced_search=true` (advanced issue search GA 2025-03-06 [RB]) across Python/Rust/Node.js/React for `good first issue`/`help wanted`/reproducible bugs; emit scored `Candidate` rows. Start from user-supplied allowlist per delivery plan. Scoring inputs include the per-repo **attribution outcome history** (repos whose squash merges stripped attribution are deprioritized — F-01).
2. **Policy Pre-flight** — fetch `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE`, repo AI-policy files; hard-skip restrictive repos (curl, Ghostty, tldraw, Matplotlib classes [RB]) and banned contribution types (typo/whitespace/image-opt/docs-drive-by [RB Hacktoberfest precedent]). Verdicts cached with TTL 7 days and re-checked atomically at publish (F-05, failure mode FM5).
3. **Prep** — clone the user's fork into a work dir, reproduce issue, generate fix/tests via Claude; **all repo-authored code (dependency install, build, lint, test) executes only inside the C8 sandbox container** (V1). Only CI-green work proceeds (Renovate merge-confidence pattern [RB]). Pipeline depends on the `SandboxRunner` interface, not on Docker directly, so CI can inject a fake (F-08).
4. **Approval Flow** — push branch to fork, open **intra-fork draft PR** (`POST /repos/{user}/{fork}/pulls`, `base` = fork default, `head` = feature branch, `draft=true`; draft PRs available in all repos since 2025-05-01, [changelog](https://github.blog/changelog/2025-05-01-draft-pull-requests-are-now-available-in-all-repositories/)). GitHub defaults a fork PR's base to the **upstream parent** ([community #11729](https://github.com/orgs/community/discussions/11729)) — the fork's pulls endpoint with explicit base is mandatory, enforced by the C5 invariant `base.repo.full_name == head.repo.full_name == fork` (F-03). Draft contains diff, proposed upstream PR text, risk notes (incl. lockfile/dependency changes and new network calls — V5), policy results. Poll for approval (label `agent:approve-upstream` **or** comment `/approve` — both first-class, both actor-verified identically, F-15) or rejection. Accepted caveat: fork pushes are already public (FR-3, R1).
5. **Publisher** — on approval **re-validated atomically at publish time** (F-05): open the **second, distinct upstream PR** (`POST /repos/{upstream}/pulls`, `head="user:branch"` — a PR's base repo is immutable; an intra-fork draft cannot be retargeted, F-04, [creating-a-pull-request-from-a-fork](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork)), then close the fork draft PR with an audit event. Commits authored with the user's connected/noreply email ([RB], [troubleshooting-missing-contributions](https://docs.github.com/en/account-and-profile/how-tos/contribution-settings/troubleshooting-missing-contributions)).
6. **Review Monitor** — poll upstream PR review comments/states each run; draft substantive responses via Claude into the approval queue; posting always requires approval. **6b Graph Verifier** — for each merged PR, ≥24h after merge ("you may need to wait for up to 24 hours", ibid.), verify contribution credit (§6 graph-verify state, F-01/F-02).
7. **Profile-Growth Engine** — own-repo cadence plans, README/profile polish, pinned-repo recommendations; mutations go through the same approval + publisher path.
8. **Audit Log + Rate-Budget Tracker** — every GitHub mutation is (a) pre-authorized by the budget tracker and (b) recorded in the **hash-chained** append-only audit log (V4, §6). No mutation API call exists outside this wrapper.
9. **Weekly Reporter** — PRs opened/merged, merge rate, **graph-credited vs graph-missing outcomes** (a merged PR with no graph credit is a partial failure surfaced here, F-01), response times, follower/star deltas, sandbox-unfit counts (F-10).

---

## 3. Tech stack

### Decision: Python 3.12+, githubkit, keyring, SQLite (stdlib), anthropic SDK, argparse CLI, Windows Task Scheduler, Docker Desktop (WSL2)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Python + githubkit** | githubkit 0.15.5 released 2026-05-01, fully typed, sync+async, auto-generated from GitHub's OpenAPI spec ([PyPI](https://pypi.org/project/GitHubKit/)); `keyring` 25.7.0 ships a first-party Windows Credential Locker backend ([PyPI](https://pypi.org/project/keyring/)); strongest subprocess/test-orchestration ergonomics; official `anthropic` SDK | Community-maintained (not GitHub-official) | **Chosen** |
| Python + PyGithub | Most-used Python GitHub lib | Actively seeking maintainers ([repo](https://github.com/PyGithub/PyGithub)); sync-only; lags endpoints | Rejected — maintenance risk |
| Node.js + Octokit | GitHub-official | No first-party Windows credential storage; weaker multi-toolchain orchestration fit | Rejected |

**New in v2 — Docker Desktop (WSL2 backend) is an MVP host prerequisite** (V1): Windows Sandbox is **not available on Windows 11 Home** ("Windows Sandbox is currently not supported on Windows Home edition", [Microsoft Learn, threat-model S4]); Docker Desktop on Home via WSL2 is the only strong, disposable, kernel-isolated execution boundary available ([Docker Docs, S5]). If Docker/virtualization is unavailable at runtime, the agent **refuses to prepare contributions** — it never executes third-party code on the bare host (C8).

Supporting choices:

- **CLI:** stdlib `argparse` (`discover`, `prepare`, `status`, `approve-sync`, `report`, `auth login`, `resume`).
- **Scheduler:** Windows Task Scheduler invoking the CLI.
- **Git operations:** shell out to system `git` with **pinned config for every agent clone: `core.autocrlf=false`, `core.longpaths=true`** (F-14 — Windows 260-char `MAX_PATH` breaks deep clones without longpaths; CRLF autoconversion produces whole-file line-ending churn, which is exactly the banned whitespace-PR spam class). The CI-green gate additionally **asserts the generated diff contains no pure line-ending changes** before C3 can be constructed (F-14).
- **HTTP:** githubkit's httpx transport; explicit timeouts on every call.
- **Secrets:** `keyring` → Windows Credential Manager: GitHub OAuth access token, OAuth client secret, Anthropic API key. Never in config files, logs, or **the sandbox container** (C8 mounts no credential paths).
- **Agent's own supply chain (C-5, sign-off condition):** the agent's venv dependencies are **lockfile-pinned with hash verification** (`pip install --require-hashes` against a hash-bearing lockfile, or `uv lock`/`pip-compile --generate-hashes` equivalent). The in-process-malicious-dependency residual (C4 residual 4) is accepted only under this control. CI installs from the hashed lockfile only.

---

## 4. Auth — OAuth App, authorization-code + PKCE, loopback redirect

GitHub App and fine-grained PAT are disqualified by ground truth: a GitHub App attributes work to a bot [RB]; fine-grained PATs cannot write to unaffiliated public repos (§10.2 verbatim). OAuth App user-to-server token acts AS the user — correct.

### Flow (local CLI)

1. **One-time setup:** user registers their own OAuth App. Callback `http://127.0.0.1/callback`; the redirect port need not match and the loopback literal is recommended over `localhost` ([Authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)). Client ID + secret → Credential Manager.
2. `outreach-agent auth login`: bind an ephemeral port **on `127.0.0.1` only — never `0.0.0.0`** (V6), generate `code_verifier` + S256 `code_challenge`, open browser to the authorize URL with `client_id`, `state`, `code_challenge`, `code_challenge_method=S256`, `scope`.
3. Loopback server: accepts **exactly one request**, validates the **single-use, cryptographically random `state`** before any exchange, enforces a **short listener timeout**, and shuts down immediately after the request (V6). Exchange `code` + `code_verifier` (+ client id/secret) at the token endpoint.
4. Token → Credential Manager; never written to disk.

**PKCE ground truth:** GitHub added PKCE 2025-07-14, S256 only; device/installation flows excluded ([changelog](https://github.blog/changelog/2025-07-14-pkce-support-for-oauth-and-github-app-authentication/)). Client secret still required even with PKCE (community-confirmed, [#15752](https://github.com/orgs/community/discussions/15752)) — acceptable: the OAuth App is the user's own, the secret lives in their own Credential Manager (accepted risk R2), and PKCE is defense-in-depth against loopback code interception (threat-model B4).

**Fallback:** device flow (no client secret, [Authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)). Documented, not built, for MVP.

### Scopes (minimum — security-engineer verified, threat-model §5)

| Scope | Why |
|---|---|
| `public_repo` | fork, push branch, create draft + real PRs, read/reply review comments on public repos ([Scopes for OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)) |
| `user:email` | resolve connected/noreply email for commit authorship |

Explicitly **never** requested: `repo`, and `workflow` — **a `public_repo` token's push is rejected when the diff creates/updates `.github/workflows/**`** (threat-model S2/S3, error reproduced in [community #26254](https://github.com/orgs/community/discussions/26254)). This is security-positive (the agent cannot tamper with CI) and is handled as a **hard-skip, never a scope broadening** (V3, §6 state `workflow-file-touch-unsupported`, contract C2/C3). Token rotation: `auth login` re-mints; revocation → failure mode FM4.

---

## 5. Rate-budget design

Ground truth ([RB], [rate-limit doc](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)): primary 5,000 req/hr; secondary limits are the real constraint — 100 concurrent, 900 REST points/min, **content creation 80/min AND 500/hr**; the review-reply endpoint explicitly warns of secondary limiting ([Pull request review comments](https://docs.github.com/en/rest/pulls/comments?apiVersion=2022-11-28)).

- **Budget ledger table** (`rate_budget`): one row per mutation with timestamp + category; budgets computed from the ledger → persist across runs/crashes by construction. Ledger rows are **hash-chained like the audit log** (V4) — tampering to reset the 1/day cap is detectable.
- **Complete per-contribution content-creation enumeration** (F-06 — v1 undercounted): fork-create (1) + fork draft PR (1) + fork-draft close (1) + upstream PR (1) + each review reply (N) + each approval-queue comment posted (N). Branch pushes are git-protocol, not REST content creation. At 1 PR/day: ~4–5 content creations/day — trivially inside both the platform limits and the self-imposed caps. The **daily PR guard keys on upstream-PR creation only**; the `content_creation` ledger category counts all of the above.
- **Self-imposed caps** (~10% of platform): ≤ 8 content-creation calls/min, ≤ 50/hr; all mutations serialized (concurrency = 1); ≥ 2 s spacing.
- **Daily PR budget:** `upstream_pr_opened_today < 1` checked transactionally with the publish intent (AC-6).
- **Header-driven backoff:** persist `x-ratelimit-remaining`/`reset`; on 403/429 with `retry-after`, sleep exactly that + jitter; otherwise exponential backoff from 60 s, max 3 retries; mutations retried only after a read confirms the mutation did not land (FM1).
- **Kill condition:** two secondary-limit hits in 24 h → global pause, run aborts, user notified ([RB] AUP §4).

---

## 6. State model and persistence

### Contribution lifecycle state machine (v2 — two-PR model, F-04)

Two distinct PR objects per contribution: `fork_draft_pr` (approval surface) and `upstream_pr` (the real submission). A PR's base repo is immutable — publishing is a second creation, never a retarget (F-04).

```
discovered → scored → policy-cleared → prepared → ci-green → draft-on-fork
                                                                  │
                       ┌── approved (atomic pre-publish gate) ────┤
                       │                                          ├── rejected (terminal; label, /reject,
                       │                                          │     or user closes draft — reason recorded)
                       ▼                                          │
              upstream-open (+ fork draft closed)                 │
                       │
                       ├──────────────── review-loop ⇄ (changes-requested → prepared')
                       │                     │
                       │                     └── upstream-unavailable (terminal: repo archived/
                       │                           deleted/members-only mid-review; 403/404/422
                       │                           on monitor or fix-up push — F-11)
                       │
                       ├── merged → graph-verify (wait ≥24h) ─┬─ graph-credited (terminal, KPI++)
                       │                                      └─ graph-missing (terminal, KPI++ for
                       │                                            merge-rate, but flagged partial
                       │                                            failure; repo deprioritized — F-01/F-02)
                       └── closed (terminal, KPI--, reason recorded)

Failure states (re-enterable): policy-blocked, ci-failed, sandbox-unfit (F-10),
budget-blocked, llm-blocked (F-13), error(detail)
Terminal skip states: workflow-file-touch-unsupported (V3)
```

State-machine decisions, with finding IDs:

- **Atomic pre-publish gate (F-05, resolves F-12):** in one transaction immediately before the upstream PR creation, the publisher re-reads the fork draft PR and requires ALL of: (1) approval signal still present (label or `/approve` comment), (2) signal actor == fork owner AND signal not agent-originated per the amended V2 controls in C4 (structural incapability + audit cross-check — login comparison alone cannot distinguish the agent from the user it acts as), (3) draft PR still open (a user close = rejection, F-12), (4) policy re-check passes (FM5). Any check fails → abort publish, audit the rejection.
- **graph-verify (F-01/F-02):** entered on merge detection; resolved on the first run ≥24 h later. Verification mechanism: fetch the merged PR's `merge_commit_sha`, `GET /repos/{upstream}/commits/{sha}`, assert the commit is on the default branch and its `author.email` matches the user's connected/noreply email. **UNVERIFIED:** `merge_commit_sha` semantics under squash-merge (whether it reliably points to the squashed default-branch commit) could not be confirmed within this revision's research budget — implementation-time check for backend-engineer; **fallback:** list recent default-branch commits, match by PR number in the commit message subject (squash commits carry `(#<pr>)` by GitHub convention), assert author email; if ambiguous, emit a **manual verification checklist item in the weekly report**. Squash merges can strip attribution ("the commit may be incorrectly attributed to the merger, or the commit will use a non-verified email", [squash attribution changelog 2022-09-15](https://github.blog/changelog/2022-09-15-git-commit-author-shown-when-squash-merging-a-pull-request/)) — `graph-missing` records exactly this; the repo's attribution outcome feeds discovery scoring (deprioritize attribution-stripping repos).
- **upstream-unavailable (F-11):** terminal; **excluded from the merge-rate KPI window** — it is a repo-side event (archived/deleted mid-review), not a quality signal about the agent's work; counting it as a failure would punish the agent for events it cannot predict. It IS reported weekly and feeds repo-health scoring. (Contrast FM6 `external-prs-blocked`, which **is counted** as a decided outcome — auto-close policies are discoverable in advance, so missing one is a discovery-quality failure.)
- **sandbox-unfit (F-10):** test suite hung (wall-clock timeout, **15 min default, config-pinned**), or requires network/Docker-in-Docker/services/secrets unavailable in the sandbox — i.e., the environment, not the patch, failed. Distinct from `ci-failed` (the patch broke tests). Does **not** penalize repo health the way `ci-failed` does; counted and surfaced in the weekly report.
- **workflow-file-touch-unsupported (V3):** pre-push diff scan finds created/modified `.github/workflows/**` → terminal skip, recorded as a decided non-KPI outcome. Never resolved by broadening scope.
- **llm-blocked (F-13):** Claude API outage/timeout/spend-cap mid-prep. `prepared` is reached only on a complete, test-green patch; an LLM failure reverts the contribution to `policy-cleared` (re-enterable), cleans the partial work dir, and respects the monthly spend cap as a hard stop.

Transitions involving a GitHub mutation are recorded as *intent → API call → confirmed* (three audit events) for crash-safe exactly-once recovery (FM7).

### Persistence: SQLite (stdlib `sqlite3`, WAL mode) — relocated (F-07)

**DB path: `%LOCALAPPDATA%\outreach-agent\state.db`** — never the project directory. SQLite documents cloud-synced folders (OneDrive/Dropbox) as a corruption vector and WAL "does not work over a network filesystem" ([sqlite.org/howtocorrupt](https://sqlite.org/howtocorrupt.html), [useovernet](https://sqlite.org/useovernet.html)); the project dir lives under `Downloads`, which is OneDrive-syncable on Windows 11. The exactly-once budget/audit guarantees rest on ACID, so:

- **Startup invariant (fail-fast):** resolve the DB path; refuse to start if it falls under a detected sync root. Detection: path-prefix check against `%OneDrive%`, `%OneDriveConsumer%`, `%OneDriveCommercial%` env vars and a Dropbox `info.json` lookup. Best-effort by design — env-var detection does not cover every sync product; the **mandated default location** (`%LOCALAPPDATA%`, which OneDrive does not sync) is the primary control, the check is the guard rail. Documented limitation.
- DB path is config-pinned; overriding it re-runs the same check.

| Option | Verdict |
|---|---|
| **SQLite** | **Chosen.** Single-user, single-writer, local-first; ACID gives exactly-once budget/state guarantees; stdlib; one-file backup. |
| JSON/JSONL | Rejected — no transactions. |
| Postgres | Rejected — operational dependency, zero benefit at 1 PR/day. |

Core tables: `contributions` (state machine, incl. `fork_draft_pr` + `upstream_pr` ids), `candidates`, `policy_verdicts`, `rate_budget` (hash-chained), `audit_log` (hash-chained append-only), `review_threads`, `kpi_outcomes` (incl. graph-credit outcomes), `profile_actions`, `config_meta` (schema version; chain heads). Migrations additive-only in MVP.

### Audit-log integrity (V4)

"Append-only by convention" is not tamper-evidence — the SQLite file is plain-file writable, and the audit log is the **proof the human approved** (AC-3) while the budget ledger enforces the 1/day cap (threat-model B5, AC4/AC5). Therefore:

- Every `audit_log` and `rate_budget` row stores `prev_hash` and `row_hash = SHA-256(prev_hash || canonical-JSON(row))`. Chain heads persisted in `config_meta`.
- **Startup verification:** full chain re-computation; on mismatch → halt all operations, set global pause, alert the user. Tamper-evidence, not tamper-prevention — sufficient for a single-user local trust model where the goal is detecting post-hoc history rewriting.

---

## 7. LLM integration (Claude API)

Call sites — all behind one `LLMGateway` module:

| Call site | Purpose | Output validation |
|---|---|---|
| Fix generation (prep) | patch + tests for a reproduced issue | repo's own test suite + linters pass **inside the C8 sandbox** — LLM output is never trusted, only test results |
| PR text drafting | convention-following description, linked issue, **mandatory AI-assistance disclosure** ([RB]) | template-validated: disclosure + issue link present, else reject |
| Issue triage drafts | reproduction/triage comments | human approval gate |
| Review-response drafts | substantive replies | human approval gate |

**Model (verified via claude-api skill reference, cached 2026-06-04):** `claude-opus-4-8` ($5/$25 per MTok), config-pinned. Rationale: merge rate is the KPI; ~64% of agent-PR rejections are trust/convention failures [RB]; at ≤1 PR/day worst-case spend ≈ $50/month. `claude-haiku-4-5` available as config override for triage drafts.

**Prompt safety (NFR-6):** prompts contain only repo content, issue text, diffs — never tokens/keychain values/paths outside the work dir; outbound deny-regex (`ghp_`, `github_pat_`, `sk-ant-`, PEM headers) fails closed. Note the deny-regex protects the *prompt path only*; host-side exfiltration is C8's job (threat-model AC2).

**Gateway resilience (F-13):** explicit per-call timeout (120 s default, config-pinned), max 2 retries with backoff on 5xx/timeouts, non-retriable on 4xx; per-run and per-month spend counters in SQLite with a configurable monthly cap as a hard stop → `llm-blocked` state, never a partial `prepared`.

**Injection containment (B2/B3, V5):** attacker-authored issue text/review comments can steer generated code. Controls: (1) C8 sandbox contains execution-time effects; (2) the human gate is the backstop, hardened per V5 — **diff size eligible for approval is capped (400 changed lines default, config-pinned)**, there is **no approve-without-viewing-diff path**, and risk notes must prominently surface lockfile/dependency changes and newly introduced network calls. Larger diffs require explicit override, recorded in the audit log.

---

## 8. Merge-rate auto-pause

Baselines [RB]: Dependabot ~54%; bot PRs 37% vs human 73%; autonomous agents 35–50%.

**Decision (unchanged from v1):** auto-pause discovery when **merge rate < 35% over the trailing 10 decided upstream PRs, evaluated once ≥ 5 outcomes exist**. `upstream-unavailable` outcomes are excluded from the window; `external-prs-blocked` and `workflow-file-touch-unsupported` count as decided non-merges only where the agent could have known better (`external-prs-blocked` yes, `workflow-file-touch-unsupported` no — it is a skip, not a submission). **Immediate-pause triggers:** maintainer spam/AI-slop complaint, repo ban/block event, 2 secondary-limit hits in 24 h. Un-pause manual only (`outreach-agent resume`).

`graph-missing` does not reduce the merge-rate KPI (the PR did merge) but is tracked as a distinct **visibility KPI**: merged-and-credited vs merged-uncredited, reported weekly (F-01).

---

## 9. Failure modes

| # | Failure | Detection | Response | Blast radius control |
|---|---|---|---|---|
| FM1 | Rate-limit hit mid-publish | 403/429 + headers | `intent` persisted; back off per `retry-after`; read-check whether mutation landed before any retry; resume or `budget-blocked` | Mutations serialized; one in-flight publish max |
| FM2 | Fork diverged from upstream | Base-SHA compare at prep AND at the atomic pre-publish gate | Rebase in sandbox; re-run tests (CI-green re-entered); conflicts → `prepared` with draft-PR note | No force-push after upstream PR exists without audit event |
| FM3 | Upstream force-push / base rewritten | Unknown base SHA or 422 on PR ops | Re-fetch, rebase, re-validate; repo-health penalty if unstable | Affected contribution only |
| FM4 | Token revoked/expired | 401 on any call | Global pause; user prompted to `auth login`; 401 non-retriable | Queue intact, resumes after re-auth |
| FM5 | Repo policy changed after pre-flight | Policy re-check inside the atomic pre-publish gate (F-05; verdict TTL ignored at publish) | `policy-blocked`, fork draft closed with explanation, candidate blacklisted | Nothing reaches upstream against current policy |
| FM6 | Upstream auto-closes external PRs (tldraw-class [RB]) | 403/422 at publish, or pre-flight detects | `closed`, reason `external-prs-blocked`, repo hard-skipped; **counts** in KPI window | One wasted prep |
| FM7 | Crash mid-run | `intent`-without-`confirmed` pairs at startup | Reconciliation pass queries GitHub per unresolved intent before new work | WAL + transactional writes |
| FM8 | Sandbox timeout / environment-unfit suite (F-10) | Wall-clock timeout (15 min default) or env-dependency failure signature | `sandbox-unfit (reason)` — distinct from `ci-failed`; no patch-quality repo penalty; weekly-report counter | Container killed + removed; work dir cleaned |
| FM9 | LLM unavailable / over-budget mid-prep (F-13) | LLMGateway timeout/5xx/spend-cap | Revert to `policy-cleared`, clean partial work dir, `llm-blocked` if cap hit | No partial `prepared` ever exists |
| FM10 | Upstream archived/deleted/members-only mid-review (F-11) | 403/404/422 during monitor or fix-up push | `upstream-unavailable` (terminal, KPI-excluded), reported | Affected contribution only |
| FM11 | Diff touches `.github/workflows/**` (V3) | Pre-push diff scan (also re-checked at publish gate) | `workflow-file-touch-unsupported` (terminal skip); never broaden scope | Push rejection never reached |
| FM12 | Audit/budget hash-chain broken (V4) | Startup chain verification | Halt everything, global pause, alert user | No operations on untrusted state |

---

## 10. Open items — resolved

### 10.1 Reply-to-review-comment endpoint (research memo path was WRONG — corrected)

Verified against [Pull request review comments, API 2022-11-28](https://docs.github.com/en/rest/pulls/comments?apiVersion=2022-11-28):

```
POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies
```

`pull_number` required; `comment_id` must be a top-level review comment; body `body` (string); 201 on success. Doc warning: secondary rate limiting — routes through the budget tracker like every mutation.

### 10.2 Fine-grained PAT unaffiliated-contributor limitation (verbatim)

From [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens): fine-grained PAT gaps include "Using fine-grained personal access token to contribute to public repos where the user is not a member"; "Only personal access tokens (classic) have write access for public repositories that are not owned by you or an organization that you are not a member of." Confirms the OAuth-App choice.

### 10.3 Merge-rate threshold — §8 (35% / trailing 10 / min 5 outcomes).

### 10.4 UNVERIFIED items carried to implementation (backend-engineer must confirm before relying on them)

| Item | Risk if wrong | Fallback |
|---|---|---|
| ~~githubkit 0.15.5 `create_pull` exposes `draft` param~~ **RESOLVED 2026-06-12**: confirmed from installed source (`githubkit/versions/v2022_11_28/rest/pulls.py:247`) | — | not needed |
| `merge_commit_sha` semantics under squash merge (§6 graph-verify) | Graph verification mis-targets commit | Default-branch commit scan matching `(#<pr>)` in subject + author-email assert; ambiguous → manual checklist in weekly report (implemented behind `graph_verify.verify_graph_credit()`) |
| Timeline label-event id ↔ label-mutation REST response id correlation (C-2 primary key) | Cross-check primary key unusable | Coarse fail-closed rule per C4: any agent label/comment mutation on the draft ⇒ whole draft ineligible |

---

## 11. Contracts for implementing agents

Errors use an RFC-7807-inspired record: `{type, title, detail, retriable, source_component}`.

### C1 — Candidate (Discovery → Policy/Scoring)

```json
{
  "candidate_id": "string (ULID)",
  "repo_full_name": "string",
  "issue_number": "integer",
  "issue_url": "string (uri)",
  "stack": "enum [python, rust, nodejs, react]",
  "contribution_type": "enum [bugfix-static-analysis, test-addition, issue-triage, dependency-bump]",
  "score": {"repo_health": "0..1", "difficulty_fit": "0..1", "visibility_payoff": "0..1", "attribution_history": "0..1", "total": "0..1"},
  "discovered_at": "ISO 8601 UTC"
}
```
Banned types are unrepresentable (no enum variant). `attribution_history` derives from per-repo graph-credit outcomes (F-01).

### C2 — PolicyVerdict (Pre-flight → state machine)

```json
{
  "candidate_id": "ULID",
  "verdict": "enum [cleared, blocked]",
  "reasons": ["string"],
  "sources_checked": ["CONTRIBUTING.md", "AI policy file", "hard-skip list"],
  "checked_at": "ISO 8601",
  "ttl_expires_at": "ISO 8601  // ignored at publish: always re-checked inside the atomic pre-publish gate (F-05)"
}
```

### C3 — PreparedContribution (Prep → Approval Flow)

```json
{
  "contribution_id": "ULID",
  "branch": "string (agent/<issue>-<slug>)",
  "base_sha": "string",
  "diff_stat": {"files": "int", "insertions": "int", "deletions": "int"},
  "diff_checks": {
    "touches_workflow_files": false,
    "pure_line_ending_changes": false,
    "lockfile_or_dependency_changes": "bool (surfaced in risk notes)",
    "new_network_calls": "bool (surfaced in risk notes)"
  },
  "sandbox_run": {"image": "string", "test_command": "string", "test_exit": 0, "lint_exit": 0, "wall_seconds": "int", "log_path": "string"},
  "pr_text": {"title": "string", "body_md": "string  // MUST contain AI-disclosure section", "linked_issue": "string"},
  "risk_notes": ["string"]
}
```
Construction invariants (object cannot exist otherwise): `sandbox_run.test_exit == 0 and lint_exit == 0` (CI-green, via C8 only — never bare host); `touches_workflow_files == false` (V3 — else terminal `workflow-file-touch-unsupported`); `pure_line_ending_changes == false` (F-14); `diff_stat` within the approval-size cap or explicit-override flag set (V5).

### C4 — Approval protocol (on the INTRA-FORK draft PR)

- **Awaiting-approval marker lives in the draft PR title, NOT a label** (v2.3): implementation proved label-event-id ↔ label-mutation-response-id correlation unverifiable (different id spaces — repo-label id vs issue-event id, githubkit models group_0048/group_0374), so the C-2 coarse rule governs labels; an agent-applied label would therefore void its own draft. Removing the agent's label capability entirely is the fail-closed resolution and strengthens C-1. **Approve:** user adds label `agent:approve-upstream` OR comments `/approve` — both first-class, both verified identically (F-15). **Reject:** label `agent:reject`, comment `/reject`, or closing the draft PR (reason = closing comment).
- **Actor binding (V2, amended v2.1 — hard gate not just audit):** an approval signal is valid ONLY if BOTH hold:
  1. **Owner check:** `actor.login == fork_owner` (from the issue timeline/events API — label-apply requires only triage access, [threat-model S1]). Rejects third parties/collaborators. Operational guidance: keep the fork collaborator-free (accepted risk R4).
  2. **Not-agent-originated check.** The v2 rule `actor.login != agent_oauth_login` is **structurally unsatisfiable**: the agent's OAuth token acts AS the user, so agent-applied labels carry the user's own login in timeline events — login comparison cannot distinguish human-in-browser from agent-via-API. Replaced by two layered controls:
     - **Structural incapability:** the GitHubGateway mutation surface (C5) is a closed set that contains **no operation capable of adding `agent:approve-upstream`, `agent:reject`, or posting `/approve`//`/reject` comments**. The generic `comment` mutation is restricted to upstream review replies and draft-creation context, never comments on an existing fork draft PR. Enforced by (C-1, BLOCKER condition): no such gateway method exists (asserted by test), **plus a CI-enforced, build-breaking lint rule "no HTTP client / githubkit use outside C5", with a positive test proving a violating import or raw label-add anywhere outside C5 fails the build**. This lint rule is the load-bearing control for the whole layer — it is what makes "incapable" true beyond the happy path.
     - **Audit cross-check at the pre-publish gate (C-2 matching rule, sign-off §Q3):** scope = the draft PR's **entire lifetime** (draft-open → gate evaluation; the agent has no legitimate reason to ever mutate the draft's labels or post approval-class comments, so any such agent event is a violation, not noise). **Primary key: exact GitHub-object-id set membership** — every gateway label/comment mutation stores the GitHub-returned object id in its `confirmed` audit event (C6 field `github_object_id`); the gate tests `approval_signal.event_id ∈ {agent-confirmed label/comment ids on this draft}` → ineligible. **Fallback (if label-event-id ↔ mutation-response-id correlation proves unverifiable at implementation — §10.4):** coarse rule — ANY `actor==agent, phase==confirmed` audit event whose endpoint is a label-add/comment-create targeting this draft ⇒ the **whole draft** is ineligible. **Timestamp-window matching is rejected** as a key (false-positive and false-negative prone, gameable). **Any ambiguity in matching ⇒ fail-closed abort + audit + alert.** The chain's tamper-evidence (V4) backs this check.
     - Residual risks (documented per C-3/C-4/C-5):
       1. **Full host compromise** — can forge both the API call and the audit log before chain heads persist. Out of scope for the single-user local trust model (consistent with V4's tamper-*evidence* posture).
       2. **Gateway bypass (C-3):** a mutation that bypasses C5 also skips the audit wrapper, so the cross-check cannot see it — the ONLY control standing between a gateway-bypassing bug and a forged signal is the C-1 lint rule at build time. Stated dependency, not implied.
       3. **Single-instance assumption (C-4):** the cross-check's completeness assumes exactly one agent instance / one hash chain per token. A second agent instance with the same OAuth token writes to a different chain this instance cannot see — running multiple instances defeats the cross-check. Operational rule: one instance per account.
       4. **In-process supply chain (C-5):** a malicious dependency in the agent's own venv runs with the token and can bypass everything. Accepted as out-of-scope ONLY because the agent's own dependencies are **lockfile-pinned with hash verification** (§3) — without that, this residual is not legitimately acceptable.
- **Atomic pre-publish gate (F-05):** approval re-validated in the same transaction as the policy re-check immediately before upstream creation: signal present + actor valid + draft PR open + policy cleared. Test case: label removed after detection, before publish → publish aborts.
- Audit log records GitHub event id + actor login (AC-3 proof, hash-chained per C6).

### C5 — GitHubGateway (single chokepoint for ALL GitHub calls)

- Reads: `search_issues(query) -> [Candidate]`, `get_pr`, `list_review_comments`, `get_commit`, `get_timeline_events`.
- Mutations: `fork_repo`, `push_branch`, `create_draft_pr_on_fork`, `close_fork_draft_pr`, `create_upstream_pr`, `comment`, `reply_to_review_comment(owner, repo, pull_number, comment_id, body)` (§10.1).
- **Intra-fork invariant (F-03):** `create_draft_pr_on_fork` calls `POST /repos/{user}/{fork}/pulls` with explicit `base` = fork default, `head` = feature, `draft=true`, and **asserts the response satisfies `base.repo.full_name == head.repo.full_name == fork`** — abort + audit otherwise. GitHub defaults fork-PR base to the upstream parent ([community #11729](https://github.com/orgs/community/discussions/11729)); this invariant is the single most important testable guard for FR-3.
- **Two-PR model (F-04):** `create_upstream_pr` (`POST /repos/{upstream}/pulls`, `head="user:branch"`) and `close_fork_draft_pr` are separate budgeted mutations, both audited.
- Every mutation: `budget.authorize(category)` → audit `intent` → call → audit `confirmed|failed`. No other module imports the HTTP client. Timeout 30 s; retries per §5 only; never retry a mutation without the idempotency read-check.

### C6 — AuditEvent (append-only, hash-chained — V4)

```json
{
  "event_id": "ULID",
  "ts": "ISO 8601 UTC",
  "actor": "enum [agent, user]",
  "phase": "enum [intent, confirmed, failed]",
  "endpoint": "string (METHOD /path/template)",
  "contribution_id": "ULID | null",
  "outcome": {"status_code": "int | null", "summary": "string"},
  "github_object_id": "string | null  // GitHub-returned object id of the mutation (label-event/comment/PR id) — C-2 cross-check primary key; REQUIRED on confirmed label/comment mutations",
  "rate_state": {"remaining": "int", "reset_at": "ISO 8601"},
  "prev_hash": "string (SHA-256 hex)",
  "row_hash": "string  // SHA-256(prev_hash || canonical-JSON(all fields above))"
}
```
Startup verifies the full chain; break → halt + global pause + alert (FM12). `rate_budget` rows carry the same `prev_hash`/`row_hash` scheme.

### C7 — BudgetAuthorization

`authorize(category: enum[content_creation, other_mutation]) -> {granted, wait_seconds, reason}` — computed transactionally from the hash-chained `rate_budget` ledger against §5 caps + the 1/day upstream-PR budget. Content-creation category covers the full F-06 enumeration (fork-create, fork draft PR, fork-draft close, upstream PR, review replies, comments). Bypassing the tracker is a build-breaking lint rule.

### C8 — SandboxRunner (NEW — V1, F-08, F-10)

All execution of repo-authored code (dependency install, build, lint, test) happens ONLY through this interface.

```
run(spec: SandboxSpec) -> SandboxResult
SandboxSpec  = {work_dir, stack, commands: [string], wall_timeout_s: int (default 900)}
SandboxResult = {test_exit, lint_exit, wall_seconds, log_path, verdict: enum [green, failed, timeout, environment-unfit]}
```

- **Real implementation:** Docker container via WSL2 (Windows Sandbox unavailable on Win 11 Home — [Microsoft Learn, S4]; Docker on Home via WSL2 — [Docker Docs, S5]). **Two-phase execution (v2.4 — first live Docker run proved single-phase network-none fails every repo with dependencies: pip DNS resolution correctly dead in-container):**
  - **Phase R (resolve, network ON, execution OFF):** dependency fetch only, with stranger-code execution structurally disabled: python `pip install --only-binary :all:` (no sdist build scripts; sdist-only deps ⇒ `environment-unfit`, never source builds), nodejs `npm ci --ignore-scripts` (no lifecycle scripts), rust `cargo fetch` (no build.rs execution). Same non-root/read-only/caps/limits hardening; separate container, removed after phase.
  - **Phase X (execute, network NONE):** build/lint/test of the now-vendored tree. All arbitrary code (test suites, conftest, build.rs, npm scripts) runs ONLY here, with no network — preserving the threat-model AC2 exfiltration control exactly where the arbitrary execution happens.
  Hardening, all mandatory in both phases: non-root user; read-only root FS + writable tmp work-mount; dropped capabilities; CPU/memory/pids limits; wall-clock timeout (FM8, per phase); container removed after every run (disposable). **No mount of Credential Manager, keyring data, host env secrets, or any path outside the work dir** — secrets live on the host, outside the sandbox (threat-model AC2). Residual (documented): a malicious package could ship a compromised *wheel/binary* fetched in Phase R — it still cannot exfiltrate during execution (Phase X has no network) and cannot reach host secrets (no mounts); equivalent residual existed in the single-phase design.
- **Refusal rule:** Docker/virtualization unavailable → `SandboxRunner` raises; prep refuses to proceed. **Never bare-host execution.** Docker Desktop is an MVP host prerequisite (§3).
- **Test seam (F-08):** the pipeline depends on this interface; CI injects a `FakeSandboxRunner` returning canned results — the pipeline is fully testable without executing third-party code. The real runner is exercised only in an opt-in integration lane against **fixture repos the project owns** (one per stack), never arbitrary upstream repos, never the default CI lane.

---

## 12. Test-lane split (F-09)

The delivery-plan invariant ("tests mock GitHub API — never call it for real in CI") and AC-1/4/5 ("live GitHub data") are reconciled by two explicit lanes:

| Lane | Runs | Asserts | GitHub | Third-party code |
|---|---|---|---|---|
| **Mocked CI lane** | every commit | pipeline behavior, state machine, C5 invariants (incl. F-03 intra-fork assert), C4 actor binding, C8 fake, hash-chain verification, budget arithmetic | recorded fixtures / mocks via the C5 + C8 seams | never |
| **Live-smoke lane** | manual/scheduled by the user, off CI, real token | AC-1 (≥10 live candidates), AC-4 (graph-verify resolution), AC-5 (review comments surfaced) | real API, real budget enforcement | fixture repos only |

AC-2/3/6/7 are CI-verifiable; AC-1/4/5 are live-smoke-only. This split is binding on testing-engineer (chain step 5a).

---

## 13. Scaling / failure / rollback notes

- **10x (10 PRs/day):** state machine, SQLite, budget design hold; binding constraint is reputation — §8 governs. Caps still ~10x below platform limits.
- **100x / multi-user:** out of scope; new ADR.
- **Rollback:** agent actions reversible at GitHub level (close PR, delete branch — budgeted, audited mutations). Schema additive-only; `config_meta.schema_version`. Killing the scheduled task stops activity; global pause flag = software kill switch.
- **Secrets rotation:** `auth login` re-mints; revocation → FM4.

---

## 14. Gate findings closure table

| Finding | Severity | Resolution | Section |
|---|---|---|---|
| F-01 | BLOCKER | Graph credit tracked separately from merge; per-repo attribution history feeds scoring; merged-uncredited = partial failure in weekly report | §2[6b,9], §6 graph-verify, §8, C1 |
| F-02 | BLOCKER | `merged → graph-verify (≥24h) → graph-credited \| graph-missing` states; verification mechanism + UNVERIFIED fallback | §6, §10.4 |
| F-03 | BLOCKER (per gate verdict) | Intra-fork draft PR via fork's pulls endpoint with explicit base; response invariant `base.repo == head.repo == fork` asserted + tested | §2[4], C5 |
| F-04 | MAJOR | Two-PR model: fork draft + upstream PR distinct budgeted objects; fork draft closed on publish | §6, C5, §5 |
| F-05 | MAJOR | Atomic pre-publish gate: signal + actor + PR-open + policy in one transaction | §6, C4, FM5 |
| F-06 | MINOR | Full content-creation enumeration in ledger; daily guard stays keyed on upstream PR | §5, C7 |
| F-07 | BLOCKER | DB → `%LOCALAPPDATA%\outreach-agent\`; startup fail-fast on sync-root path (best-effort detection documented) | §6 |
| F-08 | MAJOR | C8 SandboxRunner interface + FakeSandboxRunner for CI; real runner opt-in lane, fixture repos only | C8, §12 |
| F-09 | MAJOR | Mocked-CI vs live-smoke lane split; per-AC lane assignment | §12 |
| F-10 | MAJOR | Wall-clock timeout (15 min default); `sandbox-unfit` distinct from `ci-failed`; no patch-quality penalty; reported | §6, FM8, C8 |
| F-11 | MAJOR | `upstream-unavailable` terminal state on 403/404/422 mid-review; excluded from KPI window (rationale recorded) | §6, §8, FM10 |
| F-12 | MINOR | Resolved by F-05 atomic gate (dependency called out explicitly) | §6, C4 |
| F-13 | MINOR | FM9 + LLMGateway timeout/retry/spend-cap policy; `llm-blocked` re-enterable state | §7, §6, FM9 |
| F-14 | MINOR | Pinned git config (`core.autocrlf=false`, `core.longpaths=true`); no-pure-line-ending-diff invariant in C3 | §3, C3 |
| F-15 | MINOR | Label and `/approve` comment both first-class with identical actor verification | C4 |
| V1 | CRITICAL | Contract C8: Docker/WSL2 sandbox, network-none, non-root, read-only FS, dropped caps, limits, no credential mounts, refusal over bare-host | C8, §3 |
| V2 | CRITICAL | Approval actor binding (amended v2.1): `actor == fork_owner` + structural incapability (gateway cannot emit approval signals) + audit cross-check at pre-publish gate. Original login-comparison rule unsatisfiable (agent acts AS user). | C4, §6 |
| V3 | HIGH | `.github/workflows/**` diff scan → terminal `workflow-file-touch-unsupported`; scope never broadened | §4, §6, C3, FM11 |
| V4 | HIGH | Hash-chained audit log + budget ledger; startup verification; halt on break | §6, C6, C7, FM12 |
| V5 | HIGH (should-fix) | Approval diff-size cap (400 lines default), no approve-without-diff, risk notes surface lockfile/network changes | §7, C3, C4 |
| V6 | MEDIUM (should-fix) | Loopback hardening: 127.0.0.1-only bind, single-use state, one-request listener, short timeout | §4 |

---

## 15. Decision summary

1. Single local Python process, modules-with-contracts, SQLite single source of truth at `%LOCALAPPDATA%\outreach-agent\` (sync-root fail-fast).
2. githubkit behind a swappable gateway; keyring → Windows Credential Manager; **Docker Desktop (WSL2) = MVP host prerequisite** for the C8 sandbox.
3. OAuth App auth-code + PKCE (S256), hardened `127.0.0.1` loopback; scopes `public_repo` + `user:email`, never `workflow`; device flow documented fallback.
4. Client-side budget ~10% of secondary limits, serialized mutations, full content-creation enumeration, hash-chained ledger.
5. Two-PR lifecycle (intra-fork draft → distinct upstream PR) with atomic actor-bound pre-publish gate and intent/confirmed audit pairs.
6. Post-merge graph verification (`graph-credited`/`graph-missing`) — visibility KPI distinct from merge-rate KPI.
7. Claude `claude-opus-4-8` config-pinned; sandbox-validated output only; AI disclosure mandatory; diff-size-capped human approval.
8. Auto-pause < 35% merge rate (trailing 10, min 5) + immediate-pause triggers; un-pause manual.

**Next per delivery plan:** lightweight re-gate of this revision, then backend-engineer implementation (chain step 3) against contracts C1–C8.
