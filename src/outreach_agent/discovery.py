"""Discovery — component §2[1] (FR-1, contract C1).

Queries GitHub advanced issue search (advanced_search=true is set by the C5
client per the GA 2025-03 ground truth in the ADR) across the four target
stacks, allowlist-first per the delivery plan. Scoring inputs include the
per-repo attribution outcome history (F-01): repos whose squash merges
stripped attribution are deprioritized.

Scoring weights are MVP heuristics over the issue payload — documented as
such; merge-rate KPI (§8) is the corrective feedback loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Config
from .contracts import Candidate, ContributionType, Score, Stack
from .persistence import Database, canonical_json, new_ulid, utc_now_iso

# Stack → advanced-issue-search language qualifier. `react` is not a GitHub
# language; the MVP heuristic searches TypeScript/JavaScript with a react
# keyword — documented limitation, refined by allowlist-first usage.
_STACK_QUERY: dict[str, str] = {
    "python": "language:python",
    "rust": "language:rust",
    "nodejs": "language:javascript",
    "react": "language:typescript react",
}

# ADR-002 §6 (classifier false-positive fix): the banned-marker mechanism
# drops low-value contribution SPAM (typo/whitespace-only/image-opt PRs — ADR
# §2 Policy Pre-flight). The old rule matched a marker ANYWHERE in the title,
# which nuked genuine bugs whose subject merely contains the word (live smoke:
# "Bug: slugify produces repeated hyphens for consecutive whitespace" was
# wrongly dropped). The fix narrows the signal from "marker appears anywhere"
# to "the marker denotes the CHANGE TYPE" — i.e. it is the leading token of
# the title (optionally after a "fix"/"fixing" verb), as in spam titles like
# "Typo in README", "Whitespace fix in utils.py", "Image optimization". A
# marker that is the *subject* of a descriptive bug title ("… for consecutive
# whitespace") is no longer matched, because it is not in change-type position.
_BANNED_TITLE_MARKERS = ("typo", "whitespace", "image optimization", "image-optimization")

# Change-type position: the marker is the first meaningful phrase of the title,
# optionally preceded by a "fix"/"fixing"/"fixes" verb and followed by a
# word-boundary (so "whitespace" matches "Whitespace: …" / "whitespace fix in …"
# / "fix whitespace in …" but NOT "… consecutive whitespace"). Anchored at the
# title start only — change-type lives in the leading position.
_BANNED_MARKER_RE = re.compile(
    r"^\s*(?:fix(?:es|ing)?\s+)?(?:"
    + "|".join(re.escape(m) for m in _BANNED_TITLE_MARKERS)
    + r")\b"
)


def _is_banned_change_type(title: str) -> bool:
    """ADR-002 §6: True only when a banned marker is in CHANGE-TYPE position
    (leading token, optionally after a fix-verb) — not when it is merely the
    subject of a genuine bug title."""
    return bool(_BANNED_MARKER_RE.match(title.lower()))


def _parse_allowlist(entries: tuple[str, ...]) -> list[tuple[str, Stack]]:
    parsed: list[tuple[str, Stack]] = []
    for entry in entries:
        repo, _, stack = entry.rpartition(":")
        if repo and stack in _STACK_QUERY:
            parsed.append((repo, stack))  # type: ignore[arg-type]
    return parsed


def build_queries(config: Config) -> list[tuple[str, Stack]]:
    """(query, stack) pairs; allowlist repos first (delivery plan), then
    stack-wide label searches."""
    queries: list[tuple[str, Stack]] = []
    for repo, stack in _parse_allowlist(config.discovery_allowlist):
        for label in config.discovery_labels:
            queries.append((f'repo:{repo} is:issue is:open label:"{label}"', stack))
    for stack, qualifier in _STACK_QUERY.items():
        for label in config.discovery_labels:
            queries.append(
                (f'is:issue is:open {qualifier} label:"{label}" comments:>0', stack)  # type: ignore[arg-type]
            )
    return queries


def _repo_full_name(item: dict[str, Any]) -> str:
    url = item.get("repository_url") or ""
    # https://api.github.com/repos/{owner}/{repo}
    parts = url.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def classify(item: dict[str, Any]) -> ContributionType | None:
    """Map an issue to an allowed contribution type (C1 — banned types are
    unrepresentable). Banned-marker titles return None and are dropped."""
    title = (item.get("title") or "").lower()
    if _is_banned_change_type(title):
        return None
    labels = {(l.get("name") or "").lower() for l in item.get("labels") or []}
    text = title + " " + " ".join(labels)
    if "dependency" in text or "bump" in text:
        return "dependency-bump"
    if "test" in text or "coverage" in text:
        return "test-addition"
    if "bug" in text or "fix" in text or "error" in text or "crash" in text:
        return "bugfix-static-analysis"
    return "issue-triage"


def attribution_history(db: Database, repo_full_name: str) -> float:
    """F-01: per-repo graph-credit outcome ratio. No history → 1.0 (benefit of
    the doubt); attribution-stripping repos trend toward 0."""
    rows = db.conn.execute(
        "SELECT k.graph_credit FROM kpi_outcomes k"
        " JOIN contributions c ON c.contribution_id = k.contribution_id"
        " WHERE c.repo_full_name = ? AND k.graph_credit IS NOT NULL",
        (repo_full_name,),
    ).fetchall()
    if not rows:
        return 1.0
    credited = sum(1 for r in rows if r["graph_credit"] == "credited")
    return credited / len(rows)


def score_item(item: dict[str, Any], *, attribution: float) -> Score:
    labels = {(l.get("name") or "").lower() for l in item.get("labels") or []}
    comments = int(item.get("comments") or 0)
    # Heuristics (documented MVP): maintainer responsiveness proxied by
    # comment activity, capped; difficulty fit from curated labels.
    repo_health = min(comments, 10) / 10.0
    difficulty_fit = 0.9 if "good first issue" in labels else (
        0.6 if "help wanted" in labels else 0.4)
    reactions = int((item.get("reactions") or {}).get("total_count") or 0)
    visibility_payoff = min(0.3 + 0.1 * min(reactions, 7), 1.0)
    total = round(
        0.3 * repo_health + 0.25 * difficulty_fit
        + 0.2 * visibility_payoff + 0.25 * attribution,
        4,
    )
    return Score(
        repo_health=round(repo_health, 4),
        difficulty_fit=difficulty_fit,
        visibility_payoff=round(visibility_payoff, 4),
        attribution_history=round(attribution, 4),
        total=total,
    )


def discover(gateway: Any, db: Database, config: Config) -> list[Candidate]:
    """Run discovery queries, classify, score, persist, return candidates
    ordered by total score."""
    seen: set[tuple[str, int]] = set()
    candidates: list[Candidate] = []
    for query, stack in build_queries(config):
        for item in gateway.search_issues(query)[: config.discovery_max_per_query]:
            repo = _repo_full_name(item)
            number = int(item.get("number") or 0)
            if not repo or not number or (repo, number) in seen:
                continue
            if item.get("pull_request"):
                continue  # search returns PRs too; issues only
            contribution_type = classify(item)
            if contribution_type is None:
                continue
            seen.add((repo, number))
            score = score_item(item, attribution=attribution_history(db, repo))
            candidates.append(Candidate(
                candidate_id=new_ulid(),
                repo_full_name=repo,
                issue_number=number,
                issue_url=item.get("html_url") or "",
                stack=stack,
                contribution_type=contribution_type,
                score=score,
                discovered_at=utc_now_iso(),
            ))
    candidates.sort(key=lambda c: c.score.total, reverse=True)
    with db.transaction():
        for c in candidates:
            db.conn.execute(
                "INSERT OR IGNORE INTO candidates(candidate_id, repo_full_name,"
                " issue_number, issue_url, stack, contribution_type, score_json,"
                " discovered_at) VALUES(?,?,?,?,?,?,?,?)",
                (c.candidate_id, c.repo_full_name, c.issue_number, c.issue_url,
                 c.stack, c.contribution_type,
                 canonical_json(c.score.__dict__), c.discovered_at),
            )
        db.append_audit(
            actor="agent", phase="info", endpoint="discovery:run",
            outcome={"candidates": len(candidates)},
        )
    return candidates
