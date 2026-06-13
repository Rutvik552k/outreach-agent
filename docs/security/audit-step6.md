# Security Audit — Chain Step 6 (Adversarial, Read-Only) — Pre-MVP Release Gate

- **Date:** 2026-06-12
- **Auditor:** security-auditor (read-only; modified nothing)
- **Scope:** `src/outreach_agent/**` + `tests/**`, ADR-001 v2.3, threat-model V1–V6, v2.1-signoff C-1..C-5, QA acceptance-report (step 5b)
- **Method:** source→sink tracing, sign-off-condition verification, adversarial reasoning. Zero web research. Build under audit: 744 tests collected / green, 5 sandbox-lane deselected.
- **Mandate:** verify each v2.1 sign-off condition is implemented as specified; convert SIGN-OFF-WITH-CONDITIONS → FULL or list deltas; give a release-gate GO/NO-GO.

---

## Summary — risk posture

The security-critical core is **genuinely strong and adversarially sound**: the two-PR lifecycle, the atomic actor-bound pre-publish gate, the structural-incapability comment surface, the hash-chained tamper-evident ledgers, the exactly-once 1-PR/day budget, parameterised SQL throughout, and the hardened Docker sandbox args are all implemented carefully and well-tested. I confirm QA's finding that no integrity/budget/state-machine/approval control broke under probing, and I went deeper on the surfaces QA did not: the AST import-scanner bypass class, the git/docker argument-construction paths, the deny-regex coverage, and the sign-off conditions as *implemented vs. specified*.

The findings below are concentrated in **one place that matters**: the load-bearing C-1 "structural incapability" layer rests on an AST scanner that an in-process malicious/ refactored module can bypass (`importlib`/`__import__`/stdlib `urllib`), and that scanner is **not wired to any CI** (no `.github/workflows` exists), so the sign-off's "CI-enforced, build-breaking" requirement is unmet in-repo. Combined with C-5 (`--require-hashes`) being documented-but-not-enforced, the residual the sign-off explicitly conditioned on ("in-process supply chain accepted ONLY IF deps are hash-pinned + the lint rule bites") is **not fully closed**.

Per the ADR's own trust model these are *single-user-local* residuals, not remote-exploitable holes — so they gate the **sign-off conversion**, not the MVP's mocked-lane functionality.

| Severity | Count | Findings |
|---|---|---|
| CRITICAL | 0 | — |
| HIGH | 2 | H-1 (C-1 scanner bypass class), H-2 (C-1 not CI-enforced + C-5 not enforced) |
| MEDIUM | 3 | M-1 (deny-regex coverage), M-2 (approval-class command bypass via structural check), M-3 (git argument-injection latent surface) |
| LOW | 3 | L-1 (OAuth one-shot DoS), L-2 (sandbox image/log path), L-3 (timeline actor field trust) |
| INFO | 3 | I-1..I-3 hardening notes |

