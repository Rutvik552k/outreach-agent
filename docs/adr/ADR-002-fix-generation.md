# ADR-002 — Fix-generation redesign (addendum to ADR-001 §7)

- **Date:** 2026-06-12
- **Status:** Proposed — supersedes the bare-unified-diff mechanism in ADR-001 §7 (Fix-generation row) and the `prep.py` `_PATCH_SYSTEM` / `git apply` flow. Does NOT supersede C8, V5, NFR-6, or the two-PR/approval model — those are retained and reinforced.
- **Inputs:** `docs/findings/smoke-fixgen-blocker.md` (live runs, captured output — cited as [SMOKE]); ADR-001 §7, C3, C8, NFR-7, V5; `docs/security/threat-model.md` B1/B2/B3/AC1/AC2/V5 (cited as [TM]); local `claude --help` (captured 2026-06-12, cited as [HELP]); installed source (`src/`, `.venv/.../githubkit/...`).
- **Decision owner:** solution-architect. **Security sign-off required before implementation** — see §3.

Every load-bearing claim cites [SMOKE], [HELP], [TM], or `file:line`. Claims that could not be confirmed locally are marked **UNVERIFIED** with a fallback.

---

## 1. Context — the confirmed blocker

[SMOKE] (live run against `Rutvik552k/outreach-smoke-target` issue #1, a real slugify whitespace bug):

- `prepare` failed at `git apply --check`: *"No valid patches in input"* ([SMOKE] lines 11–14).
- Call 1 returned **1045 chars of prose** (not a diff); Call 2 **timed out at 300 s** ([SMOKE] lines 17–19).

Four grounded root causes ([SMOKE] lines 21–38), each independently confirmed in source:

1. **The LLM is blind.** `cli.cmd_prepare` passes `issue_body=row["issue_url"]` (`cli.py:225`) — the URL string, never the issue text — and the `candidates` table has no body column (`persistence.py:27–37`: columns are `issue_url`, no `issue_body`). The prep prompt (`prep.py:240–247`) includes **zero repository file contents**. The model cannot emit byte-exact unified-diff context for files it has never seen.
2. **Bare-diff contract is fragile for any backend.** `prep.py:177–183` (`_PATCH_SYSTEM`) asks for a `git apply`-compatible unified diff with exact context lines and no source — near-impossible even with source.
3. **Claude Code's strength is disabled.** The NFR-7 backend (`llm_gateway.py:161–186`) runs `--tools ""`, `--disable-slash-commands`, `--no-session-persistence`, and a **neutral empty scratch cwd** (`_default_scratch_dir`, `llm_gateway.py:108–111, 185`). That is the deliberate prompt-injection containment decision — but it also forbids Claude Code from reading the repo or editing files, so it falls back to prose. [SMOKE] line 33–36.
4. **Latency.** `claude_cli_timeout_s` (default in [SMOKE]: 300 s) is marginal; one live call exceeded it ([SMOKE] line 19, 37–38).

The KPI is **merge rate** (ADR-001 §8). A real, repo-aware fix beats a blind diff: a diff that does not apply yields zero merges, so generation quality is on the critical path, not a nicety.

---

## 2. Verified Claude Code CLI capabilities (local install, 2026-06-12, zero web)

All quotes are verbatim from the local `claude --help` [HELP]. This determines what Approach B can and cannot rely on.

| Capability | Verbatim `--help` text | Bearing on this decision |
|---|---|---|
| Non-interactive | `-p, --print` "Print response and exit (useful for pipes)." | Both approaches use `-p`. Already in `_argv` (`llm_gateway.py:163`). |
| Structured output | `--output-format <format>` "...'json' (single result)..." (only works with --print) | Already used; JSON `result`/`usage` parsing confirmed live [SMOKE-adjacent, `llm_gateway.py:201–219`]. |
| Tool allow-list | `--tools <tools...>` "...Use \"\" to disable all tools, \"default\" to use all tools, or specify tool names (e.g. \"Bash,Edit,Read\")." | **Approach B enabler.** B needs file-edit tools ONLY — e.g. `--tools "Read,Edit,Write"` — never `Bash`, never network tools. |
| Fine-grained allow/deny | `--allowedTools, --allowed-tools <tools...>` and `--disallowedTools, --disallowed-tools <tools...>` "Comma or space-separated list of tool names to allow/deny (e.g. \"Bash(git *) Edit\")." | B can deny `Bash`/`WebFetch`/`WebSearch` explicitly as defence-in-depth on top of a narrow `--tools`. |
| Settings sourcing | `--setting-sources <sources>` "Comma-separated list of setting sources to load (user, project, local)." | **Secondary (belt-and-suspenders) control — NOT the containment linchpin.** Passing `--setting-sources user` (omitting `project,local`) means the **clone's `.claude/settings.json` and `.claude/settings.local.json` are NOT loaded**, so the clone cannot inject settings. **Correction (sign-off C-1 / PROBE-4b):** `--setting-sources user` does NOT by itself contain the *user-level* MCP servers (Gmail/Drive/Calendar) or LSP — a live probe proved those leak into the session, outside the `--tools` allowlist, when only `--setting-sources user` is set. The actual containment linchpin is **`--safe-mode`** (next row). |
| CLAUDE.md discovery | `--bare` "...skip ... auto-memory ... and CLAUDE.md auto-discovery." | **Strongest neutraliser of repo `CLAUDE.md`** — but `--bare` also states "OAuth and keychain are never read," which **breaks the subscription auth NFR-7 depends on** (`llm_gateway.py:135–138` already documents this). So `--bare` is NOT usable for the claude-code subscription backend. CLAUDE.md neutralisation must instead come from running in a controlled cwd (see §3) + `--setting-sources user`. **UNVERIFIED:** whether `--setting-sources user` alone suppresses a `CLAUDE.md` sitting in the cwd (memory vs settings are distinct subsystems) — `--help` does not state this. Fallback in §3. |
| Permission mode | `--permission-mode <mode>` choices "acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan". | B in `-p` is non-interactive; to let edits apply without a prompt it needs `acceptEdits` (edits only) — NOT `bypassPermissions` (which would also green-light Bash/network if those tools were ever enabled). Pair `--permission-mode acceptEdits` with a file-edit-only `--tools`. |
| Extra writable dirs | `--add-dir <directories...>` "Additional directories to allow tool access to." | If B runs with cwd = the clone, edits land in the clone with no `--add-dir`. `--add-dir` is only needed if cwd ≠ clone; B should set cwd = clone, so `--add-dir` is **not** required. |
| Safe mode | `--safe-mode` "...customizations (CLAUDE.md, skills, plugins, hooks, MCP servers ...) disabled..." but "Auth, model selection, built-in tools, and permissions work normally." | **PRIMARY containment control (sign-off C-1, MANDATORY, structurally non-removable).** This is the verified, auth-safe flag that disables the clone's CLAUDE.md/hooks/MCP/skills AND the *user-level* MCP servers + LSP that `--setting-sources user` alone leaves exposed (PROBE-4b). `--safe-mode` — not `--setting-sources user` — is what neutralises injection/exfil surfaces; `--setting-sources user` is retained only as secondary defence-in-depth. Preserves subscription auth (unlike `--bare`). |

**Network behaviour — explicitly NOT a CLI flag.** [HELP] exposes **no `--network`, `--offline`, or `--no-network` option** (full option list reviewed). Claude Code's own model calls require network (it is a hosted model). Therefore **"network off" for an agentic Claude Code run cannot mean the CLI process has no network** — the CLI must reach the Anthropic API to function. "Network off" can only be enforced for the *tools the model is allowed to run*: deny `Bash`, `WebFetch`, `WebSearch`, MCP — i.e. the model may read/edit local files and talk to its own API, but cannot execute repo code or fetch arbitrary URLs. This is a material correction to the [SMOKE] line-50–56 framing of Approach B ("network off") and is the crux of the threat-model delta in §3.

`--dangerously-skip-permissions` / `--allow-dangerously-skip-permissions` [HELP] "Recommended only for sandboxes with no internet access." — **explicitly NOT used**; B keeps `acceptEdits` + a closed tool set.

---

## 3. Decision — Hybrid: B-for-claude-code, A-for-anthropic, one backend-agnostic contract

**Chosen:** a **hybrid keyed on the backend already selected by `build_llm_client` (`llm_gateway.py:222–251`)**, behind a single new prep-facing capability so the abstraction stays clean (§5).

- **Backend `claude-code` → Approach B (agentic-in-clone).** Claude Code's verified strength is reading a repo and editing files [HELP `--tools`]. Run it with cwd = the clone, a **file-edit-only tool set** (`--tools "Read,Edit,Write"`, plus `--disallowedTools Bash WebFetch WebSearch` as defence-in-depth), `--permission-mode acceptEdits`, `--safe-mode` + `--setting-sources user` to neutralise the clone's CLAUDE.md/.claude/hooks/MCP while preserving subscription auth (§2). The model edits files in place; **prep captures the change with `git diff`** (the mechanism already exists — `prep.py:254`). No unified-diff round-trip; the [SMOKE] "No valid patches" failure class is eliminated by construction.
- **Backend `anthropic` → Approach A (context injection).** The API backend (`AnthropicLLMClient`, `llm_gateway.py:65–105`) has no agentic file tools. Feed it the **issue body (§4 fix) + the relevant repo files read from the clone** and ask for a **deterministic anchored edit format** (defined in §3.1), which prep applies. Not a raw unified diff — that is the fragile contract [SMOKE root-cause 2].

**Rationale, tied to the finding and to merge rate:**
- B plays to the backend's strength and removes the byte-exact-context requirement entirely (root causes 1–3 all dissolve when the agent can see and edit the files). This maximises the chance the patch is real → highest merge-rate expectation.
- A keeps the project's NFR-7 promise that the Anthropic backend remains a first-class fallback (`build_llm_client` supports both); it fixes blindness (root cause 1) by injecting context and fixes fragility (root cause 2) by replacing the raw-diff contract with an applied edit format.
- A single hybrid avoids forcing the API backend into agentic behaviour it cannot do, and avoids crippling Claude Code into prose-only. The **downstream gates are identical for both** (C8 sandbox CI-green + V5 human diff review), so the safety envelope does not branch.

**Why not B for both:** `AnthropicLLMClient` has no file-tool loop; making it agentic would mean building a tool-execution harness — out of scope and redundant when A suffices.
**Why not A for both:** it throws away Claude Code's native repo-editing, which is exactly the capability NFR-7 selected it for, and re-introduces an apply step that A's format must then make robust. For the claude-code backend, B is strictly less brittle.

### 3.1 Approach A edit format (anthropic backend) — anchored search/replace, applied by prep

Raw unified diff is banned (root cause 2). A returns a JSON array of edits; prep applies each as an exact, unique-anchored string replacement against the file content read from the clone:

```json
{
  "edits": [
    {
      "path": "relative/posix/path.py",
      "search": "<exact contiguous snippet that occurs EXACTLY ONCE in the file>",
      "replace": "<replacement snippet>"
    }
  ],
  "new_files": [ {"path": "tests/test_fix.py", "content": "<full file body>"} ]
}
```

Apply rules (prep, deterministic — no fuzzy matching):
- `search` must match **exactly once** in the current file bytes; **0 or >1 matches → reject the whole edit set** (state `error`, re-enterable). No line-number context, so the [SMOKE] context-line fragility cannot recur.
- Paths normalised and confined to the clone (reuse the `prep.py:43–45` URL-shape discipline: reject `..`, absolute paths, `.github/workflows/**` is still caught downstream by `run_diff_checks` `touches_workflow_files`, `prep.py:269`).
- After applying, prep runs `git diff` (`prep.py:254`) — from here both approaches converge on the identical C3/C8/V5 path.

---

## 3.2 Threat-model delta for Approach B (the chosen claude-code path)

B lets **untrusted repo content reach an agentic Claude Code run**. The [SMOKE] framing called this "network off, execution deferred"; §2 corrects it — the CLI process itself **must** have network (it calls the hosted model). Containment is therefore by *tool restriction + execution deferral*, not process network isolation. Mapping to [TM]:

| [TM] item | Under the OLD design (`--tools ""`, neutral cwd) | Under Approach B | Verdict |
|---|---|---|---|
| **B2/B3 prompt injection via repo content** | Repo content was NOT in the cwd, so injection rode only the issue text in the prompt. | Repo files + issue text now steer an agent that **edits files**. Injection can try to plant a backdoor in the diff. | **No worse than the pre-existing B2 residual** [TM §B2 lines 64–67]: even the old design's generated diff could contain a backdoor that passes tests. B does not add an *execution* path (see AC2 row). The new edit surface is contained by: edits confined to the clone; **no `Bash`/`WebFetch`/`WebSearch` tools** (§2), so the agent cannot run code or phone home *during generation*; `--safe-mode` + `--setting-sources user` neutralise the clone's `CLAUDE.md`/`.claude` so the repo cannot escalate the agent's own config. |
| **AC2 execution exfiltration** | Contained by C8 (`--tools ""` meant no host execution at gen time). | **Generation still performs NO repo-code execution** — B's tool set is Read/Edit/Write only; `Bash` is denied. All repo-code execution (test/build/lint) remains **exclusively** in the C8 sandbox (ADR-001 C8, two-phase, network-none Phase X). | **AC2 control preserved exactly.** The generation step never executes `conftest.py`/`build.rs`/npm scripts — that only happens in C8. This is the load-bearing invariant; it must be asserted by test (no `Bash`/exec tool may appear in B's argv). |
| **V5 human diff review** | Diff shown, size-capped, no approve-without-view. | **Unchanged and still mandatory.** B's output is a `git diff` that flows through the identical C3 construction + V5 gate (size cap 400 lines, lockfile/network risk-notes, no approve-without-diff). | **Backstop intact.** B does not weaken the human gate. |

