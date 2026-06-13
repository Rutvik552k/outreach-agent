# Outreach Agent — Requirements (DRAFT v0.2)

Status: ground-truth research merged + user decisions recorded (2026-06-11).
Research baseline (cited sources): `~/.claude/agent-memory/research-agent/github-contribution-agent-sources.md`

## Vision

Automation agent that increases the user's visibility in the tech world via
genuine, automated GitHub contributions under the user's account. Strong and
realistic: every published artifact must survive maintainer review and GitHub
spam detection. Quality over volume. Merge rate — not PR count — is the
success metric.

## User decisions (locked 2026-06-11)

| Question | Decision |
|---|---|
| Target stacks for discovery | Python, Rust, Node.js, React |
| Approval UX | PR-draft-on-fork (see FR-3) |
| Contribution budget | Max 1 PR/day |
| Profile-growth engine (FR-5) | Included in MVP |

## Ground-truth constraints (from research — load-bearing)

- **Attribution**: a commit shows on the contribution graph ONLY if (a)
  authored with an email connected to the account or the GitHub noreply
  email, (b) lands on the default branch, (c) fork commits count only after
  PR merge into parent; ~24h delay.
  → fork → branch → PR → **merge** is mandatory; commit author email (git
  config), not the API token, decides attribution.
  [docs.github.com/.../troubleshooting-missing-contributions]
- **Auth**: OAuth App (authorization-code + PKCE, user-to-server token) so the
  agent acts AS the user. GitHub App attributes work to a bot (wrong for
  visibility); fine-grained PATs are unsuitable for unaffiliated OSS
  contribution (verify verbatim wording — open item).
- **Rate limits**: primary 5,000 req/hr (user token); the real constraint is
  secondary limits — content creation 80/min AND 500/hr, 900 REST pts/min,
  100 concurrent. PR-review creation endpoint explicitly warns of secondary
  limiting. 1 PR/day budget is far inside limits by design.
  [docs.github.com/.../rate-limits-for-the-rest-api]
- **Policy environment**: GitHub AUP §4 bans bulk/inauthentic automated
  activity; AI authorship per se is NOT banned platform-wide — bans are
  project-level (curl instant-ban for undisclosed AI slop; Ghostty, tldraw,
  Matplotlib restrict external/AI PRs; Hacktoberfest bans typo/whitespace/
  image-optimization PRs). One documented positive pattern: disclosed,
  accurate, AI-tool-found real bugs (curl/Stenberg praise).
- **Merge-rate data**: Dependabot ~54%; bot PRs 37% vs human 73%; autonomous
  agents 35–50% real-world acceptance; ~64% of agent-PR rejections are
  non-code (trust/convention) reasons → human gate + substantive
  explanations are the lever.

## Functional requirements

### FR-1 Opportunity discovery
- Search GitHub (advanced issue search, GA 2025-03) for candidates in
  Python/Rust/Node.js/React repos: `good first issue` / `help wanted`
  labels, reproducible bug reports, missing-test areas, stale issues
  needing triage.
- **Allowed contribution types** (research-backed): lint/static-analysis-
  surfaced REAL bug fixes (disclosed), test additions, issue
  triage/reproduction, dependency bumps where repo lacks Renovate/Dependabot.
- **Banned types** (spam-flagged): typo-only, whitespace, image-optimization,
  drive-by docs tweaks.
- Score candidates: repo health (activity, external-PR merge rate,
  maintainer responsiveness), difficulty fit, visibility payoff.
- **Policy pre-flight**: parse CONTRIBUTING.md / repo AI policy; hard-skip
  repos restricting AI or external PRs (curl-class lists maintained).

### FR-2 Contribution preparation
- Clone fork, reproduce issue where applicable, generate fix/tests.
- Run repo's own test suite + linters locally; CI-green is a precondition
  for entering the approval queue (Renovate merge-confidence pattern).
- Draft PR description following repo conventions, linked issue, and
  **first-class AI-assistance disclosure** — never hidden.

### FR-3 Human approval gate (HARD requirement) — PR-draft-on-fork UX
- Agent pushes branch to the user's fork and opens a **draft PR on the fork**
  (base = fork default branch) containing diff, proposed upstream PR text,
  risk notes, and policy-check results.