No CRITICAL: there is no unauthenticated/remote path to RCE, token theft, or tenant-bypass. Every HIGH requires either an in-process malicious dependency / hostile refactor (the trust boundary the ADR already designates as the agent's own trusted code) or a missing process control.

---

## Part 1 — Sign-off condition verification (C-1..C-5)

### C-1 [BLOCKER in sign-off] — "no HTTP client outside C5" lint rule, CI-enforced + build-breaking, with a positive test that a violating import fails the build.

**Status: PARTIALLY MET — two deltas (→ H-1, H-2).**

What IS implemented (and is good):
- `tests/test_no_client_outside_gateway.py` exists, runs in the default (non-sandbox) pytest lane, walks `src/**` via `ast`, and flags any `import`/`from … import` whose root module is `httpx` or `githubkit`, allowlisting only `github_gateway.py` and `tokens.py`. `test_scanner_detects_violating_import` is the required positive test and it genuinely bites (`tests/test_no_client_outside_gateway.py:59`).
- The gateway mutation surface is asserted to be a closed set (`test_gateway_mutation_surface_is_closed_set`, `:104`) and `test_gateway_has_no_label_capability` (`:110`) proves no label method exists. Verified against source: `GitHubGateway` (github_gateway.py:213–492) exposes exactly the expected closed set; there is no label-add method anywhere.

**Delta 1 (→ H-1): the scanner only inspects `ast.Import` / `ast.ImportFrom` nodes** (`test_no_client_outside_gateway.py:39,41`). It does not model dynamic imports or non-forbidden HTTP transports. Bypasses that defeat "structural incapability beyond the happy path":
- `importlib.import_module("httpx")` / `__import__("httpx")` — `ast.Call` nodes, not scanned. `httpx` **is installed** in the venv (confirmed: `.venv/.../httpx 0.28.1`, `httpcore 1.0.9`), so a bypassing module gets a working GitHub HTTP client and can self-label/self-approve a fork draft PR, skipping C5 *and* the audit wrapper (the exact P2 "gateway bypass also skips audit" residual the sign-off named in C-3).
- stdlib **`urllib.request`** and **`http.client`** are HTTP-capable, always present, and are **not in `FORBIDDEN_ROOTS`** (`:28` = `{"httpx","githubkit"}`). An `import urllib.request` outside C5 makes a raw GitHub API call and the scanner says nothing.
- (Mitigating fact, worth recording: `requests`/`urllib3`/`aiohttp`/`pycurl` are NOT in the venv, so the most "natural" bypass `import requests` fails at import time. The live bypasses are `httpx`-via-dynamic-import and stdlib `urllib`/`http.client`.)

**Delta 2 (→ H-2): the rule is not CI-enforced.** There is **no `.github/workflows/` directory and no CI config of any kind** in the repo (confirmed by enumeration). The sign-off's C-1 is explicit: *"CI-enforced and build-breaking, with a test proving a violating import fails the build."* The test exists; the *CI that runs it as a merge gate does not*. "Build-breaking" is currently an unverifiable process claim, not an in-repo control.

### C-2 [BLOCKER in sign-off] — audit cross-check matching rule: store GitHub object id in `confirmed`; exact id-set membership over the draft's full lifetime; coarse fail-closed fallback for labels; any ambiguity ⇒ fail-closed abort.

**Status: MET — implemented as specified, fail-closed verified.**

- Object id captured on every confirmed mutation and embedded in `outcome_json` (hash-covered) with a queryable mirror column; `verify_chains()` asserts column == hash-covered value (`persistence.py:279–301, 358–370`). Tamper of either is caught at open (QA E5 confirms).
- `cross_check_signal` (approval.py:159–234) scans agent-confirmed label/comment mutations on the draft over its full lifetime; comment signals use **exact `github_object_id` set membership** (correlation VERIFIED per ADR v2.3); label signals use the **coarse rule** (any agent label mutation ⇒ whole draft ineligible). Missing object id / unparseable outcome / missing target ⇒ `ambiguous=True`, ineligible, fail-closed. The gate audits the cross-check rejection and aborts (approval.py:287–297). Reply-side mirror (`reply_signal_agent_originated`, review_monitor.py:199–236) is exact-membership and fail-closes on a missing object id.
- **Object-id capture on EVERY label/comment mutation path** (audit task item C-2): there is no label mutation path at all (capability removed — strengthens this), and every comment path (`comment`, `reply_to_review_comment`) routes through `_mutate(..., object_id_of=…)` (github_gateway.py:457–492). Fail-closed branches cross-checked: the cross-check treats a confirmed comment mutation *without* an object id as ambiguous→ineligible, so a code path that forgot to capture the id fails safe, not open. **Confirmed.**

One scoping caveat (not a defect, but state it): the cross-check is keyed `WHERE contribution_id=?` and on a single hash chain. This is exactly the documented C-4 single-instance assumption; completeness depends on every mutation recording its `contribution_id` (the gateway always passes it where a contribution exists).

### C-3 / C-4 — gateway-bypass residual + single-instance assumption documented.

**Status: MET (documentation).** Both are written into ADR C4 residuals 2 and 3 (ADR §11 C4, lines 363–364). H-1/H-2 below are about residual 2 (gateway bypass) being *more reachable in practice* than "documented residual" implies, because the only control naming it (C-1 scanner) has the bypass class in Delta 1.

### C-5 [SHOULD-FIX, MEDIUM in sign-off] — agent's own venv lockfile-pinned with hash verification; in-process-malicious-dep residual accepted ONLY under that control.

**Status: PARTIALLY MET — delta (→ H-2).**

- The hash-bearing lockfile **exists and is correct**: `requirements.lock` was generated by `uv pip compile … --generate-hashes` and carries 355 `--hash=sha256:…` entries.
- But **nothing enforces `--require-hashes` at install time**: `--require-hashes` appears only in prose (ADR, delivery-plan, README), never in an executable install step, CI job, Makefile, or constraint. The audit prompt's own note ("venv was NOT installed with --require-hashes") is consistent with this: the lockfile is hash-bearing, the *enforcement* is absent. Since the sign-off accepts the in-process-supply-chain residual (P5) *only* under hash-verified deps, the acceptance precondition is not yet satisfied in-repo.

---

## Part 2 — Findings

### H-1 — C-1 "structural incapability" scanner is bypassable by dynamic import and stdlib HTTP (defeats the load-bearing approval-self-emission control)

- **Severity:** HIGH. CVSS-style: local, low-complexity, but **requires the attacker to already control in-process code** (a malicious/compromised agent dependency, or a future refactor) — i.e. it sits at the agent's own-code trust boundary the ADR designates as trusted. Impact if reached: confidentiality+integrity of A1 (token-as-user) and A4 (reputation) — a bypassing module can self-label/self-approve a fork draft and push an upstream PR under the user's identity, *and* skip the audit wrapper so the C-2 cross-check (which only sees audited mutations) cannot catch it. Not CRITICAL because there is no remote/unauthenticated trigger and the human-in-browser approval is still a parallel backstop for any PR the user actually looks at.
- **Confirmed (traced).**
- **Location:** `tests/test_no_client_outside_gateway.py:31–46` (scanner), `:28` (`FORBIDDEN_ROOTS = {"httpx","githubkit"}`); reachable client `.venv/Lib/site-packages/httpx` (installed).
- **Exploit scenario:**
  1. A transitive dependency in the agent's venv is compromised (the P5 supply-chain residual) — or a careless future refactor adds a helper outside C5.
  2. It executes, in-process with the live OAuth token (resolvable from keyring at runtime): `import importlib; gh = importlib.import_module("githubkit").GitHub(token)` — or `import urllib.request` and POST directly to the REST API.
  3. It applies `agent:approve-upstream` to the fork draft (or posts `/approve`) and/or opens the upstream PR directly. None of this is an `ast.Import` node → the C-1 scanner is silent at "build" time; the call never enters `_mutate` → no `intent/confirmed` audit row → the C-2 cross-check has nothing to match → the forged approval reads as human (the exact P2 chain the sign-off flagged as the real residual).
- **Remediation (for security-engineer):** Strengthen the scanner from an import-name denylist to a capability allowlist: (a) flag `ast.Call` to `importlib.import_module`, `__import__`, `importlib.util.spec_from_file_location` outside the allowlist; (b) add stdlib HTTP roots to `FORBIDDEN_ROOTS` (`urllib`, `urllib.request`, `http.client`, `socket` used for connect, `ftplib`, plus `httpcore`); (c) treat the allowlist as "these two files may import a transport — nothing else may import OR dynamically resolve one." Recognise the ceiling: AST cannot stop a determined in-process adversary (`getattr`-chains, C extensions) — so pair this with C-5 enforcement (H-2) and keep the documented residual honest. Consider a runtime defence: drop the token from process env/keyring cache except during the C5 call window.

### H-2 — Sign-off's two enforcement preconditions (C-1 CI gate, C-5 `--require-hashes`) are documented but not enforced in-repo

- **Severity:** HIGH (process/control gap that the sign-off explicitly made load-bearing). Same blast radius as H-1; this is the *enforcement* half.
- **Confirmed.**
- **Location:** repo root — no `.github/workflows/**` exists (no CI runs the C-1 test as a gate); `requirements.lock` is hash-bearing but no install path passes `--require-hashes`.
- **Exploit scenario:** Without a CI gate, a PR that introduces a forbidden import (or removes the `addopts`/test) merges green if a developer doesn't run the full suite locally; "build-breaking" is aspirational. Without `--require-hashes`, `pip install` can resolve a tampered/typosquatted transitive dep that doesn't match the locked hash, instantiating the P5 in-process adversary that H-1 then weaponises — the two findings chain.
- **Remediation:** (1) Add a CI workflow that runs the full default pytest lane (incl. `test_no_client_outside_gateway.py`) as a required, merge-blocking check; (2) make the only supported install path `pip install --require-hashes -r requirements.lock` (or `uv pip sync` against the hashed lock) and assert it in CI; (3) until both exist, the v2.1 sign-off conditions C-1 (CI-enforced) and C-5 (hash-verified) cannot be marked closed.

### M-1 — LLM outbound deny-regex misses the OAuth client secret and is prefix-only (homoglyph/split-token evadable)

- **Severity:** MEDIUM. The deny-regex is, by ADR design, a prompt-path-only leak guard (host-side exfil is C8's job), which correctly bounds impact. But two real gaps within its own remit.
- **Confirmed.**
- **Location:** `llm_gateway.py:27–29` (`_DENY = ghp_|github_pat_|sk-ant-|PEM`).
- **Exploit scenario / gaps:**
  - The **GitHub OAuth client secret (A2)** has no fixed prefix and **no pattern in `_DENY`** — if any code path ever placed it in a prompt/system string, it would send. (Today no call site does, but the guard is the backstop precisely for the path nobody intended.)
  - The match is literal-prefix; it does not catch a token that an injected issue body has been crafted to make the LLM echo with a zero-width space or line split (`ghp​_…`). Since the guard runs on *outbound* text (what the agent sends), the realistic vector is narrow, but the guard advertises more coverage than it has.
- **Remediation:** Add an OAuth-client-secret pattern (or, better, never let secret-bearing strings reach `generate()` — assert at construction that prompt/system are built only from repo/issue text). Document explicitly that `_DENY` is a defence-in-depth backstop, not the primary secret boundary, so the gap isn't mistaken for coverage. Normalising unicode (NFKC) + stripping zero-width chars before matching closes the homoglyph/split evasion cheaply.

### M-2 — Approval-class command structural check is first-line/prefix based; defence-in-depth, not airtight

- **Severity:** MEDIUM (the *primary* control — the fork-owner-repo refusal — is solid; this is about the secondary command-body check).
- **Confirmed.**
- **Location:** `github_gateway.py:433–455` (`_assert_comment_capability`): refuses bodies where any stripped line `startswith(comment_approve)` / `comment_reject`.
- **Analysis:** The strong half (`owner == fork_owner → refuse`, `:441`) means the agent can never comment on the fork at all — that is the real structural incapability and it holds. The command-body half is a belt-and-suspenders check for upstream targets. It is `str.startswith` on each stripped line, so `" /approve"` (leading space, stripped) is caught, but the matching is purely lexical: it would not catch a homoglyph `／approve` (fullwidth solidus) or a command embedded mid-line. Because the owner check already blocks the only place an approval comment could *matter* (the fork draft), the residual is low — an upstream `/approve` body is inert for draft approval — but the check shouldn't be relied on as the boundary it's described as.
- **Remediation:** Keep the owner-repo refusal as the boundary (it is). Treat the command-body check as advisory; if it's meant to be a control, NFKC-normalise and scan all lines (not just prefix) for approval tokens. Do not let future code weaken the owner check on the assumption the body check covers it.

### M-3 — Latent git argument-injection surface (currently neutralised by URL/int framing, but fragile)

- **Severity:** MEDIUM (latent — not currently exploitable, but one refactor from it).
- **Confirmed not currently exploitable; flagged as fragile.**
- **Location:** `prep.py:151–153` (`git clone … fork_clone_url str(work_dir)`, `git checkout -b branch`), `:145` (`branch = f"agent/{issue_number}-{slugify(issue_title)}"`), `publisher.py:63` (`git push origin prepared.branch`); upstream sources: `discovery._repo_full_name` (attacker-named repos), issue title/body.
- **Analysis:** All git calls use list-form `subprocess.run(["git", …])` (no shell) — shell metacharacters are inert (good). `fork_clone_url` is interpolated into a full `https://…` URL, so an attacker repo name starting with `-` becomes a URL path segment, not a flag (neutralised). `branch` is `agent/<int>-<slug>` where `slugify` strips to `[a-z0-9-]` and the leading `agent/` prevents a leading-dash branch (neutralised). `issue_number` is `int()`-coerced. So **no injection today.** The fragility: the protection is incidental (URL prefix, `agent/` prefix, slug charset), not a deliberate "validate-then-pass" with `--` end-of-options separators. A future change that passes a raw repo/branch as a bare git arg, or relaxes `slugify`, reopens it.
- **Remediation:** Make the safety explicit, not incidental: insert `--` before positional args in every git invocation that takes user-derived values (`git clone -- <url> <dir>`, `git checkout -b -- <branch>` where supported, `git push origin -- <branch>`); add an assertion that `branch` matches `^agent/\d+-[a-z0-9-]+$` and that `repo_full_name` matches `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$` before any use. This turns a latent finding into a closed one.

### L-1 — OAuth loopback one-shot handler enables a trivial local approval DoS

- **Severity:** LOW. PKCE + single-use state already prevent code/token theft (verified); this is availability only.
- **Location:** `oauth.py:104–135` (`server.handle_request()` serves exactly one request).
- **Scenario:** Any local process that races the user's browser and hits `127.0.0.1:<port>/callback` first with a wrong/empty `state` consumes the single request; the handler sets `result.error`, `handle_request()` returns, and the real callback is never processed → login fails (no token minted, no security loss). Low because it's local-only nuisance, self-correcting on retry.
- **Remediation:** Loop `handle_request()` until a request with the matching `state` arrives or the listener timeout fires, instead of consuming the first arbitrary request. (Keeps single-use of the *valid* state while tolerating a racing bad request.)

### L-2 — Sandbox image is `:latest` and log dir defaults under CWD

- **Severity:** LOW (hardening / container-security rule alignment).
- **Location:** `config.py:26` (`sandbox_image = "outreach-agent-sandbox:latest"`), `sandbox.py:103` (`log_dir = Path.cwd() / "sandbox-logs"`).
- **Analysis:** `:latest` violates the project's own container rule ("never `latest` in production; pin to digest"); a re-pull could change the execution base under the agent's feet. The Docker run args themselves are well-hardened (`--network=none`, `--cap-drop=ALL`, `--read-only`, non-root, pids/mem/cpu limits, `--rm`, `no-new-privileges`, tmpfs) and **repo content cannot inject docker flags** — `work_dir` is `work_root/<ULID>`, `container_name` is a timestamp, `commands` are code constants (`prep.py:37–42`); confirmed no path/space/`--` injection into the docker argv (audit item 5: clean). CWD-relative log dir can write `sandbox-logs/` into whatever directory the CLI is launched from (minor; if CWD is a synced/clone dir, log noise).
- **Remediation:** Pin the sandbox image by digest (`@sha256:…`) and rebuild on base updates; default the log dir under `%LOCALAPPDATA%\outreach-agent\sandbox-logs` like the DB (F-07 consistency).

### L-3 — Timeline actor binding trusts the `actor`/`user` field of API JSON without object-type discrimination

- **Severity:** LOW (GitHub is the system of record; this is robustness, not a known bypass).
- **Location:** `approval.py:68–70` (`_actor_login`), `review_monitor.py:184–185`.
- **Analysis:** `_actor_login` reads `event["actor"].login` or `event["user"].login`. For a `commented` timeline event the comment author is `event["actor"]` — correct. The owner check (`actor == fork_owner`) is the load-bearing test and it's exact-match. The minor robustness gap: it does not assert the event *type* matches the field it reads, so a malformed/mixed event shape degrades to `""`, which `_actor_valid` rejects (fail-closed) — so the failure direction is safe. Recorded for completeness.
- **Remediation:** None required; optionally assert event-shape per `event` type and audit-drop anything that doesn't parse, to avoid silently ignoring a malformed approval-bearing event.

### INFO

- **I-1:** `cmd_approve_sync` hardcodes `upstream_base_branch="main"` (cli.py:253) and graph-verify uses a passed `default_branch`; repos whose default branch isn't `main` will mis-target the upstream PR base. Correctness/operability, not security — flag to backend-engineer.
- **I-2:** `sync_outcome` classifies upstream-unavailable by substring-matching `"403"/"404"/"422"` in `str(exc)` (publisher.py:29,236). A repo/issue body or error text containing those substrings could misclassify; low impact (only changes KPI accounting). Prefer matching the structured status code.
- **I-3:** `get_repo_file` base64-decodes repo content with `errors="replace"` (github_gateway.py:204) and feeds it to the policy regex — fine — but note policy is a *heuristic* (acknowledged in policy.py docstring); a hostile repo can phrase an AI ban to evade `_RESTRICTIVE_PATTERNS`. The hard-skip list + merge-rate auto-pause are the real controls, as designed. No action; just don't treat policy regex as a security boundary.

---

## Part 3 — Verdicts

### (a) Sign-off conversion verdict: **CONDITIONS OUTSTANDING — remains SIGN-OFF-WITH-CONDITIONS (does NOT convert to FULL).**

| Cond | Requirement | Status |
|---|---|---|
| C-1 | "no HTTP client outside C5" lint rule, **CI-enforced + build-breaking**, positive test | **Outstanding.** Test + positive proof exist and pass; but (i) scanner bypassable via dynamic import / stdlib HTTP (H-1), (ii) no CI runs it as a gate (H-2). Both must close. |
| C-2 | audit cross-check matching rule (object-id membership + coarse fallback + fail-closed) | **MET as specified.** |
| C-3 | gateway-bypass residual documented | MET (doc); note H-1 makes it more reachable than implied. |
| C-4 | single-instance assumption documented | MET (doc). |
| C-5 | agent venv hash-pinned + verified; P5 accepted only thereunder | **Outstanding.** Hashed lockfile exists; `--require-hashes` enforcement absent (H-2). |

Two of the five conditions (C-1, C-5) are not closed as *implemented vs. specified*. Converting to FULL requires: harden the C-1 scanner against dynamic/stdlib HTTP (H-1), wire it into a merge-blocking CI lane (H-2), and make `--require-hashes` the enforced install path (H-2/C-5).

### (b) Release-gate recommendation: **CONDITIONAL GO for the mocked-lane MVP / NO-GO for closing the v2.1 security sign-off.**

- **GO** to ship the **mocked-lane MVP for evaluation** (no live token/Docker in this environment): the functional core is sound, parameterised SQL is clean, the sandbox argv is injection-free, the approval/audit/budget machinery is correct and fail-closed, and there is **no remote-exploitable CRITICAL/HIGH**. This matches QA's GO-for-mocked-lane.
- **NO-GO to mark the v2.1 sign-off FULL or to go live** until these blockers land (none require architecture change; all are bounded):
  1. **H-2 — add a merge-blocking CI lane** that runs the default pytest suite (incl. `test_no_client_outside_gateway.py`) **and** enforces `pip install --require-hashes` from `requirements.lock`. *(Closes the enforcement half of C-1 and C-5.)*
  2. **H-1 — harden the C-1 scanner** to flag dynamic imports (`importlib`/`__import__`) and stdlib HTTP roots (`urllib`/`http.client`/`socket`/`httpcore`), not just `httpx`/`githubkit`. *(Closes the implementation half of C-1.)*
- **Should-fix before live (not gate-blocking):** M-1 (deny-regex: add client-secret pattern / NFKC-normalise), M-3 (make git arg-safety explicit with `--` and input asserts). M-2, L-1..L-3, I-1..I-3 are hardening/operability.
- **Carry-forward (already correctly fenced):** the live-smoke obligations (AC-1/4/5) and the in-process-supply-chain residual remain accepted **only once C-5 enforcement (H-2) lands** — until then that residual is not legitimately out-of-scope, per the sign-off's own wording.

**Bottom line:** the engine is well-built and adversarially solid; ship it to the mocked-lane evaluation. But the v2.1 sign-off's two enforcement-shaped conditions (C-1 CI gate + scanner hardening, C-5 hash enforcement) are not yet met in-repo — so the sign-off stays conditional and a true production go-live is NO-GO until the two HIGH blockers close.

---

## Part 4 — Fix-pass disposition (ADDED POST-AUDIT by the backend-engineer fix pass, 2026-06-12 — NOT part of the original read-only audit)

| Finding | Disposition | Where |
|---|---|---|
| H-1 | **FIXED.** Scanner rewritten: stdlib HTTP/network roots banned (`urllib.request`, `http.client`, `socket`, `asyncio`, `ftplib`, `smtplib`, `poplib`, `imaplib`, `telnetlib`, `xmlrpc`, bare `urllib`/`http`), third-party transport roots banned even when uninstalled, and the dynamic-import MECHANISM banned outright (`importlib` imports, `__import__` references, `.import_module()`/`spec_from_file_location()`/`module_from_spec()` calls) — static analysis cannot resolve dynamic targets, so the mechanism is banned, not the target. Per-file allowances are relative-path-keyed and narrow (`github_gateway.py`→githubkit, `tokens.py`→httpx, `oauth.py`→socket). Negative tests cover all three audit bypass classes. The AST ceiling (eval/exec/getattr-chains/C extensions) is documented in the test docstring and is exactly what C-5 enforcement closes. | `tests/test_no_client_outside_gateway.py` |
| H-2 | **FIXED (in-repo; activates at first push).** `.github/workflows/ci.yml`: job 1 = `--require-hashes` install + full default pytest lane; job 2 = the C-1 scanner by explicit path (pytest exits non-zero if the file is missing or fully deselected). Action tags are major-version pinned with an in-file note to SHA-pin at first push (authored offline — SHAs unverifiable without network). Local equivalent: `scripts/check.ps1` (pre-push gate, README-documented) + `scripts/verify_lock.py` (offline lock conformance; the install-time-only nature of archive-hash verification is documented honestly in both). | `.github/workflows/ci.yml`, `scripts/check.ps1`, `scripts/verify_lock.py`, `tests/test_ci_config.py` |
| M-1 | **FIXED.** Outbound guard now (a) NFKC-normalizes + strips zero-width chars before the deny-regex (homoglyph/split evasion) and (b) exact-value-matches against every credential loaded in-process (registry in `outbound_safety.py`, fed by `tokens.py` keyring fetch/store, OAuth exchange, and `AnthropicLLMClient` construction) — this covers the GitHub OAuth client secret, which has no matchable prefix. Fail-closed; the value is never echoed in the error. Existing prefix patterns kept. | `src/outreach_agent/outbound_safety.py`, `llm_gateway.py`, `tokens.py` |
| M-2 | **FIXED (≤30 min, applied).** Command-body check now NFKC-normalizes + strips zero-width chars and refuses approval-class tokens ANYWHERE in the body (all four configured tokens), not just as a line prefix. The owner-repo refusal remains the load-bearing boundary, as the audit specified. | `src/outreach_agent/github_gateway.py` (`_assert_comment_capability`) |
| M-3 | **FIXED (≤30 min, applied).** Validate-then-pass made explicit: clone URL must match `^https://github\.com/<owner>/<repo>[.git]$`, branch must match `^agent/\d+-<slug>$` (asserted at prep AND at push), and `--` end-of-options separators inserted in `git clone` and `git push` (both forms verified accepted by local git, 2026-06-12). | `src/outreach_agent/prep.py`, `publisher.py` |
| L-1, L-2, L-3, I-1..I-3 | **NOT IN THIS PASS' SCOPE — remain open** as recorded above (low/hardening/operability). No acceptance is implied by this fix pass. | — |
