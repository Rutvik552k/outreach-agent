# Session Save — updated 2026-06-12

> **2026-06-12 update:** ADR v2 DONE (main session — background agents had
> stalled twice). Re-gate: **GATE PASS**, all 21 findings closed.
> backend-engineer (chain step 3) DISPATCHED: core engine per C1–C8,
> mocked-CI lane — scaffold, hash-chained persistence, state machine,
> BudgetTracker, GitHubGateway, SandboxRunner (fake + docker), diff checks,
> approval actor-binding, named gate tests. The "Next action" section below
> is superseded.

# Original save — 2026-06-11

Project: outreach agent — automation agent for tech-world visibility via
genuine GitHub contributions (see CLAUDE.md goal).

## Where we are in the chain

| Step | Status |
|---|---|
| 0. Ground-truth research | DONE — baseline at `~/.claude/agent-memory/research-agent/github-contribution-agent-sources.md` |
| 1. Architecture ADR v1 | DONE — `docs/adr/ADR-001-outreach-agent-architecture.md` (31 KB, contracts C1–C7) |
| Gate: architecture critique | DONE — PASS-WITH-CONDITIONS, `docs/critique/architecture-critique.md` (3 BLOCKER / 9 MAJOR / 3 MINOR) |
| 2. Threat model | DONE — CHANGES REQUESTED, `docs/security/threat-model.md` (V1–V4 must-fix) |
| **ADR v2 revision** | **NOT DONE — NEXT ACTION.** Two solution-architect attempts stalled in open-ended web-verification loops (54+ min, zero writes); both killed. ADR still v1. |
| 3. backend-engineer implementation | Blocked on ADR v2 |
| 4–7. fullstack → tests/QA → security-audit → release | Pending |

## Locked decisions (do not re-litigate)

- User decisions: Python/Rust/Node.js/React discovery; PR-draft-on-fork
  approval UX; 1 upstream PR/day; profile-growth engine in MVP.
- Stack: Python + githubkit + keyring (Windows Credential Manager), SQLite
  WAL, single process. OAuth App auth-code + PKCE (client secret kept —
  GitHub still requires it), device-flow fallback. Scopes:
  `public_repo` + `user:email`, NEVER `workflow`.
- Contribution types: lint/static-analysis real bug fixes (disclosed), test
  additions, issue triage, dep bumps where no Renovate. BANNED: typo/docs/
  whitespace PRs (spam-flagged). AI disclosure mandatory in PR text.
- KPI: merge rate. Auto-pause < 35% over trailing 10 decided PRs (min 5);
  instant pause on spam complaint / repo ban / 2 secondary-limit hits per 24h.
- LLM: claude-opus-4-8 config-pinned; repo tests+lint = only trusted
  validator of LLM output.

## Next action (resume here)

Run solution-architect to produce ADR v2 closing ALL gate findings. The full
revision prompt (requirements per finding ID) is embedded in
`docs/delivery-plan.md` "Gate results" section + the two gate docs. Key
anti-stall instruction proven necessary: **reuse citations already in the
critique/threat-model docs, hard cap on new web lookups (≤3), mark anything
else UNVERIFIED with implementation-time fallback**. Two prior attempts
ignored/never reached the write step — consider running revision in
FOREGROUND (not background) or doing it in the main session, splitting into
two passes: (1) write state-machine + contract changes from finding docs
only, no web; (2) optional verification pass after.

Must-close list for ADR v2:
- F-07: SQLite → `%LOCALAPPDATA%\outreach-agent\`, fail-fast on sync-root path.
- F-01/F-02: states `merged → graph-verify → graph-credited|graph-missing`
  (squash merge can strip attribution); graph-missing feeds repo scoring.
- F-03: intra-fork draft PR base, invariant `base.repo == head.repo == fork`
  (GitHub defaults base to upstream parent).
- F-04: two-PR model (fork draft + upstream PR, both budgeted; close draft
  on publish). F-05: atomic approval re-check at publish. F-08/F-09:
  SandboxRunner mock seam, mocked-CI vs live-smoke lanes. F-10: sandbox
  timeout + `sandbox-unfit` state. F-11: `upstream-unavailable` transition.
- V1: contract C8 SandboxRunner — Docker/WSL2, network-none, non-root,
  read-only FS, time/resource caps, no keyring mount; no Docker → refuse
  (NOTE: Docker Desktop = MVP prerequisite; Windows Sandbox unavailable on
  Win 11 Home).
- V2: approval label valid only if actor == fork owner AND != agent login.
- V3: `.github/workflows/**` diff → terminal `workflow-file-touch-unsupported`.
- V4: hash-chained audit log + budget ledger, startup verification, halt on
  break.
- githubkit `draft` param on create_pull: UNVERIFIED — backend-engineer
  confirms at implementation; fallback = direct REST call.

## Key files

- `CLAUDE.md` — project rules + goal (updated this session)
- `docs/requirements.md` v0.2 — locked requirements + resolved open items
- `docs/delivery-plan.md` v0.2 — chain, gate results, locked ADR decisions
- `docs/adr/ADR-001-outreach-agent-architecture.md` — v1 (needs v2 revision)
- `docs/critique/architecture-critique.md` — gate findings F-01..F-15
- `docs/security/threat-model.md` — V1–V6 + accepted risks
