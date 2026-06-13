# Outreach Agent — Delivery Plan (v0.2)

Source: team-orchestrator output + research-agent findings + user decisions,
2026-06-11. Research baseline:
`~/.claude/agent-memory/research-agent/github-contribution-agent-sources.md`

## Specialist chain (8 steps)

| # | Step | Department | Gate before next step |
|---|------|-----------|----------------------|
| 0 | Ground-truth research — **DONE 2026-06-11** | research-agent | Cited findings delivered (see research baseline) |
| 1 | Architecture & ADR — **DONE 2026-06-11** (`docs/adr/ADR-001`) | solution-architect | ADR answers research findings; contracts C1–C7 defined ✓ |
| 2 | Threat model & secrets design | security-engineer | Token scoping, keychain storage, approval-gate design signed off |
| 3 | Core engine (discovery, GitHub API client, queue) | backend-engineer | Contract tests pass against mocked GitHub API |
| 4 | Approval UX + vertical slice wiring | fullstack-engineer | End-to-end slice works behind human gate |
| 5a | Automated test suites (parallel with 5b) | testing-engineer | Suites green in CI; GitHub API fully mocked |
| 5b | Functional/exploratory QA (parallel with 5a) | qa-engineer | Acceptance criteria pass/fail report |
| 6 | Adversarial security review (read-only) | security-auditor | No critical findings open |
| 7 | Go/no-go, versioning, rollback | release-manager | Explicit GO |

## Always-on invariants (never gated away)

- Human approval before any publish under user identity.
- Least-privilege token scopes; token in OS keychain.
- Audit log of every GitHub mutation.
- ToS/anti-spam guardrails active in every mode.
- Tests mock GitHub API — never call real API in CI.

## MVP scope (revised per research + user decisions)

Single-user, local-first vertical slice:

- **Contribution types (research-backed allowed set)**: lint/static-analysis-
  surfaced real bug fixes (disclosed), test additions, issue
  triage/reproduction, dependency bumps where repo lacks Renovate/Dependabot.
  Docs/typo/whitespace PRs are BANNED (Hacktoberfest spam precedent +
  curl/Ghostty/tldraw/Matplotlib backlash) — original "docs-fix MVP" idea
  dropped.
- **Discovery scope**: Python, Rust, Node.js, React repos via GitHub advanced
  issue search; policy pre-flight (CONTRIBUTING.md / AI-policy) hard-skips
  restrictive repos. Start with user-supplied allowlist, expand to open
  discovery after first merged PRs.
- **Approval UX**: PR-draft-on-fork (user decision) — agent pushes branch +
  draft PR to user's fork; explicit approval triggers upstream PR.
- **Budget**: max 1 upstream PR/day (user decision).
- **Auth**: OAuth App, authorization-code + PKCE, user-to-server token;
  commit author email = user's connected/noreply email (graph attribution).
- **Profile-growth engine: IN MVP** (user decision — expands orchestrator's
  original cut; lowest-risk component, operates only on user's own repos).
- **Deferred**: LLM feature-code PRs, automated review-response *posting*
  (drafting stays, posting needs approval), multi-platform outreach,
  private-mirror staging, multi-tenant anything.

KPI: merge rate (not PR count). Sustained low merge rate auto-pauses
discovery. AI disclosure mandatory in every PR.

## Locked architecture decisions (ADR-001, 2026-06-11)

- **Stack**: Python + githubkit 0.15.5 (typed, current; PyGithub seeking
  maintainers; Octokit lacks Windows credential-store story) + keyring
  25.7.0 (Windows Credential Locker backend). Single process, sync, SQLite
  (WAL, single writer); GitHub is system of record, SQLite reconciles per run.
- **Auth**: user's own OAuth App, auth-code + PKCE (S256, GitHub PKCE GA
  2025-07-14; client secret still required — stored in Credential Manager),
  loopback 127.0.0.1 redirect; device flow as secretless fallback.