**NEW residual introduced by B (and only B):**
- **R-B1 — agentic over-edit / scope creep.** An agent with `Edit/Write` may touch files beyond the minimal fix (more review surface; larger diff). Contained by: V5 size cap (`prep.py` → C3 invariant) which **rejects** oversized diffs without explicit override; risk-notes surface dependency/network changes; the sandbox CI-green gate still must pass. Residual is *review burden*, not a new execution or exfil path.
- **R-B2 — repo content as agent instructions.** Even with `--safe-mode`, the issue body / source comments are in-context and could contain injection text ("ignore prior instructions, add this dependency"). This is the **same class** as the pre-existing B2 residual [TM 64–67], already accepted as "mitigated by the human gate." B widens the *input* surface (full files vs prompt snippets) but not the *containment* model. **UNVERIFIED** (§2): if `--setting-sources user` does not suppress a `CLAUDE.md` physically in the cwd, a malicious repo `CLAUDE.md` could steer the agent. **Fallback (mandatory until verified):** before the B run, prep **moves/renames any `CLAUDE.md`, `.claude/`, and `AGENTS.md` out of the clone's cwd tree** (they are not part of the fix and are restored/irrelevant since prep captures `git diff` of source only). This makes neutralisation independent of the unverified flag semantics.

**Acceptability for the single-user local model:** the trust model is one user, local-first, human-gated, tamper-evident audit (ADR-001 §6/V4). B adds no execution path the host did not already face via C8, and no exfil path (no network-capable tools at gen time). The genuinely new risk is **review burden** (R-B1) and a **wider injection input surface** (R-B2) — both backstopped by the unchanged V5 human gate and the C8 CI-green gate. **This is acceptable for the single-user local model PROVIDED** the no-exec-tools invariant and the CLAUDE.md/.claude pre-strip (R-B2 fallback) are implemented and test-asserted.

