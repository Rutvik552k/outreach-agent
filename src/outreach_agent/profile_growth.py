"""Profile-Growth Engine — component §2[7] (FR-5, MVP per user decision; AC-7).

Produces three artifacts per run, all persisted in profile_actions and
surfaced via `report` and the `profile` CLI command:

(a) profile README improvement proposal — reads the user's profile repo
    README ({login}/{login}/README.md via the gateway contents read), drafts
    an improvement with the LLMGateway, stores it as a PROPOSAL artifact.
    Applying it (a README PR to the user's own profile repo) is a mutation
    and flows through the existing approval + publisher path — never here.
(b) pinned-repo recommendation — ranks the user's own repos by stars then
    recency from GET /user/repos (githubkit 0.15.5 rest/repos.py:21979,
    Repository fields models/group_0020.py). Pinning has NO REST endpoint
    (grep over the full githubkit REST surface: zero hits — GraphQL-only,
    ProfilePins), so per ADR Rule-1 fallback the output is recommendation
    TEXT the user applies manually. A recommendation needs no mutation.
(c) own-repo cadence plan — LLM-drafted weekly plan from repo activity data
    (pushed_at, open issues, topics). Only real work, no graph gaming (FR-5).

No GitHub mutation exists in this module; reads only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import Config
from .github_gateway import GitHubGateway
from .llm_gateway import LLMGateway
from .persistence import Database, canonical_json, new_ulid, utc_now_iso

KIND_README = "profile-readme-proposal"
KIND_PINNED = "pinned-repo-recommendation"
KIND_CADENCE = "repo-cadence-plan"

_README_SYSTEM = (
    "You improve GitHub profile READMEs. Keep claims factual and grounded in "
    "the repo data provided — never invent projects, stats, or employers. "
    "Output the full proposed README markdown, no preamble."
)
_CADENCE_SYSTEM = (
    "You plan a realistic weekly maintenance cadence for a developer's own "
    "GitHub repositories. Only real work items (issues, tests, releases, "
    "docs) — never activity for activity's sake or contribution-graph gaming. "
    "Output a markdown plan grouped by repo, no preamble."
)


def _insert_action(db: Database, *, kind: str, payload: dict[str, Any]) -> str:
    action_id = new_ulid()
    now = utc_now_iso()
    with db.transaction():
        db.conn.execute(
            "INSERT INTO profile_actions(action_id, kind, payload_json, state,"
            " created_at, updated_at) VALUES(?,?,?,'proposed',?,?)",
            (action_id, kind, canonical_json(payload), now, now),
        )
        db.append_audit(
            actor="agent", phase="info", endpoint="profile:action-proposed",
            outcome={"action_id": action_id, "kind": kind},
        )
    return action_id


def _own_source_repos(gateway: GitHubGateway) -> list[dict[str, Any]]:
    """The user's own, public, non-fork, non-archived repos."""
    return [
        r for r in gateway.list_own_repos()
        if not r.get("fork") and not r.get("archived") and not r.get("private")
    ]


def _repo_summary(repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": repo.get("full_name", ""),
        "description": repo.get("description") or "",
        "stars": int(repo.get("stargazers_count") or 0),
        "topics": list(repo.get("topics") or []),
        "open_issues": int(repo.get("open_issues_count") or 0),
        "pushed_at": str(repo.get("pushed_at") or ""),
    }


# -- (a) profile README improvement proposal ----------------------------------


def propose_profile_readme(
    *,
    db: Database,
    gateway: GitHubGateway,
    llm: LLMGateway,
    config: Config,
    login: str,
    repos: list[dict[str, Any]],
) -> str:
    profile_repo = f"{login}/{login}"
    current = gateway.get_repo_file(login, login, "README.md")
    repo_lines = "\n".join(
        f"- {s['full_name']}: {s['stars']}★ — {s['description']}"
        f" (topics: {', '.join(s['topics']) or 'none'})"
        for s in (_repo_summary(r) for r in repos[:20])
    ) or "- (no public source repos yet)"
    if current is None:
        situation = (
            f"The profile repo {profile_repo} has no README yet. Propose one "
            "from scratch."
        )
    else:
        situation = f"Current README of {profile_repo}:\n\n{current}\n\nImprove it."
    prompt = (
        f"GitHub user: {login}\nPublic repos:\n{repo_lines}\n\n{situation}"
    )
    proposal = llm.generate(
        purpose="profile-readme-proposal", system=_README_SYSTEM, prompt=prompt,
    )
    return _insert_action(db, kind=KIND_README, payload={
        "target_repo": profile_repo,
        "readme_exists": current is not None,
        "proposal_md": proposal,
        "note": "applying this is a README PR to the user's own profile repo "
                "and flows through the approval + publisher path (ADR §2[7])",
    })