- **Approval protocol**: draft PR on fork + `agent:approve-upstream` label
  polling (contract C4).
- **GitHubGateway chokepoint** (C5): every mutation flows budget-authorize →
  audit intent → API call → audit confirmed. Append-only audit log (C6).
  Budget ledger transactional, 1 upstream PR/day, ~10% of secondary limits (C7).
- **Merge-rate auto-pause**: < 35% over trailing 10 decided PRs (min 5);
  immediate pause on spam complaint / repo ban / 2 secondary-limit hits in
  24h; manual un-pause.
- **LLM**: claude-opus-4-8 config-pinned; repo tests+lint are the only
  trusted validator of LLM output (C3 unconstructible otherwise).

## Gate results

### Architecture critique (SDLC phase 4) — PASS-WITH-CONDITIONS (2026-06-11)

`docs/critique/architecture-critique.md` — 15 findings (3 BLOCKER, 9 MAJOR,
3 MINOR). Rate-budget arithmetic, OAuth/PKCE flow, PAT exclusion all
verified correct. BLOCKERs requiring ADR revision before implementation:

- **F-07**: SQLite under Downloads (OneDrive-sync risk) voids WAL/ACID
  guarantees → DB must live under `%LOCALAPPDATA%`, fail-fast if under a
  sync root. (sqlite.org/howtocorrupt)
- **F-01/F-02**: AC-4 contribution-graph credit not modeled — squash merge
  can strip author attribution; merged PR can yield ZERO graph credit. Add
  `merged → graph-verify → graph-credited | graph-missing` states.
- **F-03**: naive create_pull on a fork defaults base to UPSTREAM parent —
  would bypass the approval gate at draft time. C5 must force intra-fork
  base + invariant test `base.repo == head.repo == fork`.

Key MAJORs: two-PR model (fork draft ≠ upstream PR — separate objects,
both budgeted); label-approval TOCTOU re-check at publish; SandboxRunner
mock seam + mocked-CI vs live-smoke lane split; sandbox timeout policy
(`sandbox-unfit` ≠ `ci-failed`); upstream archived/deleted transition.

Implementation-time verify: githubkit `draft` param on create_pull
(UNVERIFIED — confirm against githubkit source).

### Threat model (chain step 2) — CHANGES REQUESTED (2026-06-11)

`docs/security/threat-model.md` — 4 MUST-FIX before implementation:

- **V1 CRITICAL — hostile-repo RCE**: prep sandbox runs third-party test
  suites = stranger-authored code execution. Windows Sandbox NOT on Win 11
  Home; Docker/WSL2 container is the only strong option. New contract
  **C8 SandboxRunner**: `--network=none`, non-root, read-only FS, dropped
  caps, time/resource limits, no keyring mount; refuse bare-host fallback.
- **V2 CRITICAL — approval-label confused deputy**: label-add needs only
  triage; agent's own token HAS triage on the fork → could self-approve.
  C4 must hard-reject label actor ≠ fork owner and actor == agent login.
- **V3 HIGH — workflow-file push rejected**: `public_repo` token cannot
  push `.github/workflows/**` diffs (verified). Hard-skip such diffs;
  NEVER broaden to `workflow` scope.
- **V4 HIGH — audit/budget ledger tamperable**: hash-chain rows, verify at
  startup, halt on break.

SHOULD-FIX: diff review vs test-passing backdoors; loopback OAuth binding.
Verified sound: `public_repo`+`user:email` scopes, PKCE S256, Credential
Manager storage, intent/confirmed mutation, rate budget, auto-pause.

### ADR v2 revision — DONE (2026-06-12, main session)

All gate findings closed: F-01..F-15 + V1–V6. Closure table = ADR §14.
Background solution-architect stalled twice on this task (see SESSION.md);
revision done in main session from gate-doc citations, no new research.
Key additions: contract C8 SandboxRunner (Docker/WSL2, MVP prerequisite),
two-PR model, graph-verify states, hash-chained audit+budget ledger,
approval actor binding, workflow-file hard-skip, DB → %LOCALAPPDATA%,
test-lane split (mocked-CI vs live-smoke), approval diff-size cap.
Two UNVERIFIED items carried to implementation (ADR §10.4): githubkit
`draft` param; merge_commit_sha semantics under squash.