- User reviews on GitHub; explicit approval action (e.g., label/comment/
  approve) triggers the real upstream PR. Reject closes and records reason.
- **Caveat (accepted)**: pushing to a public fork already exposes commits
  publicly under the user's identity — gate controls *upstream submission*,
  not fork visibility. Private mirror option = phase 2.
- Audit log of every published action.

### FR-4 Publish & follow-through
- Fork → branch → commit (author email = user's connected/noreply email) →
  upstream PR via API on approval.
- Monitor review comments; draft substantive responses/fix-ups for approval;
  rebase on request; detect merge/close, record outcome.
- Enforce budget: max 1 upstream PR/day; merge-rate KPI tracked; sustained
  merge rate < threshold (set in ADR) auto-pauses discovery.

### FR-5 Profile growth engine (MVP per user decision)
- Own-repo cadence: project scaffolds, release notes, README/profile polish,
  pinned-repo suggestions.
- Contribution consistency planning — only real work, no graph gaming.

### FR-6 Reporting
- Weekly visibility report: PRs opened/merged, merge rate, response times,
  follower/star deltas, what worked.

## Non-functional requirements

- **NFR-1 Integrity**: no spam-class PRs (see FR-1 banned list), no fake
  commits. Merge rate is the tracked KPI with auto-pause.
- **NFR-2 Policy compliance**: GitHub AUP, secondary rate limits (80/min,
  500/hr content creation) enforced by client-side budget, per-repo rules.
- **NFR-3 Secrets**: OAuth token in OS keychain (Windows Credential Manager),
  never in code/logs. Minimal scopes; rotation supported.
- **NFR-4 Footprint**: single-user, local-first (CLI + scheduled runs).
- **NFR-5 Observability**: every GitHub mutation logged (timestamp, endpoint,
  outcome); rate-limit budget tracked from response headers.
- **NFR-6 LLM safety**: generated code/text must pass repo tests + lint
  before approval queue; prompts never contain secrets; AI disclosure
  mandatory in PR text.

## Acceptance criteria (MVP)

1. Agent surfaces ≥10 scored candidates from live GitHub data across the four
   target stacks, with banned-type and policy pre-flight filtering proven.
2. Agent prepares a complete contribution (branch, diff, PR text with AI
   disclosure) with repo tests passing locally.
3. Draft-PR-on-fork approval flow works; nothing reaches upstream without
   explicit approval action; audit log proves it.
4. Approved contribution lands as upstream PR via API, commits authored with
   user's connected email (verifiable on contribution graph post-merge).
5. Agent surfaces maintainer review comments within its polling interval.
6. Daily budget (1 PR/day) and secondary-rate-limit budget never exceeded in
   a full run (logged proof).
7. Profile-growth engine produces at least: profile README improvement
   proposal + pinned-repo recommendation + one own-repo cadence plan.

## Out of scope (MVP)

- Multi-platform outreach (LinkedIn, X, dev.to, conference CFPs).
- Multi-user/multi-account support.
- Fully autonomous publishing (no approval gate) — explicitly rejected.
- Automated review-response posting without approval.
- Private-mirror staging (fork-push visibility caveat accepted for MVP).

## Open items — ALL RESOLVED in ADR-001 (2026-06-11)

1. ~~Reply-to-review-comment endpoint~~ — research memo's path was WRONG.
   Actual: `POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies`
   (requires pull_number; top-level comments only; doc warns of secondary
   rate limiting). https://docs.github.com/en/rest/pulls/comments
2. ~~Fine-grained-PAT limitation~~ — verbatim confirmed: fine-grained PATs
   cannot contribute to public repos where user is not a member; "Only
   personal access tokens (classic) have write access for public
   repositories that are not owned by you." OAuth App confirmed correct.
3. ~~Merge-rate auto-pause threshold~~ — < 35% over trailing 10 decided PRs
   (min 5 outcomes); immediate-pause on spam complaint, repo ban, or 2
   secondary-limit hits/24h; un-pause manual. See ADR-001.
