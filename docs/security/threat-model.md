# Threat Model — Outreach Agent (Security-Engineer sign-off, chain step 2)

- **Date:** 2026-06-11
- **Status:** Review BEFORE implementation (SDLC phase 4 gate — architecture critique with facts, per CLAUDE.md Rule 4)
- **Input under review:** `docs/adr/ADR-001-outreach-agent-architecture.md` (Proposed), `docs/requirements.md` v0.2
- **Method:** Per CLAUDE.md Rule 1, every load-bearing security claim is grounded in a primary source cited inline. Objections cite evidence — no opinion-only findings.
- **Reviewer scope:** This is a design review of the ADR, not a code audit (no code exists yet). Verdict in §6 gates implementation.

---

## 0. Primary sources used (ground truth)

| # | Claim verified | Source |
|---|---|---|
| S1 | Applying/dismissing labels needs only **triage** access; create/edit/delete needs **write** | [Managing labels, GitHub Docs](https://docs.github.com/en/issues/using-labels-and-milestones-to-track-work/managing-labels) — verbatim "Anyone with triage access to a repository can apply and dismiss labels." / "Anyone with write access to a repository can create a label." |
| S2 | `public_repo` grants read/write to public-repo code but **NOT** workflow files; `workflow` is a separate scope | [Scopes for OAuth apps, GitHub Docs](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps) — `workflow` "Grants the ability to add and update GitHub Actions workflow files." |
| S3 | Pushing a workflow file without `workflow` scope is **rejected**; allowed only "if the same file exists on another branch in the repository" | [Scopes for OAuth apps, GitHub Docs](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps); error reproduced widely ([community #26254](https://github.com/orgs/community/discussions/26254), [cli/cli #7251](https://github.com/cli/cli/discussions/7251)) |
| S4 | **Windows Sandbox is NOT supported on Windows 11 Home** — Pro/Enterprise/Education only | [Windows Sandbox, Microsoft Learn](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/) — verbatim "Windows Sandbox is currently not supported on Windows Home edition." |
| S5 | Docker Desktop WSL2 backend runs on Windows Home, but requires hardware virtualization/SLAT (WSL2 pulls in a Hyper-V-based VM) | [Install Docker Desktop on Windows, Docker Docs](https://docs.docker.com/desktop/setup/install/windows-install/); [Docker WSL2 backend](https://docs.docker.com/desktop/features/wsl/) |
| S6 | On a personal-account fork you may grant maintainers push to your PR branch; org forks cannot. Outside parties do not get triage on your personal fork by default | [About permissions and visibility of forks, GitHub Docs](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/about-permissions-and-visibility-of-forks) |
| S7 | GitHub PKCE for OAuth added 2025-07-14, S256 only; client secret still required (community-confirmed) | ADR §4 citations: [GitHub changelog](https://github.blog/changelog/2025-07-14-pkce-support-for-oauth-and-github-app-authentication/), [community #15752](https://github.com/orgs/community/discussions/15752) |

---

## 1. Assets (what is worth protecting)

| ID | Asset | Why it matters | Impact if compromised |
|---|---|---|---|
| A1 | **GitHub OAuth user-to-server access token** | Acts AS the user with `public_repo` write on every public repo on GitHub (ADR §4) | Full impersonation: push code, open PRs, comment under the user's name across all of GitHub. Reputation + supply-chain blast radius. **Highest-value asset.** |
| A2 | **GitHub OAuth client secret** | Required in the token exchange even with PKCE (S7). Held in Credential Manager (ADR §4) | With an intercepted auth code, enables token minting for the user's OAuth App. Re-auth impersonation. |
| A3 | **Anthropic API key** | Billable spend; access to the user's Claude account | Financial loss (runaway spend), data exfil via attacker-controlled prompts. |
| A4 | **User's public reputation** (PRs/comments under their identity) | The entire product purpose is reputation; one malicious/slop PR under their name is a reputational + AUP event (ADR §1, §8) | Account flagged for spam/abuse; trust destroyed; possible GitHub AUP action. |
| A5 | **Local SQLite state + append-only audit log integrity** | Single source of truth for state machine, budget ledger, and the proof that "the human approved" (ADR §6, C6) | Tampering breaks exactly-once guarantees (double-publish), hides agent actions, or forges approval provenance. |
| A6 | **The host machine itself (Windows 11 Home)** | The agent clones and EXECUTES third-party repo code locally (test suites, linters) (ADR §2 component 3) | Arbitrary code execution → all of A1–A5 fall, plus the user's entire machine. **This is the largest attack surface — see B1.** |

---

## 2. Entry points / trust boundaries

Ordered by severity of the trust boundary crossed.

### B1 — [CRITICAL] Hostile third-party repo code executed locally
**Boundary:** untrusted internet repo → arbitrary code execution on the user's machine.
ADR §2 component 3 ("Prep Sandbox") clones the user's fork and **runs the repo's own test suite + linters in a subprocess**. This is the single biggest hole in the design. Test suites, `conftest.py`, `build.rs`, `package.json` lifecycle scripts (`preinstall`/`postinstall`), `Makefile`, tox/nox configs, and pytest plugins are **arbitrary code authored by strangers**. Running them is, by definition, RCE-as-designed.

Concretely on the four target stacks:
- **Node/React:** `npm install`/`npm ci` runs `preinstall`/`postinstall`/`prepare` scripts from every dependency — established malware vector (the entire typosquatting/supply-chain class). `npm test` runs arbitrary JS.
- **Python:** `pip install -e .` / `setup.py` executes arbitrary Python at install; `conftest.py` executes at pytest collection; tox runs arbitrary commands.
- **Rust:** `cargo build` runs `build.rs` (arbitrary code at build); `cargo test` runs arbitrary code.

**The ADR has NO contract covering this.** C1–C7 cover candidate shape, policy verdicts, prepared-contribution shape, approval, the GitHub gateway, audit, and budget — none isolate execution. ADR §7 prompt-safety protects the *LLM*, not the *host*. This is the #1 MUST-FIX (V1).

**Sandbox options realistic on Windows 11 Home (verified):**

| Option | Verdict on Win 11 Home | Evidence |
|---|---|---|
| **Windows Sandbox (WSB)** | ❌ **NOT AVAILABLE.** Pro/Enterprise/Education only | S4 — Microsoft Learn verbatim "not supported on Windows Home edition" |
| **Docker Desktop / WSL2 Linux container** | ✅ **Available and recommended.** Runs on Home via WSL2 backend; needs SLAT + hardware virtualization enabled in BIOS (WSL2 uses a lightweight Hyper-V VM even on Home) | S5 — Docker Docs |
| **Plain WSL2 distro (no Docker)** | ✅ Available on Home; gives a Linux VM boundary but weaker per-run disposability than a container; usable fallback | S5 |
| **Restricted local Windows user / Job Object** | ⚠️ Weak. A separate low-privilege Windows account limits blast radius but shares the kernel and (without extra ACL work) the network and parts of the filesystem; does not contain a determined exploit. Acceptable only as defense-in-depth, never as the primary boundary | — |

**Recommendation (cite-backed):** Primary boundary = **Docker container via WSL2** (S5), because (a) it is the only strong, disposable, kernel-isolated option that actually exists on Win 11 Home given WSB is excluded (S4), and (b) it maps cleanly to the four toolchains via language base images. Container hardening required: `--network=none` during dependency install and test runs (defeats exfil C-class and dependency-confusion phone-home), non-root user, read-only root FS + a writable tmp work-mount, dropped capabilities, CPU/memory/pids limits, and a wall-clock timeout (ADR already mandates explicit timeouts — resilience rule). Network may be re-enabled only for the explicit `git fetch`/dependency-resolve step if unavoidable, and never during arbitrary `test`/`build` execution. If Docker/virtualization cannot be enabled on the host, the **correct failure mode is to refuse to run untrusted tests**, not to fall back to bare-host execution.

### B2 — [HIGH] LLM prompt injection via repo-controlled text
**Boundary:** attacker-authored issue text / `CONTRIBUTING.md` / PR review comments → Claude prompt → generated code or drafted agent actions.
ADR §7 feeds "repo content, issue text, and diffs" into the LLM. All of that is attacker-controllable. Injection goals: (a) steer generated *code* to insert a backdoor or secret-exfil snippet; (b) steer drafted *PR text / review replies* to include attacker content; (c) attempt to make the agent take an action (the agent is human-gated, which blunts (c), but not (a)/(b)).
ADR §7 has a partial mitigation: **repo tests are the only trusted validator**, and an outbound deny-regex for secret patterns. That defends secret *leakage into prompts* but NOT malicious code *in the generated diff* — a backdoor that passes the repo's own tests sails through the CI-green gate and into the human review queue. Mitigated by the human approval gate (the diff is shown), but humans miss subtle backdoors. Partial gap — see V5.

### B3 — [HIGH] Malicious maintainer review comments
**Boundary:** upstream maintainer (or impersonator) review comment → Review Monitor → Claude draft.
Same injection surface as B2 via component 6. Lower severity because responses are human-gated before posting (ADR §7, requirements out-of-scope list). Covered partially by the same mitigation as B2.

### B4 — [MEDIUM] OAuth loopback redirect interception
**Boundary:** the `127.0.0.1` loopback HTTP server receiving the auth `code` (ADR §4).
Any local process can attempt to connect to the ephemeral loopback port and race for the `code`, or a malicious app could pre-bind. PKCE (S256) is exactly the defense-in-depth for this (ADR §4 states this correctly — S7). Residual: `state` validation and single-use, exact-port binding, and binding to `127.0.0.1` (not `0.0.0.0`) must be enforced in code. Covered in principle by ADR §4; flagged as an implementation-must (V6).

### B5 — [MEDIUM] SQLite state / audit-log tampering
**Boundary:** any local process or the user → the SQLite file (A5).
The DB is a plain local file. "Append-only audit log" (C6) is an application convention ("no UPDATE/DELETE statements exist in code", ADR §6) — it is **not** tamper-evident at rest. Any process with file access can rewrite history, forge an approval actor, or reset the budget ledger to bypass the 1-PR/day cap. Gap — see V4.

### B6 — [LOW] Anthropic key / spend
Covered by ADR §7 monthly cap + max-token limits + Credential Manager storage. Residual is financial only, bounded by the cap. Accepted.

---

## 3. Abuse cases

### AC1 — [CRITICAL] Agent tricked into opening a malicious PR under the user's identity
Chain: B2 injection or B1 execution writes attacker code into the diff → passes repo tests → CI-green gate → draft PR → **human approval** → upstream PR under user's name (A4 + A1).
The human approval gate (FR-3) is the backstop and it is correctly placed. But the threat model must be honest: the gate's strength is bounded by **how reviewable the diff is**. A large or obfuscated diff defeats human review. Mitigation hardening: cap diff size surfaced for one-click approval, force the human to view the full diff, and never let the agent's own token apply the approval label (see AC3). MUST-FIX-adjacent — see V5.

### AC2 — [CRITICAL] Secret exfiltration via generated code or test execution
Two paths: (a) B1 — a hostile test suite reads `%LOCALAPPDATA%`/Credential Manager/env and phones home during `npm test`/`cargo test`; (b) B2 — generated code contains an exfil snippet that runs when tests execute locally.
ADR §7's deny-regex scans *outbound prompts*, which does nothing for (a)/(b) — those are host-side execution, not prompt content. The **only** effective control is the B1 sandbox with `--network=none` and no access to the credential store from inside the container (the container must never mount Credential Manager or the keyring; secrets live on the host, outside the sandbox). This is why V1 (sandbox) is the linchpin: it is the control for both B1 and AC2. MUST-FIX — see V1.

### AC3 — [HIGH] Approval-label spoofing / confused-deputy on the approval gate
The approval signal is the GitHub label `agent:approve-upstream` on the **draft PR on the user's own fork** (C4). Verified facts:
- Applying a label needs only **triage** access (S1).
- The draft PR lives on the **user's personal fork**; base and head are both within that fork (ADR §4 component 4). By default, no outside party has triage on a personal fork (S6) — *unless* the user has added collaborators or enabled "allow maintainers to push to PR branch" (S6 notes personal forks can grant maintainer push; that grant is about branch push, but any added collaborator with triage could label).
- **The agent's own OAuth token has `public_repo` write (S2), which includes triage on the user's fork — so the agent itself is technically capable of applying `agent:approve-upstream`.** This is the real confused-deputy risk: a prompt-injected or buggy agent could self-approve.

Findings:
1. **The audit proof must bind the approval to a human actor.** C4 says "Audit log records the GitHub event id + actor login as proof." This must be enforced as: **reject any approval whose actor login == the agent's own authenticated login**, and require the actor to be the repo owner. Without that check, self-approval bypasses the entire human gate. MUST-FIX — see V2.
2. **Do not add collaborators to the fork**, and document that enabling maintainer-push or adding collaborators widens the approval-trust set (S6). Accepted-risk note in V7.
3. Label-event source must be verified server-side (poll the timeline/events API for the label-add actor), not inferred from PR state, because PR state can be changed by others. Covered by V2.

### AC4 — [HIGH] Audit-log tampering to hide actions or forge approval
Per B5, the audit log is append-only by convention only. An attacker (or a compromised agent post-B1) can rewrite the SQLite file to erase an unauthorized PR or fabricate an approval actor. Because the audit log is the evidence for acceptance criterion 3 ("audit log proves it"), its integrity is load-bearing. Gap — see V4.

### AC5 — [MEDIUM] Budget-ledger tampering to exceed 1-PR/day or rate caps
The `rate_budget` ledger is in the same tamperable SQLite file (B5). Resetting it bypasses the 1/day cap (A4 reputation risk) and the secondary-rate-limit caps (AUP risk). Same root cause and fix as V4.

---

## 4. Mitigations mapped to ADR contracts (C1–C7) — and GAPS

| Threat | Covered by | Adequate? | Gap |
|---|---|---|---|
| B1 hostile-repo RCE | — (none) | ❌ | **No contract isolates test execution.** Need a new contract **C8 — SandboxRunner** (V1). |
| B2/B3 prompt injection (code) | §7 "tests are the only trusted validator" + human gate (C4) | ⚠️ partial | Tests don't catch test-passing backdoors; need diff-review hardening (V5). |
| B2 prompt injection (secret-in-prompt) | §7 outbound deny-regex (fail-closed) | ✅ | Adequate for the leak-into-prompt path. Keep. |
| AC2 secret exfil via execution | — (deny-regex is prompt-only) | ❌ | Same root as B1: covered only once C8 sandbox with `--network=none` + no credential mount exists (V1). |
| B4 loopback interception | §4 PKCE S256 + state | ✅ design / ⚠️ impl | Design sound (S7); enforce exact-port, 127.0.0.1-only, single-use state in code (V6). |
| AC3 approval self-approval (confused deputy) | C4 (actor login recorded) | ❌ | C4 records actor but does not **reject agent-as-actor or non-owner actor**. Need explicit check (V2). |
| AC4 audit-log tampering | C6 (append-only by convention) | ❌ | Convention ≠ tamper-evidence. Need hash-chain / integrity (V4). |
| AC5 budget tampering | C7 + §5 ledger | ⚠️ | Transactional correctness is fine; at-rest tampering is not addressed. Same fix as V4. |
| Secret storage A1–A3 | §3 keyring → Credential Manager | ✅ | Adequate for local-first single-user. Keep; ensure container never mounts it (V1). |
| A1 token over-scope (workflow files) | §4 scopes `public_repo`+`user:email`, "never request workflow" | ⚠️ functional bug | Correct *security* choice, but creates a **publish-failure** when a fix touches `.github/workflows` (S3). Need a pre-publish guard (V3). |
| Exactly-once mutation | C5 intent/confirmed + F1 idempotency read | ✅ | Sound. Keep. |
| Rate/AUP abuse | §5 caps + C7 + §8 auto-pause | ✅ | Sound. Keep. |

---

## 5. Scope check — minimum OAuth scopes (verified)

**ADR's choice: `public_repo` + `user:email`. This is the correct minimum — confirmed.**

- `public_repo` (S2, verbatim): "read/write access to code, commit statuses, repository projects, collaborators, and deployment statuses for public repositories." This covers fork, push branch, create draft + upstream PRs, comment, reply to review comments — the entire mutation set in C5. ✅
- `user:email` (S2): "Grants read access to a user's email addresses." Needed to resolve the connected/noreply email for commit authorship (attribution depends on author email, not the token — requirements ground truth). ✅
- **`repo` correctly NOT requested** — would add private-repo write, out of scope and over-privileged (A1 blast-radius reduction). ✅
- **`workflow` correctly NOT requested** (S2) — the agent must never add/update GitHub Actions workflows; requesting it would massively widen A1 (workflow files run arbitrary code on GitHub's runners). ✅ Security-correct.

**But there is a verified functional consequence the ADR under-states (S2/S3):**
A `public_repo`-only token **cannot push a branch whose diff creates or updates a file under `.github/workflows/`** — GitHub rejects the push with *"refusing to allow an OAuth App to create or update workflow ... without `workflow` scope"* (S3). The push is allowed only if "the same file exists on another branch in the repository" (S2), i.e., the workflow content is unchanged from an existing branch. Since the agent **generates fixes** and some bug fixes legitimately touch CI config, the Publisher (component 5) **will fail at push time** for any such contribution. This is both:
- a **security-positive** (the agent genuinely cannot tamper with CI/workflows — keep the scope as-is), and
- a **correctness gap** that must be handled gracefully rather than crashing or, worse, prompting the user to broaden the scope (which would be a security regression).

**Required (V3):** Policy Pre-flight / Prep Sandbox must detect any diff touching `.github/workflows/**` and **hard-skip** that contribution with reason `workflow-file-touch-unsupported`, recorded as a decided outcome — never request `workflow` scope to "fix" it. Keep scopes at `public_repo` + `user:email`.

---

## 6. Verdict

### MUST-FIX BEFORE IMPLEMENTATION

| ID | Severity | Item | Why it blocks | Maps to |
|---|---|---|---|---|
| **V1** | **CRITICAL** | Add contract **C8 — SandboxRunner**: all untrusted repo execution (install, build, lint, test) runs inside a **Docker/WSL2 container** (Windows Sandbox is unavailable on Home — S4) with `--network=none` during install/test, non-root, read-only root FS, dropped caps, CPU/mem/pids limits, wall-clock timeout, and **no mount of Credential Manager / keyring / host secrets**. If virtualization is unavailable, refuse to run tests — never execute on bare host. | B1 + AC2 are RCE and secret-exfil by design; nothing in C1–C7 contains them. The host (A6) and all secrets (A1–A3) are exposed every run until this exists. | B1, AC2, §2 |
| **V2** | **CRITICAL** | Enforce approval-actor binding: reject any `agent:approve-upstream` whose label-add actor (from the events/timeline API) is **not the fork owner**, and explicitly reject the case where the actor login == the agent's own authenticated login. Make this a hard gate in the Approval Flow, not just an audit field. | The agent's own `public_repo` token can self-apply the label (S1+S2); a prompt-injected/buggy agent could self-approve and bypass the entire human gate (FR-3, the product's core safety invariant). | AC3, C4 |
| **V3** | **HIGH** | Detect any diff touching `.github/workflows/**` and hard-skip it (`workflow-file-touch-unsupported`); keep scopes at `public_repo`+`user:email`. Never broaden to `workflow` scope. | A `public_repo` token's push is **rejected** by GitHub for workflow-file create/update (S2, S3). Without this, Publisher crashes on such contributions; the "fix" of adding `workflow` scope would be a security regression (A1 blast radius). | §5, AC-N/A (correctness+scope) |
| **V4** | **HIGH** | Make the audit log + budget ledger **tamper-evident**: hash-chain each `audit_log` row (each row stores `prev_hash`; chain head persisted), verify the chain at startup, and alarm/halt on break. Treat acceptance-criterion-3 proof as integrity-protected. | C6's "append-only" is a code convention, not tamper-evidence (B5). Audit + budget integrity (A5) is load-bearing for proving the human approved (AC4) and for the 1/day cap (AC5). | B5, AC4, AC5 |

### SHOULD-FIX (high value, can land in first implementation pass)

| ID | Severity | Item | Maps to |
|---|---|---|---|
| **V5** | HIGH | Approval-gate hardening against test-passing backdoors: cap the diff size eligible for approval, require the reviewer to view the full diff (no approve-without-diff path), surface dependency/lockfile changes and any new network calls prominently in the draft PR risk-notes. | AC1, B2 |
| **V6** | MEDIUM | Loopback OAuth hardening in code: bind `127.0.0.1` only (never `0.0.0.0`), exact ephemeral port, single-use cryptographically-random `state` validated before code exchange, short timeout on the listener, shut the listener immediately after one request. | B4 |

### ACCEPTED RISKS (documented, not blocking)

| ID | Severity | Risk | Justification |
|---|---|---|---|
| R1 | MEDIUM | Fork pushes are public under the user's identity before upstream approval (FR-3 caveat). | Explicitly accepted in requirements; gate controls *upstream submission*, not fork visibility; private-mirror is phase 2. |
| R2 | LOW | Client secret stored locally (required even with PKCE, S7). | It is the user's own OAuth App; secret lives in the user's own Credential Manager; single-user local-first (NFR-4). PKCE is correctly used as defense-in-depth, not secret elimination. |
| R3 | LOW | Anthropic spend abuse via prompt manipulation. | Bounded by §7 monthly cap + per-call max-token limits. Financial-only, capped. |
| R4 | MEDIUM | Adding fork collaborators or enabling "allow maintainers to push to PR branch" widens the approval-trust set (anyone with triage can apply the approval label — S1, S6). | Document as operational guidance: keep the fork collaborator-free for the approval gate to remain single-actor. Revisit if the user needs collaborators. |

### Keep as-is (verified sound)
OAuth scope minimization `public_repo`+`user:email` (§5, S2); PKCE S256 design (§4, S7); secret storage in Credential Manager (§3); outbound prompt deny-regex fail-closed (§7); intent/confirmed exactly-once mutation (C5, F1); rate-budget + merge-rate auto-pause (§5, §8).

---

## 7. Gate decision

**Architecture critique result: CHANGES REQUESTED.** The design is fundamentally sound — the human approval gate, scope minimization, exactly-once mutation, and audit intent provide a strong skeleton. But **two CRITICAL gaps (V1 hostile-repo execution sandbox, V2 approval-actor binding) must be closed in the ADR before implementation begins**, because both defeat the product's central safety invariant ("nothing reaches upstream / runs without explicit human approval, and the host stays safe"). V3 and V4 are HIGH and must also be resolved in the ADR (V3 is a correctness blocker; V4 protects the very evidence the acceptance criteria depend on).

Recommend the ADR be revised to add contract **C8 — SandboxRunner**, extend **C4** with the actor-binding rule, add the `.github/workflows/**` hard-skip to **C2/C3**, and specify hash-chaining in **C6**. Re-review on the revised ADR before SDLC phase 5 (implementation).