### Re-gate ADR v2 — **GATE PASS** (2026-06-12)

All 21 findings (F-01..F-15, V1..V6) verified CLOSED; state machine,
contracts C1–C8, KPI rules all cross-checked consistent. Two non-blocking
UNVERIFIED items correctly carried to implementation (ADR §10.4).
Implementation (chain step 3) unblocked.

### Chain step 3 (core engine) — DONE (2026-06-12)

backend-engineer delivered `src/outreach_agent/` (14 modules) + 67 passing
tests incl. all 9 gate-named tests. githubkit `draft` param CONFIRMED from
installed source (`pulls.py:247`); review-reply signature matches §10.1.
Both ADR §10.4 UNVERIFIED items resolved. Deviations: none functional
(added audit phase `info` for non-mutation rows; ULID via uuid4+seq column).

**Implementation surfaced V2 design flaw → ADR amended to v2.1**: original
rule `actor != agent_oauth_login` unsatisfiable (agent acts AS user; same
login). Replaced: actor == fork_owner + structural incapability (gateway
cannot emit approval signals; tested + linted) + audit cross-check at
pre-publish gate. security-engineer sign-off IN FLIGHT.

Known follow-ups: DockerSandboxRunner unexercised until fixture-repo lane
(step 5a); network/dependency diff detectors are heuristic risk-flags only;
repo not under git yet.

### v2.1 security sign-off — SIGN-OFF-WITH-CONDITIONS (2026-06-12)

Full review: `docs/security/v2.1-signoff.md`. v2.1 judged STRONGER than the
broken original control (prevention via capability removal beats detection
via login comparison with zero discriminating power). Conditions closed in
ADR v2.2: C-1 lint rule CI-enforced + named test (BLOCKER), C-2 cross-check
matching = exact GitHub-object-id set membership, coarse fail-closed
fallback, timestamp matching rejected (BLOCKER), C-3 gateway-bypass
residual documented, C-4 single-instance assumption documented, C-5 agent
venv hash-pinned lockfile mandated. Sign-off converts to FULL once C-1/C-2
are implemented.

### Chain step 4 (vertical slice + sign-off blockers) — DONE (2026-06-12)

121 tests green (from 67). C-1 implemented (AST import scan + closed-surface
+ runtime refusal tests); C-2 implemented (comment-id correlation CONFIRMED
from githubkit source — exact id-set primary key for comments; label-id NOT
correlatable — coarse fail-closed rule; `github_object_id` in audit chain);
C-5 done (`requirements.lock`, 355 hashes, --require-hashes documented).
Built: discovery, policy pre-flight (hard-skip seed list), LLMGateway
(deny-regex, spend hard-stop), prep pipeline, two-PR publisher with
auto-pause, PKCE OAuth + V6 hardening, 7 CLI commands, e2e pipeline test.
**Design feedback → ADR v2.3**: awaiting-approval marker = draft-PR title,
not label (agent label capability removed entirely).
Deferred to 4b: review-response drafting, profile-growth engine.
Live-lane follow-up noted: candidates schema stores issue URL only (no
body) — prepare prompt thin until live refinement.

### Chain step 4b (review monitor + profile engine) — DONE (2026-06-12)

review_monitor.py + profile_growth.py + CLI `profile` command. 721 tests
passing (incl. AC-7 named test). Key decisions: reply approval =
`/approve-reply <comment_id>` by fork owner on the upstream PR (same C4
verification layers; CLI approve rejected as indistinguishable from agent);
reply cross-check = exact-membership only (coarse rule would void every PR
after first posted reply — agent legitimately comments upstream); never-
auto-post enforced structurally (`response_state='approved'` only settable
by user-actor-verified signal); profile engine read-only (proposals;
README PR would go through normal approval path); pinned-repos =
deterministic ranking, REST has no pinned-repo endpoint (verified — GraphQL
only) → recommendation text. Gateway grew 2 READ methods only.