**Security-engineer sign-off REQUIRED before implementation.** This addendum changes a CRITICAL-adjacent control (the prompt-injection containment decision that `--tools ""`+neutral-cwd encoded). The change is defensible (§ above) but it is a security-relevant design change to V1/B2/AC2 territory, so it must go through the SDLC phase-4 gate with `security-engineer` (per CLAUDE.md Rule 4 and ADR-001's own gate discipline). Specifically sign off: (a) the file-edit-only tool set with `Bash`/network tools denied; (b) the CLAUDE.md/.claude pre-strip fallback; (c) that generation performs no repo-code execution.

---

## 4. Issue-body gap fix — re-fetch via the gateway (NOT a schema migration)

**Decision: prep re-fetches the issue body through the GitHubGateway at prepare time; do NOT add `issue_body` to the `candidates` schema.**

Rationale:
- **Freshness + size.** The issue can be edited between discover and prepare; the body is large free text that bloats every candidate row even for candidates never prepared. The gateway read is one cheap call on the one candidate actually being prepared.
- **Ground-truth confirms the read exists.** `IssuesClient.get` → `GET /repos/{owner}/{repo}/issues/{issue_number}` → `Response[Issue]` (`.venv/.../githubkit/versions/v2022_11_28/rest/issues.py:1682–1717`); the `Issue` model carries `number: int`, `title: str`, and `body: Missing[Union[str, None]]` (`.venv/.../models/group_0057.py:47, 54, 55`). The default media type "Returns the raw markdown body. Response will include `body`" (issues.py:1707) — exactly what fix-generation needs.
- **`title` comes free** from the same read, fixing the second blindness bug: `cli.py:224` currently fabricates `issue_title=f"issue #{row['issue_number']}"`. The real title feeds both generation and `slugify` (`prep.py:156`, branch naming).

Implementation contract (backend-engineer):
- Add `GitHubGateway.get_issue(owner, repo, issue_number) -> {number, title, body}` to the **read** set of C5 (it is a read; it routes through the existing `_read` wrapper, `github_gateway.py:332–334` pattern; reads are not budgeted mutations).
- `cmd_prepare` (`cli.py:177–230`) derives `owner, repo` from `row["repo_full_name"]`, calls `get_issue`, and passes the **real** `issue_title` and `issue_body` into `prepare_contribution` — replacing `cli.py:224` and `cli.py:225`.
- `Missing`/`None` body → pass empty string; generation proceeds on title alone (degraded, not blocked).

---

## 5. Contract / state-machine implications — keep the backend abstraction clean

**The `LLMClient` protocol does NOT change.** `complete(model, system, prompt, max_tokens) -> LLMCompletion` (`llm_gateway.py:60–63`) stays as the text-generation primitive used by PR-text drafting, triage, and review replies (ADR-001 §7) — those are unaffected.

**A new, separate prep-facing capability is added for fix generation** so the diff-vs-edit difference does not leak into the generic text protocol:

```
FixGenerator (new Protocol, prep-facing)
  generate_fix(work_dir, branch, issue_title, issue_body, stack, config) -> None
      # MUTATES files in work_dir; returns nothing. Prep captures `git diff` after.
```

- **`ClaudeCodeFixGenerator` (Approach B):** invokes the CLI with cwd = `work_dir`, `--tools "Read,Edit,Write"`, `--disallowedTools Bash WebFetch WebSearch`, `--permission-mode acceptEdits`, `--safe-mode`, `--setting-sources user`, `-p --output-format json`. Pre-strips `CLAUDE.md`/`.claude/`/`AGENTS.md` from the cwd (R-B2 fallback). Edits files in place.
- **`AnthropicFixGenerator` (Approach A):** reads the relevant repo files from `work_dir`, calls the existing `LLMGateway.generate` with the §3.1 system prompt, parses the JSON edit set, applies anchored search/replace + new files into `work_dir`.
- **Selection mirrors `build_llm_client`** (`llm_gateway.py:222–251`): `config.llm_backend == "claude-code"` → B; `"anthropic"` → A. One factory, same switch already in the codebase.

**Prep state-machine change (`prep.py:240–254`):** replace the `llm.generate` → write `.patch` → `git apply --check` → `git apply` → `git diff` block with: `fix_generator.generate_fix(...)` → `git diff` (capture). The downstream is **unchanged**: `run_diff_checks` (`prep.py:268`), workflow-file hard-skip (`prep.py:269–276`), C8 sandbox run (`prep.py:284–290`), C3 `PreparedContribution` construction (`prep.py:332–342`), CI-green transition. `git apply` is removed entirely — the [SMOKE] failure site no longer exists.

- **C3 contract:** **unchanged.** `PreparedContribution` is built from `git diff` + sandbox result regardless of how the diff was produced. The construction invariants (CI-green, no-workflow-files, no-pure-line-ending, size-cap) all still apply and are the right gate for both approaches.
- **FM9 (`llm-blocked`) mapping:** B's `LlmUnavailableError`/timeout and A's parse/apply failures map to the existing `prep.py:255–266` handlers (timeout → revert to `policy-cleared`; budget → `llm-blocked`; apply failure → `error`, re-enterable). No new terminal states needed.

---

## 6. Classifier false-positive — scope the banned markers (MINOR follow-up)

**Finding** [SMOKE lines 70–77]: `discovery._BANNED_TITLE_MARKERS = ("typo", "whitespace", "image optimization", "image-optimization")` (`discovery.py:32`); `classify` drops any issue whose **title substring-matches** a marker (`discovery.py:69–71`). It dropped a **genuine bug** whose title merely contained "whitespace" (the seeded slugify bug).

**Recommended fix scope (minimal, targeted — not a redesign):**
- The banned-marker mechanism exists to drop **low-value contribution spam** (typo/whitespace-only/image-opt PRs — ADR-001 §2 Policy Pre-flight, [RB Hacktoberfest precedent]). Title-substring matching is too blunt: "whitespace" legitimately appears in real bug titles.
- **Recommendation:** narrow the signal from "marker appears anywhere in the title" to "the issue is *about* that change type." Two concrete options for backend-engineer (either acceptable; prefer the first):
  1. **Require the marker to denote the change type, combined with labels.** Drop only when a banned marker co-occurs with a low-value signal (e.g. label `good first issue` AND title is *only* the marker phrase, or the marker is the leading token like "Typo: …", "Whitespace fix in …"). A bug titled "slugify drops leading whitespace" is kept because "whitespace" is the *subject*, not the *change type*.
  2. **Move the gate from title-substring to whole-token + position** (marker as the first word or after a `:` prefix), which catches "Typo: …"/"Whitespace: …" spam titles without nuking descriptive bug titles.
- **Keep it MINOR / non-blocking** for the fix-gen redesign — it is an independent discovery-quality bug. It does NOT gate this ADR. File as a discovery follow-up; add a regression test asserting "slugify drops leading whitespace" classifies as a bugfix (not `None`).

---

## 7. Timeout / latency guidance for `claude_cli_timeout_s`

Ground truth [SMOKE lines 17–19, 37–38]: one bare-generation call **exceeded 300 s**; the prose call took long enough to be marginal.

- **Approach B is more work per call** (agentic: the model reads files, plans, edits — multiple internal turns) than a single text completion, so the timeout **must rise**, not fall. **Recommend `claude_cli_timeout_s` default = 600 s** for the claude-code/B path, config-pinned, with the rationale that an agentic repo edit is a multi-step operation. This stays well inside `SystemGitRunner`'s own 600 s git timeout (`prep.py:131`) and the C8 sandbox wall-clock (900 s default, ADR-001 C8) — generation, then a separate sandbox budget.
- **Do not unify** the gen timeout with `llm_timeout_s` (the anthropic per-call timeout, 120 s default — `config` per `llm_gateway.py:247`). They are different operations: A's API call is a single completion (120 s is fine), B's CLI run is agentic (600 s). Keep `claude_cli_timeout_s` and `llm_timeout_s` distinct (they already are — `llm_gateway.py:239` vs `:247`).
- **Timeout → retriable.** Both paths already map timeout to `LlmUnavailableError` → revert to `policy-cleared` (`llm_gateway.py:187–190`, `prep.py:259–262`), so a slow run re-enters cleanly rather than wedging the contribution. Keep that.
- **Streaming is optional, not required.** `--output-format json` (single result) is sufficient and already parsed; `stream-json` would only help a progress UI, which the batch CLI does not need. No change.

---

## 8. Decision summary

1. **Hybrid fix generation, keyed on the existing backend switch:** claude-code → **Approach B** (agentic edit in the clone, file-edit-only tools, capture `git diff`); anthropic → **Approach A** (context injection + anchored search/replace edit set). Same C3/C8/V5 downstream for both.
2. **Verified CLI flags for B** [HELP]: `--tools "Read,Edit,Write"` + `--disallowedTools Bash WebFetch WebSearch`, `--permission-mode acceptEdits`, `--safe-mode` + `--setting-sources user` (auth-safe CLAUDE.md/.claude neutraliser — `--bare` rejected because it kills subscription auth). **No `--network` flag exists**; "network off" = no exec/fetch *tools*, enforced by the tool set, with all repo-code execution still confined to C8.
3. **Threat-model delta:** B adds no execution or exfil path beyond the pre-existing B2 residual; the new residuals are review-burden (R-B1) and a wider injection input surface (R-B2), both backstopped by the unchanged V5 human gate and C8 CI-green gate. **Security-engineer sign-off required before implementation** (no-exec-tools invariant + CLAUDE.md pre-strip + edits-only).
4. **Issue-body gap:** prep **re-fetches** via a new `GitHubGateway.get_issue` read (githubkit `issues.get`, source-confirmed) — fixes both `issue_body` and the fabricated `issue_title`; no schema migration.
5. **Contracts:** `LLMClient` protocol unchanged; add a separate prep-facing `FixGenerator` capability; prep replaces the `git apply` block with `generate_fix(...)` → `git diff`. C3 and all C3 invariants unchanged.
6. **Classifier false-positive:** scope `_BANNED_TITLE_MARKERS` from title-substring to change-type/position/label-gated; MINOR, non-blocking, add a regression test.
7. **`claude_cli_timeout_s` → 600 s** for the agentic B path (kept distinct from `llm_timeout_s` = 120 s); timeout stays retriable → `policy-cleared`.

**Next per SDLC:** phase-4 security-engineer gate on this addendum (§3 sign-off items), then backend-engineer implements §4/§5/§6/§7 against ADR-001 contracts C3/C5/C8 (unchanged) plus the new `FixGenerator` capability and `get_issue` read.
