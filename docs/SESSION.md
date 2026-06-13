# Session Save — 2026-06-12

Project: outreach agent — automation agent that increases the user's tech
visibility via genuine, human-approved GitHub contributions (CLAUDE.md goal).

## STATUS: MVP shipped + full pipeline PROVEN end-to-end on real GitHub

The entire thesis is demonstrated live: agent discovered a real issue, wrote
a correct fix agentically (Claude Code) with regression tests, validated it
in a hardened Docker sandbox, committed under the user's noreply email,
opened a reviewable draft PR with AI disclosure — merged → lands on the
contribution graph.

## Repos (live, public, under Rutvik552k)

- **github.com/Rutvik552k/outreach-agent** — the product. main @ commit
  `4ccf47b`, tag `v0.1.0`. 863 tests (default lane) + 5/5 live Docker sandbox
  lane + opt-in real-agentic test. CI workflow present (dormant — account
  billing lock).
- **github.com/Rutvik552k/outreach-smoke-target** — throwaway test target.
  Issue #1 (slugify whitespace bug) → agent fix merged to main as commit
  `f3621782`, author `92663812+Rutvik552k@users.noreply.github.com`. Green
  square expected within ~24h of 2026-06-12.

## What the build went through (full SDLC, all in this project's chain)

Requirements → research → ADR-001 (architecture, v2.4 after gate-driven
revisions) → threat model → implementation (contracts C1–C8) → QA (7/7 AC,
6 defects fixed) → security audit (sign-off FULL-with-first-push-conditions)
→ release gate (GO, v0.1.0) → live-smoke (5 gaps found+fixed) →
ADR-002 (agentic fix-generation, security-signed-off) → loop closed.

Key docs: docs/adr/ADR-001-*.md (v2.4), docs/adr/ADR-002-fix-generation.md,
docs/security/{threat-model,v2.1-signoff,adr-002-signoff,audit-step6}.md,
docs/qa/acceptance-report.md, docs/release/go-no-go-v0.1.0.md,
docs/findings/smoke-fixgen-blocker.md, docs/delivery-plan.md (full gate
history). Research baseline:
~/.claude/agent-memory/research-agent/github-contribution-agent-sources.md

## Architecture (locked, ADR-001 v2.4 + ADR-002)

- Python + githubkit + keyring (Windows Credential Manager) + SQLite (WAL,
  %LOCALAPPDATA%). OAuth App + PKCE, scopes public_repo + user:email.
- Single GitHubGateway chokepoint (C5): every mutation = budget-authorize →
  audit intent → call → audit confirmed. Hash-chained audit + budget ledger.
- Approval = intra-fork draft PR + `agent:approve-upstream` label / `/approve`
  comment by the fork owner (actor-bound, C-2 cross-check). Agent CANNOT emit
  approval signals (structural incapability, C-1 lint + --safe-mode).
- C8 SandboxRunner: two-phase Docker (Phase R resolve network-on/exec-off,
  Phase X execute network-none). Per-stack images.
- Fix-gen (ADR-002): claude-code → agentic-in-clone (Read/Edit/Write, Bash/
  fetch disallowed, --safe-mode mandatory, config pre-stripped diff-neutral);
  anthropic → context-injection search/replace. Capture git diff.
- LLM backend default = Claude Code CLI (subscription, $0). claude-opus-4-8
  for anthropic backend. Merge-rate KPI auto-pause <35%.

## Live infra state (config_meta in %LOCALAPPDATA%\outreach-agent\state.db)

- github_login = Rutvik552k; user_emails = rutviksavaliya141@gmail.com,
  92663812+Rutvik552k@users.noreply.github.com (both bootstrapped manually —
  see gaps below).
- OAuth token (gho_) in keyring service 'outreach-agent' key
  'github_oauth_token'. client id/secret stored. bootstrap_pat removed.

## Resume points (none blocking; user-driven)

1. **~24h graph-credit check**: verify the green square for commit f3621782
   appeared. Agent graph-verify scans default-branch commits by author email
   — would now match.
2. **fork==upstream own-repo publish handler** — the one unbuilt path. Smoke
   used user's own repo; approve-sync's upstream-PR step would 422 (same
   repo/base/head as the draft). ALSO a real FR-5 profile-growth scenario
   (operates on own repos). Needs: own-repo case = draft IS the contribution,
   approval marks ready/merges, not a second PR. External-repo two-PR loop
   also untested (needs different-account target).
3. **auth-login gaps** (worked around live): cmd_auth_login now stores
   github_login (fixed), but user_emails was bootstrapped manually — consider
   a `GET /user/emails` C5 read at login to populate it (closed-set change,
   needs C4 re-review).
4. **classifier**: banned-marker now position-gated (fixed); was dropping
   genuine bugs mentioning whitespace/typo.

## USER to-dos (their side)

- Delete the broad bootstrap classic PAT on GitHub (Settings → Developer
  settings → Tokens classic) — admin-everything scopes, went through chat.
- Fix GitHub account billing lock (Actions jobs won't start) → then restore
  R-1 branch protection on outreach-agent/main (both CI jobs required,
  enforce_admins, no force-push) and SHA-pins are already in ci.yml.
  NOTE: branch protection was REMOVED by explicit user decision when the
  billing lock made required checks unsatisfiable.
- Optional: regenerate OAuth client secret (pasted in chat) + re-store via
  keyring.

## Lessons (also in agent-memory)

- Live-smoke finds what mocks can't: 5 real gaps (blind-LLM, unbuilt image,
  push auth, missing commit/attribution, dropped model field) all passed the
  mocked lane. Run it for real before trusting an integration.
- Background doc-revision subagents stalled twice (web-verification loops);
  recover transcript text from
  ~/.claude/projects/<proj>/<session>/subagents/agent-<id>.jsonl when the
  .output placeholder is empty.
- `--safe-mode` (not --setting-sources) is the real Claude Code containment
  control — it disables user MCP/hooks/CLAUDE.md.
