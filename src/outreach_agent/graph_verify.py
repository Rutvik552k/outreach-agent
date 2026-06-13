"""Graph-credit verification (ADR §6 graph-verify, F-01/F-02, §10.4 item 2).

PRIMARY mechanism: fetch the merged PR's merge_commit_sha, get the commit,
assert it is reachable on the default branch and author.email matches the
user's connected/noreply email.

FALLBACK (merge_commit_sha semantics under squash are UNVERIFIED per ADR
§10.4 — implemented behind this single interface, no further research): scan
recent default-branch commits for `(#<pr>)` in the subject (GitHub squash
convention) and assert the author email. Ambiguous → MANUAL verdict, which the
weekly reporter surfaces as a manual checklist item.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class GraphVerdict(StrEnum):
    CREDITED = "credited"
    MISSING = "missing"
    MANUAL = "manual-check-required"


@dataclass(frozen=True)
class GraphVerifyResult:
    verdict: GraphVerdict
    mechanism: str
    detail: str


class _Reads(Protocol):
    def get_pr(self, owner: str, repo: str, pull_number: int) -> Any: ...
    def get_commit(self, owner: str, repo: str, ref: str) -> dict[str, Any]: ...
    def list_commits(self, owner: str, repo: str, *, sha: str,
                     per_page: int = 50) -> list[dict[str, Any]]: ...


def _author_email(commit: dict[str, Any]) -> str:
    inner = commit.get("commit") or {}
    author = inner.get("author") or {}
    return (author.get("email") or "").lower()


def _subject(commit: dict[str, Any]) -> str:
    inner = commit.get("commit") or {}
    return ((inner.get("message") or "").splitlines() or [""])[0]


def verify_graph_credit(
    gateway: _Reads,
    *,
    upstream_full_name: str,
    pull_number: int,
    default_branch: str,
    user_emails: set[str],
    merge_commit_sha: str | None,
) -> GraphVerifyResult:
    owner, repo = upstream_full_name.split("/", 1)
    emails = {e.lower() for e in user_emails}

    if merge_commit_sha:
        commit = gateway.get_commit(owner, repo, merge_commit_sha)
        email = _author_email(commit)
        on_default = _commit_on_default_branch(
            gateway, owner, repo, default_branch, merge_commit_sha
        )
        if on_default and email in emails:
            return GraphVerifyResult(
                GraphVerdict.CREDITED, "merge_commit_sha",
                f"commit {merge_commit_sha[:12]} on {default_branch}, author={email}",
            )
        if on_default and email not in emails:
            return GraphVerifyResult(
                GraphVerdict.MISSING, "merge_commit_sha",
                f"commit on {default_branch} but author={email!r} not a connected email "
                "(squash attribution stripped — F-01)",
            )
        # merge_commit_sha did not resolve to a default-branch commit → fallback.

    matches = [
        c for c in gateway.list_commits(owner, repo, sha=default_branch, per_page=50)
        if f"(#{pull_number})" in _subject(c)
    ]
    if len(matches) == 1:
        email = _author_email(matches[0])
        if email in emails:
            return GraphVerifyResult(
                GraphVerdict.CREDITED, "default-branch-scan",
                f"squash commit matched (#{pull_number}), author={email}",
            )
        return GraphVerifyResult(
            GraphVerdict.MISSING, "default-branch-scan",
            f"squash commit matched (#{pull_number}) but author={email!r} not connected (F-01)",
        )
    return GraphVerifyResult(
        GraphVerdict.MANUAL, "default-branch-scan",
        f"{len(matches)} commits matched (#{pull_number}) on {default_branch} — "
        "ambiguous; manual verification checklist item for the weekly report",
    )


def _commit_on_default_branch(
    gateway: _Reads, owner: str, repo: str, default_branch: str, sha: str
) -> bool:
    recent = gateway.list_commits(owner, repo, sha=default_branch, per_page=50)
    return any((c.get("sha") or "") == sha for c in recent)