**Known issue (5a must fix):** `tests/fixtures/repos/hang/` collected by
bare pytest → hangs; needs `norecursedirs`/`collect_ignore` in
pyproject.toml. 4b verified with `--ignore=tests/fixtures`.

### Chain step 5a (test hardening) — DONE (2026-06-12)

Fixture repos per stack (react == nodejs vector, asserted), hang + CRLF
fixtures, sandbox lane (skips on `docker info` fail), state-machine matrix,
budget clock edges, migration chain tests, C8 command-construction asserts.
Hang-fixture collection FIXED (`norecursedirs=["fixtures"]`). Main session
verified bare pytest: **721 passed, 5 deselected, 4.37s, no hang.**
Latent trap documented: `Path.read_text()` translates CRLF→LF (defeats F-14
tests; use read_bytes). Follow-up option: coverage ratchet (pytest-cov)
not yet wired.

### Chain step 5b (QA acceptance) — DONE (2026-06-12)

`docs/qa/acceptance-report.md`. **7/7 testable obligations PASS, zero AC
failed**; AC-1/4/5 live parts correctly BLOCKED-ON-ENV. All 14 adversarial
edge probes held (banned-type CHECK, tamper-halt, 1/day exactly-once, spend
cap, CRLF bomb, non-owner rejection, AC-3 human-approval proof in chain).
6 defects, all CLI/presentation layer, none in core: DEF-001 MAJOR
(traceback leak on missing credential), DEF-002..006 MINOR. QA opinion: GO
for mocked-lane scope conditional on DEF-001 fix.

### Defect-fix pass (DEF-001..006) — DONE (2026-06-12)

All six fixed + 23 regression tests → 744 passed. CredentialError with
per-credential remediation (no traceback to user, no circular guidance);
UTF-8 stdout forced; AC numbering reconciled (requirements canonical);
__main__.py added; status/report allowed under pause with banner, chain-
break pause blocks everything. Follow-up flagged by fixer (pause-reason
prefix coupling cli↔persistence) CLOSED in main session: shared
`CHAIN_BREAK_PAUSE_PREFIX` constant in persistence, single writer
`_pause_chain_break()`, cli imports the constant. 744 green re-verified.

### Chain step 6 (security audit) — DONE (2026-06-12)

`docs/security/audit-step6.md`. Core verified clean: SQL parameterized,
docker argv injection-proof, git args neutralized, C-2/C-3/C-4 implemented
as specified. Verdict: CONDITIONAL GO mocked-lane; sign-off stays
WITH-CONDITIONS until 2 HIGH fixed (no architecture change needed):
- **H-1**: C-1 AST scanner misses importlib.import_module/__import__ and
  stdlib HTTP (urllib/http.client) — bypass path to self-approval that
  skips the audit wrapper.
- **H-2**: enforcement preconditions not executable in-repo: no CI workflow
  exists; `--require-hashes` only in prose, never an install path.
MEDIUM: M-1 deny-regex (no client-secret pattern; prefix-only, homoglyph-
evadable), M-2 prefix-based approval-command check (defense-in-depth),
M-3 latent git-arg surface (currently neutralized).

### Audit-fix pass (H-1/H-2/M-1/M-2/M-3) — DONE (2026-06-12)