# -- (b) pinned-repo recommendation (text-only — no REST pin endpoint) --------


def recommend_pinned_repos(
    *,
    db: Database,
    config: Config,
    repos: list[dict[str, Any]],
) -> str:
    # Most stars first; among equals, most recently pushed first.
    ranked = sorted(
        (_repo_summary(r) for r in repos),
        key=lambda s: (s["stars"], s["pushed_at"]),
        reverse=True,
    )
    picks = ranked[: config.pinned_repo_limit]
    lines = [
        "Pinning repos has no REST endpoint (GraphQL-only); apply manually at "
        "github.com → profile → Customize your pins:",
        "",
    ]
    for i, s in enumerate(picks, 1):
        lines.append(
            f"{i}. {s['full_name']} — {s['stars']}★, last push {s['pushed_at'] or 'n/a'}"
            f"{' — ' + s['description'] if s['description'] else ''}"
        )
    if not picks:
        lines.append("(no public source repos to pin yet)")
    return _insert_action(db, kind=KIND_PINNED, payload={
        "recommended": picks,
        "text_md": "\n".join(lines),
        "limit": config.pinned_repo_limit,
        "manual_apply_reason": "pin mutation is GraphQL-only; unconfirmable "
                               "from githubkit REST source (Rule 1 fallback)",
    })


# -- (c) own-repo cadence plan -------------------------------------------------


def plan_repo_cadence(
    *,
    db: Database,
    llm: LLMGateway,
    config: Config,
    login: str,
    repos: list[dict[str, Any]],
) -> str:
    summaries = [_repo_summary(r) for r in repos[: config.cadence_plan_repo_limit]]
    activity = "\n".join(
        f"- {s['full_name']}: last push {s['pushed_at'] or 'never'}, "
        f"{s['open_issues']} open issues, {s['stars']}★, "
        f"topics: {', '.join(s['topics']) or 'none'}"
        for s in summaries
    ) or "- (no public source repos)"
    prompt = (
        f"GitHub user: {login}\nOwn-repo activity:\n{activity}\n\n"
        "Draft a one-week maintenance cadence plan."
    )
    plan = llm.generate(
        purpose="repo-cadence-plan", system=_CADENCE_SYSTEM, prompt=prompt,
    )
    return _insert_action(db, kind=KIND_CADENCE, payload={
        "week_plan_md": plan,
        "repos_considered": [s["full_name"] for s in summaries],
    })


# -- orchestration (AC-7: all three artifacts per run) -------------------------


@dataclass(frozen=True)
class ProfileGrowthResult:
    readme_action_id: str
    pinned_action_id: str
    cadence_action_id: str


def run_profile_growth(
    *,
    db: Database,
    gateway: GitHubGateway,
    llm: LLMGateway,
    config: Config,
    login: str,
) -> ProfileGrowthResult:
    repos = _own_source_repos(gateway)
    readme_id = propose_profile_readme(
        db=db, gateway=gateway, llm=llm, config=config, login=login, repos=repos,
    )
    pinned_id = recommend_pinned_repos(db=db, config=config, repos=repos)
    cadence_id = plan_repo_cadence(
        db=db, llm=llm, config=config, login=login, repos=repos,
    )
    return ProfileGrowthResult(readme_id, pinned_id, cadence_id)


def list_profile_actions(db: Database, *, state: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT action_id, kind, payload_json, state, created_at"
           " FROM profile_actions")
    params: tuple[Any, ...] = ()
    if state is not None:
        sql += " WHERE state=?"
        params = (state,)
    sql += " ORDER BY created_at"
    rows = []
    for row in db.conn.execute(sql, params).fetchall():
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        rows.append(item)
    return rows
