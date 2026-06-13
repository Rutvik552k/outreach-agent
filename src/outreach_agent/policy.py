"""Policy Pre-flight — component §2[2] (contract C2, FM5).

Fetches CONTRIBUTING.md / AI-policy files through C5 gateway reads, applies
the hard-skip list (curl-class restrictive repos, ADR §2[2] seed) and
restrictive-language heuristics. Verdicts cached with TTL 7 days; the TTL is
IGNORED at publish — approval.pre_publish_gate always calls recheck_policy.

The restrictive-language patterns are documented MVP heuristics: they catch
explicit "no AI / no external PR" statements; the merge-rate KPI and the
hard-skip list are the corrective controls for what they miss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .contracts import PolicyVerdict
from .persistence import Database, canonical_json, utc_now_iso

_POLICY_FILES = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    ".github/AI_POLICY.md",
    "AI_POLICY.md",
)

_RESTRICTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (reason, re.compile(pattern, re.IGNORECASE))
    for reason, pattern in (
        ("bans AI-generated contributions",
         r"\b(no|not accept\w*|never accept\w*|ban\w*|prohibit\w*|do not "
         r"(?:submit|use))\b[^.\n]{0,120}\b(ai|llm|language model|copilot|"
         r"machine[- ]generated)\b"),
        ("bans AI-generated contributions",
         r"\b(ai|llm|machine)[- ]generated\b[^.\n]{0,120}\b(not accepted|"
         r"rejected|banned|closed|prohibited)\b"),
        ("does not accept external PRs",
         r"\b(do(es)? not|don't|no longer|not currently) accept\w*\b"
         r"[^.\n]{0,80}\b(external|outside|unsolicited|community) "
         r"(pull requests|prs|contributions|patches)\b"),
        ("auto-closes external PRs",
         r"\b(pull requests|prs)\b[^.\n]{0,80}\b(auto[- ]?closed|"
         r"automatically closed|will be closed)\b"),
    )
)


def _hard_skip_reason(repo_full_name: str, config: Config) -> str | None:
    if repo_full_name.lower() in {r.lower() for r in config.hard_skip_repos}:
        return f"{repo_full_name} is on the hard-skip list (ADR §2[2] curl-class seed)"
    org = repo_full_name.split("/", 1)[0].lower()
    if org in {o.lower() for o in config.hard_skip_orgs}:
        return f"org {org!r} is on the hard-skip org list (ADR §2[2])"
    return None


def evaluate_policy(
    gateway: Any,
    *,
    repo_full_name: str,
    candidate_id: str,
    config: Config,
) -> PolicyVerdict:
    """Fresh evaluation — no cache. Used by both the TTL-cached pre-flight and
    the always-fresh publish-time recheck (FM5)."""
    now = datetime.now(timezone.utc)
    ttl = (now + timedelta(days=config.policy_ttl_days)).isoformat(timespec="seconds")
    checked_at = now.isoformat(timespec="seconds")

    skip = _hard_skip_reason(repo_full_name, config)
    if skip is not None:
        return PolicyVerdict(
            candidate_id=candidate_id, verdict="blocked", reasons=(skip,),
            sources_checked=("hard-skip list",), checked_at=checked_at,
            ttl_expires_at=ttl,
        )

    owner, repo = repo_full_name.split("/", 1)
    reasons: list[str] = []
    sources: list[str] = ["hard-skip list"]
    for path in _POLICY_FILES:
        content = gateway.get_repo_file(owner, repo, path)
        if content is None:
            continue
        sources.append(path)
        for reason, pattern in _RESTRICTIVE_PATTERNS:
            match = pattern.search(content)
            if match:
                reasons.append(f"{path}: {reason} ({match.group(0)[:80]!r})")
    return PolicyVerdict(
        candidate_id=candidate_id,
        verdict="blocked" if reasons else "cleared",
        reasons=tuple(reasons),
        sources_checked=tuple(sources),
        checked_at=checked_at,
        ttl_expires_at=ttl,
    )


def _persist(db: Database, verdict: PolicyVerdict) -> None:
    with db.transaction():
        db.conn.execute(
            "INSERT INTO policy_verdicts(candidate_id, verdict, reasons_json,"
            " sources_checked_json, checked_at, ttl_expires_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(candidate_id) DO UPDATE SET verdict=excluded.verdict,"
            " reasons_json=excluded.reasons_json,"
            " sources_checked_json=excluded.sources_checked_json,"
            " checked_at=excluded.checked_at, ttl_expires_at=excluded.ttl_expires_at",
            (verdict.candidate_id, verdict.verdict,
             canonical_json(list(verdict.reasons)),
             canonical_json(list(verdict.sources_checked)),
             verdict.checked_at, verdict.ttl_expires_at),
        )
        db.append_audit(
            actor="agent", phase="info", endpoint="policy:verdict",
            outcome={"candidate_id": verdict.candidate_id,
                     "verdict": verdict.verdict, "reasons": list(verdict.reasons)},
        )


def _cached(db: Database, candidate_id: str) -> PolicyVerdict | None:
    row = db.conn.execute(
        "SELECT * FROM policy_verdicts WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        return None
    import json

    return PolicyVerdict(
        candidate_id=row["candidate_id"], verdict=row["verdict"],
        reasons=tuple(json.loads(row["reasons_json"])),
        sources_checked=tuple(json.loads(row["sources_checked_json"])),
        checked_at=row["checked_at"], ttl_expires_at=row["ttl_expires_at"],
    )


def preflight(
    gateway: Any,
    db: Database,
    config: Config,
    *,
    repo_full_name: str,
    candidate_id: str,
) -> PolicyVerdict:
    """TTL-cached pre-flight check (C2). Cache hit within TTL → cached verdict;
    otherwise fresh evaluation, persisted."""
    cached = _cached(db, candidate_id)
    if cached is not None:
        expires = datetime.fromisoformat(cached.ttl_expires_at)
        if datetime.now(timezone.utc) < expires:
            return cached
    verdict = evaluate_policy(
        gateway, repo_full_name=repo_full_name, candidate_id=candidate_id,
        config=config,
    )
    _persist(db, verdict)
    return verdict


def recheck_policy(
    gateway: Any,
    db: Database,
    config: Config,
    *,
    repo_full_name: str,
    candidate_id: str,
) -> bool:
    """Publish-time re-check (FM5): TTL ignored, always fresh. Persisted so the
    audit trail shows the verdict that gated the publish."""
    verdict = evaluate_policy(
        gateway, repo_full_name=repo_full_name, candidate_id=candidate_id,
        config=config,
    )
    _persist(db, verdict)
    return verdict.verdict == "cleared"