**767 passed, 1 skipped (pyyaml absent), 5 deselected.** H-1: scanner bans
dynamic-import primitives + stdlib outbound roots (socket/asyncio/smtplib/
etc.), per-file narrowed allowlist; survived 20/20 in-scope bypass probes
(eval/exec ceiling documented → C-5's control). H-2: ci.yml (2 jobs,
C-1 job non-deselectable) + scripts\check.ps1 + verify_lock.py (33/33 lock
conformance); --require-hashes in executable paths. M-1: value-redaction
registry (covers client secret) + NFKC + zero-width strip, fail-closed.
M-2: token scan anywhere in normalized body. M-3: URL/branch regex asserts
+ `--` end-of-options in git clone/push. Nothing deferred. check.ps1
end-to-end gate PASS. Open low-priority: L-1..L-3, I-1..I-3 (I-1
upstream_base_branch="main" hardcode = correctness follow-up ticket).

### Sign-off conversion — **FULL-WITH-FIRST-PUSH-CONDITIONS** (2026-06-12)

All five conditions C-1..C-5 verified implemented-as-specified with
file:line evidence (appended to v2.1-signoff.md). check.ps1 accepted as
executable enforcement pre-push (live CI unsatisfiable without a remote).
Open: **R-1** at first push — SHA-pin actions in ci.yml + branch
protection requiring both jobs, no force-push.

### Chain step 7 (release gate) — **GO, mocked-lane MVP v0.1.0** (2026-06-12)

CHANGELOG.md + docs\release\go-no-go-v0.1.0.md. All gates PASS except
branch protection (structural — no repo yet; deferred to R-1). GO does NOT
authorize live operation; live-pilot gate = Docker ✓ + OAuth App + git
remote + R-1 + live-smoke lane + I-1 fix + SBOM. Untagged until git init
approved.

### Docker live sandbox lane — first run 3/5 → ADR v2.4 (2026-06-12)

Docker Desktop started by user. network-none proof + nodejs + rust PASS.
python-pass + hang FAIL `environment-unfit`: single-phase --network=none
correctly blocks pip dep fetch (DNS dead in container — isolation working
as designed; fixture/runner model too strict for real repos). **ADR v2.4**:
C8 two-phase — Phase R resolve (network ON, execution structurally OFF:
--only-binary :all: / --ignore-scripts / cargo fetch), Phase X execute
(network NONE). AC2 exfiltration control preserved at execution time.

### Two-phase C8 implemented + sandbox lane FULLY GREEN (2026-06-12)

backend-engineer delivered two-phase DockerSandboxRunner (Phase R resolve
network-on/execution-off, Phase X execute network-none, per-phase timeouts,
phased logs). Main session fixed remaining failure: subprocess decode used
Windows cp1252 → UnicodeDecodeError on docker UTF-8 pull output; now
`encoding="utf-8", errors="replace"`. **Live sandbox lane 5/5 PASS**
(python/nodejs/rust green in real containers, hang→TIMEOUT verdict at wall
clock, network-none proof). Default lane: **791 passed, 1 skipped**.

### Publish + R-1 — DONE (2026-06-12)

- NFR-7 added (user decision): Claude Code CLI = default LLM backend
  (subscription, no API key); Anthropic API = opt-in. 806 tests.
- OAuth App registered (Ov23...; first attempt was a GitHub App — Iv23
  prefix — caught and re-registered), creds in keyring, `auth login` done,
  token `gho_` present (public_repo + user:email).
- Repo live: github.com/Rutvik552k/outreach-agent (public), main +
  v0.1.0 tag pushed (3 commits). Agent token CANNOT push workflow files
  (V3 working as designed) → one-time broad classic PAT used for
  bootstrap; PAT removed from keyring; **user must delete it on GitHub**.
- R-1 CLOSED: actions SHA-pinned (checkout v4.3.1, setup-python v5.6.0,
  resolved via API), branch protection on main: both CI jobs required
  status checks, strict, enforce_admins, no force-push/deletions.
- **CI BLOCKED — account-level**: "account is locked due to a billing
  issue" — GitHub Actions won't start any job. User must resolve at
  github.com/settings/billing, then re-run. Not a code failure.

### Live-smoke round 1 + gap fixes — DONE (2026-06-12, commit 068f502)

