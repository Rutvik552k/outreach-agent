# Live-smoke finding — fix-generation blocker (2026-06-12)

Status: BLOCKER for live-pilot (AC-2/4 end-to-end). Grounded in live runs.

## What happened

End-to-end smoke against a seeded repo (`Rutvik552k/outreach-smoke-target`
issue #1, a real slugify whitespace bug). Discovery ✓, policy ✓, fork ✓,
clone ✓, then `prepare` failed:

```
prepare: error — git apply --check ...aed3767....patch exited 128:
error: No valid patches in input (allow with "--allow-empty")
```

Captured raw LLM output (Claude Code backend) for the same prompt:
- Call 1: **1045 chars of PROSE** (contained `→` U+2192) — an explanation of
  the fix, NOT a bare unified diff.
- Call 2: **timed out at 300s** (LlmUnavailableError) — latency marginal.

## Root causes

1. **LLM is blind.** `cli.cmd_prepare` passes `issue_body=row["issue_url"]`
   (candidates schema stores URL only — flagged earlier as a known gap) and
   the prep prompt (`prep._PATCH_SYSTEM` + the generate call) includes **no
   repository file contents**. The model cannot produce a `git
   apply`-compatible unified diff with correct context lines for files it has
   never seen.
2. **Bare-diff contract is fragile for any backend.** Asking an LLM for a
   byte-exact unified diff (exact context, line numbers) without the source
   is near-impossible; even with source it is brittle.
3. **Claude Code's strength is disabled.** The NFR-7 backend runs with
   `--tools ""`, `--disable-slash-commands`, and a neutral scratch cwd (the
   prompt-injection containment decision). That deliberately prevents Claude
   Code from doing what it is best at: reading the repo and editing files
   agentically. So it falls back to prose.
4. **Latency.** `claude_cli_timeout_s=300` is marginal for a real generation;
   one live call exceeded it.

## Decision needed (solution-architect)

Redesign the fix-generation mechanism. Candidate approaches:

- **A — context injection (backend-agnostic):** the clone already exists in
  prep; read the issue body (fix the URL-only gap) + the relevant repo files,
  include them in the prompt, and ask for full-file replacements or an
  anchored edit format the agent applies deterministically (not a raw diff).
- **B — agentic-in-clone (plays to Claude Code):** run Claude Code INSIDE the
  cloned repo with file-edit tools enabled but **network off** and the
  clone's own settings/CLAUDE.md neutralized (`--setting-sources` excluding
  project/local), then capture `git diff`. Reconciles with the injection
  concern: generation only EDITS files (no execution — that stays in the C8
  sandbox), the change still passes the sandbox test gate and the V5
  human-diff review. Needs a threat-model pass on letting untrusted repo
  content into an agentic (but network-off, execution-deferred) Claude Code
  run.

Also fix regardless of approach:
- Pass the real issue body to prep (candidates schema or a fresh fetch).
- Raise/relax `claude_cli_timeout_s`; consider streaming/`--output-format json`
  usage already present.

## Smoke state to resume

- Repo `Rutvik552k/outreach-smoke-target` + issue #1 exist (good first issue,
  bug labels). Issue retitled to avoid the `whitespace` banned-marker
  false-positive (see below).
- Contribution row left in ERROR; reset to re-run after the redesign.

## Secondary finding — classifier false positive

`discovery._BANNED_TITLE_MARKERS` contains `"whitespace"` (to drop
whitespace-only spam PRs). It dropped a **genuine bug** whose title contained
"whitespace". Title-substring banning is too blunt. Follow-up: scope the
banned markers to PR-spam context or require the marker to be the change
*type*, not merely present in the title.
