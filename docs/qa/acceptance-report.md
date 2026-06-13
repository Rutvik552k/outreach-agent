# Outreach Agent — Functional/Exploratory QA Acceptance Report

- **Chain step:** 5b (functional + exploratory QA against MVP acceptance criteria)
- **Date:** 2026-06-12
- **Tester:** qa-engineer
- **Test basis:** `docs/requirements.md` v0.2 acceptance criteria 1–7
- **Lane reference:** `docs/adr/ADR-001-...md` v2.3 §12, `tests/README.md`
- **Build under test:** working tree (no commit; src read-only). Baseline
  `pytest -q` = **721 passed, 5 deselected** (re-confirmed at start and end of run).
- **Scope:** mocked CI lane only. Sandbox lane NOT run (per constraints). Zero web
  research. No source/test modification. Throwaway probes + temp DBs under `%TEMP%`.

> **Numbering note (read first):** the **requirements doc** (the binding test basis
> for this task) and the **tests/README lane table** number the criteria
> differently. Requirements AC-7 = *profile-growth engine*; tests/README "AC-7" =
> *crash-replay/resume*. This report uses the **requirements doc numbering** as
> instructed, and explicitly verifies crash-replay/resume as well (it is the
> mechanism behind AC-6's "full run" guarantee, FM7). The renumbering itself is
> logged as DEF-004 (documentation inconsistency).

---

## 1. Acceptance-criteria verdicts

| AC | Criterion (abbrev.) | Lane | Verdict | Evidence |
|---|---|---|---|---|
| **AC-1** | ≥10 scored candidates from **live** GitHub; banned-type + policy pre-flight filtering proven | live-smoke | **BLOCKED-ON-ENV** (live count) / **PASS** (filtering logic, mocked) | See §2.1 |
| **AC-2** | Prepares complete contribution (branch, diff, PR text w/ AI disclosure), repo tests pass locally | mocked CI | **PASS** | See §2.2 |
| **AC-3** | Draft-PR-on-fork approval; nothing upstream without explicit approval; audit log proves it | mocked CI | **PASS** | See §2.3 |
| **AC-4** | Approved contribution lands upstream via API; commits authored w/ user email; graph-verifiable post-merge | live-smoke | **BLOCKED-ON-ENV** (live graph + real publish) / **PASS** (state machine reaches & resolves graph-verify, two-PR model, mocked) | See §2.4 |
| **AC-5** | Maintainer review comments surfaced within polling interval | live-smoke (fetch) / mocked (parse) | **BLOCKED-ON-ENV** (live fetch) / **PASS** (parse → draft → queue, mocked) | See §2.5 |
| **AC-6** | Daily budget (1 PR/day) + secondary-rate-limit budget never exceeded in a full run (logged proof) | mocked CI | **PASS** | See §2.6 |
| **AC-7** | Profile-growth engine: README proposal + pinned-repo rec + own-repo cadence plan | mocked CI | **PASS** | See §2.7 |

**Coverage of mocked-lane scope (AC-2/3/6/7 + testable parts of 1/4/5): 7/7 testable
obligations PASS.** No AC FAILED. Two criteria (AC-1, AC-4) and part of AC-5 have a
genuinely live-only residual correctly fenced off as BLOCKED-ON-ENV (Docker + a
registered OAuth App are not provisioned on this host, consistent with ADR §12).

---

## 2. Per-AC verification detail

### 2.1 AC-1 — discovery (BLOCKED-ON-ENV for live count; PASS for filtering)

- **Live residual (BLOCKED-ON-ENV):** "≥10 scored candidates from **live** GitHub
  data" requires the real Search API and a token — ADR §12 assigns this to the
  live-smoke lane, which is not built and not runnable on this host. Cannot be
  asserted offline; **not a failure**.
- **Offline-verifiable (PASS):** scoring, banned-type drop, and policy pre-flight /
  hard-skip all run mocked at the C5 seam.
  ```
  pytest -q tests/test_discovery.py tests/test_policy.py  → 11 passed
  ```
  Proving node IDs: `test_banned_type_titles_are_dropped`,
  `test_hard_skip_repo_blocked`, `test_restrictive_contributing_blocked`,
  `test_external_pr_ban_blocked`, `test_clean_repo_cleared`,
  `test_allowlist_queries_come_first`.
- **Edge E10 (empty discovery):** `discover()` with zero search results returns
  0 candidates with no exception. PASS.
- **Edge E1 (banned type unrepresentable):** direct DB insert of
  `contribution_type='typo-fix'` is refused by the SQLite CHECK constraint:
  `CHECK constraint failed: contribution_type IN (...)`. The banned classes are
  structurally unrepresentable (C1). PASS.

### 2.2 AC-2 — contribution preparation (PASS)

```
pytest -q tests/test_state_machine.py tests/test_state_machine_matrix.py \
          tests/test_pipeline_e2e.py            → 548 passed
pytest -q tests/test_prep.py tests/test_diff_checks.py \
          tests/test_diff_checks_edges.py       → 27 passed
```
- CI-green is a hard precondition for the approval queue: `PreparedContribution`
  cannot exist unless `sandbox_run.test_exit == 0 and lint_exit == 0` (C3 invariant,
  enforced in `prep.py` + asserted in tests).
- AI-disclosure: PR-text drafting is template-validated (disclosure + linked issue
  present, else reject) — covered in `test_prep.py`/`test_llm_gateway.py`.
- **Edge E9 (workflow-file scan):** a diff touching `.github/workflows/**` sets
  `touches_workflow_files=True` → terminal `workflow-file-touch-unsupported` (V3). PASS.
- **Edge E8 (CRLF bomb, end-to-end):** `tests/fixtures/diffs/crlf_bomb.diff` →
  `pure_line_ending_changes=True`; such a diff cannot satisfy the C3 construction
  invariant, so it never reaches the approval queue (F-14, banned whitespace class). PASS.

### 2.3 AC-3 — human approval gate + audit proof (PASS)

```
pytest -q tests/test_approval.py tests/test_persistence.py \
          tests/test_persistence_migrations.py  → 25 passed
pytest -q tests/test_publisher.py               → 7 passed
pytest -q tests/test_gateway.py tests/test_no_client_outside_gateway.py → 16 passed
```
- **Nothing reaches upstream without approval:** the atomic pre-publish gate (F-05)
  re-validates signal + actor-binding + draft-open + policy in one transaction.
  Proven by `test_gate_aborts_when_draft_pr_closed`,
  `test_gate_aborts_on_policy_recheck_failure`, `test_gate_aborts_on_rejection_signal`.
- **Audit proves approval (the load-bearing AC-3 evidence):** edge probe E14 drove
  the gate with a valid owner `/approve` on an open draft and confirmed the audit
  row: `actor=user, phase=confirmed, endpoint=gate:pre-publish(F-05)`. The
  human-approval event is recorded in the hash-chained log.
- **Actor binding (V2) — edge E11:** a `agent:approve-upstream` label and a
  `/approve` comment from non-owners (`random-collaborator`, `another-person`)
  produce `approval=None` + 2 violations — never counted as approval.
- **Structural incapability (C-1):** the gateway has no label-add method and its
  comment surface refuses fork-owner targets and approval-class command bodies
  (`StructuralIncapabilityError`), proven by `test_no_client_outside_gateway.py`.
- **Agent-originated signal cross-check (C-2):** `test_agent_comment_object_id_match_rejected`
  + `test_agent_labeled_draft_rejected` (coarse fail-closed for labels).
- **Edge E12:** `/approve-reply` (an upstream-PR reply command) does **not** read as
  a draft `/approve` — exact first-token match, no prefix false-positive. PASS.
- **Edge E13:** gate aborts when the user closed the draft (`state='closed'` ⇒
  rejection, F-12). PASS.

### 2.4 AC-4 — upstream publish + graph credit (BLOCKED-ON-ENV / PASS for state machine)

- **Live residual (BLOCKED-ON-ENV):** the real upstream PR creation and the actual
  contribution-graph credit (verifiable only ≥24h after a real merge) require a real
  token/OAuth App + time — live-smoke lane, not runnable here.
- **Offline-verifiable (PASS):**
  ```
  pytest -q tests/test_graph_verify.py   → 4 passed
  pytest -q tests/test_publisher.py      → 7 passed
  ```
  Two-PR model (intra-fork draft → distinct upstream PR), `head="user:branch"`, and
  the `merged → graph-verify → graph-credited|graph-missing` resolution all verify
  mocked, including the squash-attribution-stripped → `graph-missing` path
  (`test_primary_mechanism_attribution_stripped`) and the ambiguous → manual-checklist
  fallback (`test_fallback_ambiguous_goes_manual`).
- **Intra-fork invariant (F-03):** `create_draft_pr_on_fork` asserts
  `base.repo == head.repo == fork`; violation raises `IntraForkInvariantError` +
  audits — `test_gateway.py`.

### 2.5 AC-5 — review comments surfaced (BLOCKED-ON-ENV fetch / PASS parse)

- **Live residual (BLOCKED-ON-ENV):** the live fetch of real review comments needs
  a real PR + token (live-smoke).
- **Offline-verifiable (PASS):**
  ```
  pytest -q tests/test_review_monitor.py  → 9 passed
  ```
  Comment polling → persistence → Claude-drafted response → approval queue, plus
  changes-requested re-entry, `/approve-reply` budgeted posting, `/reject-reply`
  never-posts, invalid-actor violation, and agent-originated cross-check rejection.
- **Surfacing in report:** pending review-response drafts and posted replies are
  rendered in `report` output (`render_report`, "Review monitor (§2[6], AC-5)").

### 2.6 AC-6 — budget never exceeded, logged (PASS)

```
pytest -q tests/test_budget.py tests/test_budget_clock_edges.py  → 15 passed
```
- **1 PR/day exactly-once — edge E3:** first `upstream_pr` authorize granted; second
  same-day authorize denied: `reason='daily upstream-PR budget exhausted (1/day)'`.
- Clock edges proven: `test_yesterdays_upstream_pr_does_not_block_today`,
  `test_upstream_pr_earlier_today_does_block`.
- Secondary-limit kill condition (2 hits/24h → global pause) and hash-chained ledger
  (tamper-evident) covered in `test_budget*.py` + edge E4 below.
- **Logged proof:** every budget grant appends a hash-chained `rate_budget` row; the
  daily guard reads the ledger transactionally (persists across crashes by construction).

### 2.7 AC-7 — profile-growth engine (PASS)

```
pytest -q tests/test_profile_growth.py  → 7 passed
```
- Produces the three required artifacts (README improvement proposal, pinned-repo
  recommendation, own-repo cadence plan) recorded in `profile_actions` as `proposed`.
- The README proposal is correctly **not auto-applied** — applying it is a mutation
  that flows through the approval + publisher path (printed in `cmd_profile`).
- Surfaced in `report` ("Profile growth proposals (FR-5, AC-7)").

### 2.8 Crash-replay / resume (tests/README "AC-7"; supports AC-6 "full run") — PASS

- `cmd_resume` clears a global pause and audits `actor=user, endpoint=cli:resume`
  (`test_cli_resume_clears_pause_and_audits`).
- Chain re-verification on every DB open (FM12) halts on tamper — edge E4/E5 below.

---

## 3. CLI exploratory pass (8 commands)

Driven through the real entry point (`outreach_agent.cli:main`, the
`[project.scripts]` target) via a throwaway runner under `%TEMP%` (the package is
not pip-installed and has no `__main__.py`, so `python -m outreach_agent` does not
work — see DEF-005).

| Command | Help/usage | Arg validation | Error path tested | Result |
|---|---|---|---|---|
| `--help` / no-arg | clean usage, rc=0 / rc=2 | required subcommand enforced | — | OK |
| bogus subcommand | — | `invalid choice` rc=2, no trace | — | OK |
| `auth login`/`auth bogus` | — | `invalid choice: 'logout'` rc=2 | no registered app → **traceback leak** | **DEF-001** |
| `status` | renders states, PRs today, pause | — | paused → rc=3 clean | OK |
| `report` | renders full report rc=0 | — | paused → rc=3 clean | OK (cosmetic: DEF-003) |
| `resume` | clears pause, audits, rc=0 | — | no-pause → "no global pause active" rc=0 | OK |
| `discover` | — | — | no token → **traceback leak** rc=1 | **DEF-001** |
| `prepare` | — | — | no cleared candidate → clean "run discover first" rc=1 | OK |
| `approve-sync` | — | — | no token (when draft rows exist) → **traceback leak** | **DEF-001** |
| `profile` | — | — | no token → **traceback leak** rc=1 | **DEF-001** |

- **Global pause gating:** every command except `resume` is blocked with
  `rc=3, "agent is globally paused: <reason>"` — clean, no trace. `resume` then
  clears it. (Read-only `status`/`report` are also blocked — defensible but see
  DEF-006 UX note.)
- **OAuth offline (no real flow completed):** V6 loopback hardening is fully
  testable offline — `build_authorize_url` is well-formed (S256, scopes); a 1s
  `oauth_listener_timeout_s` fires `OAuthError` in ~1.0s with no hang; 127.0.0.1-only
  bind is asserted in code; single-use state + CSRF mismatch rejection covered by
  `test_oauth.py` (4 passed). The flow mechanics are solid; only the missing-
  credential entry path leaks (DEF-001).

---

## 4. Defect list (severity-ordered)

### DEF-001 — Missing-credential CLI commands leak a Python traceback to the user — MAJOR

- **Severity:** MAJOR. No data loss/security hole, but a raw stack trace reaches the
  end user on the most common first-run error (no token / no OAuth App yet), directly
  violating the global error-handling rule *[CRITICAL] NO STACK TRACES TO USERS* and
  the project's NFR posture. Downgraded from BLOCKER only because the leaked text
  contains no secret and the underlying message is actionable.
- **Affected AC / requirement:** NFR-3/NFR-6 operability; error-handling domain rule
  "Never expose stack traces … in API responses or UI." Blocks a clean first-run UX
  for AC-1/AC-4/AC-5 live operation.
- **Repro reliability:** 10/10.
- **Steps to reproduce:**
  1. Fresh temp DB, no credentials in Windows Credential Manager.
  2. `outreach-agent --db-path <tmp>/x.db discover`
     (same for `profile`, `auth login`, and `approve-sync` when `draft-on-fork`
     rows exist).
- **Expected:** a one-line, sanitized message + non-zero exit, e.g.
  `auth required: run \`outreach-agent auth login\` (rc=1)`, consistent with the
  graceful `OutreachError` path already used elsewhere in `main()`.
- **Actual:** full multi-frame `Traceback (most recent call last): …` ending in
  `LookupError: credential 'github_oauth_token' not found …`.
- **Root cause (evidence):** `src/outreach_agent/tokens.py:33` (`_get`) and
  `:51/:54` (`oauth_client_id`/`oauth_client_secret`) raise a **bare `LookupError`**,
  not an `OutreachError`. `src/outreach_agent/cli.py:360` only catches `OutreachError`,
  so `LookupError` escapes uncaught through `main()`. `_build_llm`'s
  `anthropic_api_key()` lookup (cli.py:169/200) has the identical flaw.
- **Note:** for `auth login` specifically the leaked message advises
  "run `outreach-agent auth login`" — i.e., it tells the user to re-run the very
  command that just failed (the real fix is to register the OAuth App + store
  client id/secret). Misleading guidance for that command (see DEF-002).
- **Environment:** Windows 11 Home; Python 3.12.7; `.venv`; mocked lane; no Docker,
  no token. Reproduced via the `cli:main` console-script entry point.

### DEF-002 — `auth login` failure message gives circular guidance — MINOR

- **Severity:** MINOR (UX/clarity). Subset of DEF-001's surface but a distinct,
  independently-fixable wording bug.
- **Affected AC / requirement:** ADR §4 one-time-setup operability.
- **Repro reliability:** 10/10.
- **Steps:** `outreach-agent auth login` with no `github_oauth_client_id` stored.
- **Expected:** message points at the one-time OAuth App registration step
  (register app → store client id/secret), not at `auth login` itself.
- **Actual:** `LookupError: credential 'github_oauth_client_id' not found … run
  \`outreach-agent auth login\`` — advises re-running the failing command.
- **Environment:** as DEF-001.

### DEF-003 — Report/console output mojibake for non-ASCII chars on Windows console — MINOR

- **Severity:** MINOR (cosmetic). Affects readability, not correctness.
- **Affected AC / requirement:** FR-6 report readability; i18n UTF-8 rule.
- **Repro reliability:** 10/10 on Windows console (cp1252 default code page).
- **Steps:** `outreach-agent --db-path <tmp>/x.db report`.
- **Expected:** `§2[6]`, `—`, `≥` render correctly.
- **Actual:** rendered as `�` (e.g. `Review monitor (�2[6], AC-5)`, `none yet � run`).
  The report builds `§` and `—` literals (`report.py`) and stdout is not forced to
  UTF-8, so they mojibake under the console's default encoding.
- **Environment:** as DEF-001. (Would not reproduce when output is redirected to a
  UTF-8 file or on a UTF-8 console.)

### DEF-004 — Acceptance-criterion numbering diverges between requirements and tests/README — MINOR

- **Severity:** MINOR (documentation/traceability). Creates real risk of a tester or
  release-manager checking the wrong evidence against an AC number.
- **Affected AC / requirement:** all (traceability of AC ↔ test mapping).
- **Repro reliability:** N/A (static doc inspection).
- **Detail:** `docs/requirements.md` v0.2: **AC-7 = profile-growth engine**,
  **AC-6 = budget**. `tests/README.md` lane table: "AC-6 = budget" (matches) but
  **"AC-7 = crash-replay/resume"** and lists only AC-1..AC-7 mapped to lanes without
  the profile-growth criterion by its requirements number. The README silently
  renumbered/repurposed AC-7. Recommend a single canonical AC list referenced by both.
- **Environment:** N/A.

### DEF-005 — `python -m outreach_agent` fails; no `__main__.py` and package not installed — MINOR

- **Severity:** MINOR (operability/onboarding). The documented invocation surface is
  the console script, but nothing in the repo lets you run the CLI without either a
  `pip install -e .` or knowing to call `outreach_agent.cli`. A new operator following
  the obvious `python -m outreach_agent` hits a confusing error.
- **Affected AC / requirement:** NFR-4 (CLI, local-first) usability.
- **Repro reliability:** 10/10.
- **Steps:** `PYTHONPATH=src python -m outreach_agent status`.
- **Expected:** runs the CLI (or a clear pointer to the right invocation).
- **Actual:** `No module named outreach_agent.__main__; 'outreach_agent' is a package
  and cannot be directly executed`. (Works only via the installed `outreach-agent`
  console script, or `python -m outreach_agent.cli`, or an explicit `cli:main` wrapper.)
- **Suggested fix direction (no fix applied):** add `src/outreach_agent/__main__.py`
  delegating to `cli:main`, and/or document `pip install -e .` in onboarding.
- **Environment:** as DEF-001.

### DEF-006 — Read-only `status`/`report` are blocked under global pause — MINOR (observation)

- **Severity:** MINOR (UX judgment call, not a correctness bug).
- **Detail:** When globally paused (e.g. merge-rate auto-pause or chain break), even
  `status` and `report` return rc=3 and refuse to render. A paused operator most wants
  to *read why* — blocking the two read-only, diagnostic commands works against that.
  `main()` gates everything except `resume` on the pause flag (cli.py:339). Consider
  allowing read-only commands through. Flagged for product decision, not filed as a
  failure.

---

## 5. Exploratory notes (what was probed and what held)

Integrity and safety controls were probed adversarially and **all held**:

- **E1 banned-type DB insert** → SQLite CHECK refuses it (structurally unrepresentable).
- **E2 illegal/terminal transitions** → `discovered→upstream-open` refused;
  `rejected` is sticky (terminal).
- **E3 budget exhaustion** → 2nd same-day `upstream_pr` denied (exactly-once 1/day).
- **E4 audit-log tamper** → mutating `outcome_json` is caught at next DB open
  (`ChainIntegrityError`), and the DB is left globally paused
  ("hash chain break in audit_log at seq=1") so no operation runs on untrusted state.
- **E5 `github_object_id` mirror tamper** → column-vs-hash mismatch caught at open (C-2).
- **E6 sandbox-unfit propagation** → transition surfaces in `report.sandbox_unfit_count`
  and as a `status` state. PASS.
- **E7 LLM monthly spend cap** → after a ~$30 recorded call against a $0.01 cap, the
  next `generate()` hard-stops with `LlmBudgetError` (F-13). Unknown models price at
  the most-expensive known rate (fail-closed toward the cap).
- **E7b secret deny-regex** → a prompt containing `ghp_…` fails closed
  (`SecretLeakError`) before any send (NFR-6).
- **E8 CRLF bomb** (fixture, raw-bytes) → `pure_line_ending_changes=True`; cannot
  satisfy the C3 invariant → never reaches approval (F-14).
- **E9 workflow-file diff** → `touches_workflow_files=True` → terminal skip (V3).
- **E10 empty discovery** → 0 candidates, no crash.
- **E11 non-owner approval** → label + comment from non-owners are violations, never
  approval (V2 actor binding).
- **E12 `/approve-reply` exactness** → does not read as a draft `/approve`.
- **E13 user-closed draft** → pre-publish gate aborts (F-12).
- **E14 valid owner approval** → gate passes and records `actor=user, phase=confirmed`
  in the hash chain (the AC-3 human-approval proof).

No defect was found in any integrity, budget, state-machine, approval-actor-binding,
or LLM-safety control. Every defect filed is in the **CLI presentation / operability
/ documentation** layer — none touch the security- or correctness-critical core.

---

## 6. Release-readiness opinion (mocked-lane scope)

**GO for the mocked-lane scope, conditional on fixing DEF-001 before any end-user
hands-on use.**

- The correctness- and security-critical machinery — state machine, two-PR
  lifecycle, atomic actor-bound approval gate, hash-chained tamper-evident audit +
  budget ledgers, 1-PR/day exactly-once enforcement, sandbox-unfit/workflow-file/CRLF
  guards, LLM spend cap + secret deny-regex — is **solid and adversarially verified**.
  All 7 testable AC obligations PASS; 721/721 mocked tests green; no FAILED criterion.
- The residual live-only assertions (AC-1 ≥10 live candidates, AC-4 real publish +
  graph credit, AC-5 live fetch) are correctly BLOCKED-ON-ENV — they need Docker + a
  registered OAuth App, which ADR §12 already designates as live-smoke-only and which
  are not provisioned on this host. **Not failures**; they remain open verification
  obligations for the live-smoke lane before a true production go-live.
- **Blocking issue before user exposure:** DEF-001 (traceback leak on missing
  credentials). It is the single most likely first-run experience and violates a
  CRITICAL error-handling rule. The fix is small and well-scoped (wrap the keyring
  lookups in an `OutreachError` subtype, or catch `LookupError` in `main()`), but it
  must land before the CLI is put in front of the user.
- **Non-blocking, recommended for the same fix pass:** DEF-002 (circular auth-login
  guidance), DEF-003 (report mojibake), DEF-005 (`python -m` entry point),
  DEF-004 (AC renumbering doc), DEF-006 (read-only commands under pause).

**Bottom line:** the engine is release-ready for the mocked lane and inspires
confidence; ship the one-line-error fix (DEF-001) before any real-user run, and keep
the live-smoke obligations open until Docker + the OAuth App are in place.