- I-1 FIXED: default branch resolved via new gateway read (per-run cache).
- Live smoke crash FIXED: gateway reads retry-once-on-timeout + typed
  retriable GitHubReadError; discover continues per-candidate, no TTL
  cache poisoning.
- auth-login now stores github_login (was never persisted; live DB
  bootstrapped manually first, overwritten cleanly on next login).
- Pipeline gaps WIRED: prepare → submit_for_approval (draft-on-fork, fork
  default branch); graph-verify execution in approve-sync (upstream
  default branch, user_emails from config_meta — fetched + stored).
- **AC-1 LIVE PASS**: 178 candidates / 173 policy-cleared from real GitHub
  search across 4 stacks; per-candidate block-and-continue proven live.
- 820 tests green.
- **Branch protection REMOVED per explicit user decision** (CI billing
  lock made required checks unsatisfiable; user chose full removal over
  temporary lift). **RESTORE R-1 PROTECTION when billing fixed**: both CI
  jobs required, strict, enforce_admins, no force-push.

### Live-smoke round 2 — BLOCKER FOUND (2026-06-12)

Seeded `Rutvik552k/outreach-smoke-target` issue #1 (real slugify bug).
Discovery/policy/fork/clone ✓; **`prepare` FAILED at fix-generation**:
Claude Code returned prose, not an appliable unified diff → `git apply`:
"No valid patches in input". Root cause: LLM is blind (no repo files, only
issue URL as body) and asked for a byte-exact diff; Claude Code's agentic
strength disabled by `--tools ""`/neutral cwd. Full grounded finding:
`docs/findings/smoke-fixgen-blocker.md`. Also found: classifier
banned-marker `whitespace` dropped a genuine bug (false positive).

### ADR-002 fix-generation — DECIDED (2026-06-12, `docs/adr/ADR-002-fix-generation.md`)

Hybrid: **B agentic-in-clone for claude-code** (Claude Code edits files in
the clone with Read/Edit/Write; Bash/WebFetch/WebSearch disallowed;
--permission-mode acceptEdits, --safe-mode, --setting-sources user; pre-strip
CLAUDE.md/.claude/AGENTS.md), **A context-injection for anthropic**. Prep
captures `git diff` after in-place edit; `git apply` removed (the smoke
failure site). Key correction: Claude Code has NO --network flag (must reach
hosted model) → "network off" = no exec/fetch tools only; all repo-code exec
still C8-contained. Also: issue-body re-fetch via gateway get_issue (fixes
blind LLM + fabricated title); classifier banned-marker scope fix; B-path
timeout → 600s. LLMClient protocol unchanged; new FixGenerator; C3 unchanged.

### ADR-002 security gate — SIGN-OFF-WITH-CONDITIONS (2026-06-12, `docs/security/adr-002-signoff.md`)

8 conditions (3 BLOCKERS), each with a named test. Verified by live probing
of local Claude Code 2.1.176. **Key finding: `--safe-mode` is the real
containment linchpin, NOT `--setting-sources user`** — without safe-mode,
user-level MCP servers (Gmail/Drive/Calendar, connected on this host) + LSP
leak into the agentic session despite the `--tools` allowlist. PROBE-1:
agent made only the minimal edit, flagged malicious CLAUDE.md, no Bash, repo
.mcp.json never spawned. AC2 preserved (zero repo-code exec at gen time).
- C-1 BLOCKER: `--safe-mode` mandatory/non-removable; correct ADR-002 text.
- C-2 BLOCKER: complete DIFF-NEUTRAL pre-strip (add .mcp.json, .cursor/,
  .windsurfrules, nested **/CLAUDE.md, copilot-instructions; strip must NOT
  appear in captured git diff).
- C-3 BLOCKER: no-exec-tools argv lint (Bash/WebFetch/WebSearch never in argv).
- C-4 structural cwd-confinement; C-5 R-B2 risk-note surfacing; C-6 no host
  secrets in cwd; C-7 retain hardening flags; C-8 timeout→re-enterable+discard.
Re-review VOID trigger: dropping --safe-mode, adding --mcp-config/--settings/
--add-dir, or widening --tools.

### ADR-002 implemented + verified (2026-06-12, commit 468b572)

FixGenerator (`src/outreach_agent/fix_generator.py`): B agentic-in-clone for
claude-code, A context-injection (search/replace, fail-closed on ambiguous
match) for anthropic. get_issue gateway read feeds real issue title+body.
git apply removed; capture git diff. Classifier banned-markers now
position-gated (genuine whitespace/typo bugs no longer dropped). All 8
security conditions C-1..C-8 with 28 named tests. **851 default-lane tests +
real agentic path verified** (`pytest -m local` → real Claude Code fix in
16.85s). Diff-neutral strip = strip-then-`git checkout --` restore of
tracked config files before diff capture.

### Smoke round 2 attempt 1 — sandbox image gap found + fixed (2026-06-12)

Agentic fix-gen + diff checks PASSED; sandbox returned environment-unfit:
prod config defaulted `sandbox_image=outreach-agent-sandbox:latest` (never
built). Live lane used per-stack official tags. FIX: config `sandbox_images`
per-stack map (python:3.12-slim / node:20-slim / rust:1-slim; react→node) +
`Config.image_for_stack()`; cmd_prepare resolves image by candidate stack.
851 tests still green.

### Smoke round 2 attempt 2 — fix-gen + sandbox WORK; push-auth gap (2026-06-12)

**`prepare: ci-green`** — agentic Claude Code produced a GENUINE fix to the
seeded slugify bug:
```
-    return text.strip().lower().replace(" ", "-")
+    return re.sub(r"\s+", "-", text.strip().lower())
```
PLUS two regression tests (consecutive-spaces, tab/newline). Passed the real
two-phase Docker sandbox (pytest). **Core thesis proven: agentic path yields
real, sandbox-validated quality.** Blocker: `submit_for_approval` push to
fork failed auth — `SystemGitRunner` uses ambient git creds, not the keyring
OAuth token. Needs leak-safe token injection (env-fed credential helper).

### Smoke round 2 attempt 3 — push OK; PR 422 (no commit step) (2026-06-12)

Git-auth FIXED + real-push verified (username=x-access-token + token,
leak-safe, 856 tests). Push now succeeds; PR creation 422. Root cause:
**no `git commit` anywhere in the pipeline** — fix applied to working tree,
captured as diff, sandbox-validated, but never committed → pushed branch ==
fork default → 422 "no commits between". This ALSO means the
author-email attribution (the whole contribution-graph mechanism, ADR §2[5])
was unwired. Masked because mocked tests use FakeGitRunner.

### Smoke round 2 attempt 4 — DRAFT PR CREATED on GitHub (2026-06-12)

Commit+attribution wired (863 tests). Re-run: commit ✓, push ✓, **draft PR
#2 CREATED on Rutvik552k/outreach-smoke-target** (open, draft,
"[agent:awaiting-approval] Fix slugify...", head agent/1-... → base main).
Crash AFTER creation: `_pull_ref` read `p.merge_commit_sha` which the
githubkit v2026_03_10 PullRequest model OMITS (has merged/merged_at, no
SHA). FIX: `getattr(p, "merge_commit_sha", None)` → graph-verify uses its
documented commit-message-scan fallback (ADR §10.4 — primary always
UNVERIFIED). Cleaned up PR#2 + branch; re-running for a clean draft-on-fork.

## Next actions

1. Smoke round 2 attempt 5 (merge_commit_sha fix) — IN FLIGHT. Expect clean
   draft-on-fork state + draft PR awaiting label.
2. On draft PR: USER adds `agent:approve-upstream` label → approve-sync
   opens upstream PR → merge → graph-verify (commit-scan fallback).
3. USER: delete bootstrap PAT on GitHub; fix billing lock → restore R-1.
4. Optional hygiene: regenerate OAuth client secret (was pasted in chat).
