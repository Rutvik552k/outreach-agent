"""GitHubGateway — contract C5. The single chokepoint for ALL GitHub calls.

Every mutation: budget.authorize → audit intent → call → audit confirmed|failed.
No other module may import the HTTP client. githubkit sits behind the
GitHubClient protocol so the mocked CI lane injects a fake at this seam (§12).

Ground sources:
- draft param: githubkit 0.15.5 installed source,
  githubkit/versions/v2022_11_28/rest/pulls.py:247 (verified 2026-06-12).
- reply endpoint signature: ADR §10.1,
  POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies.
- intra-fork base default pitfall: ADR C5 / community #11729.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .budget import BudgetTracker
from .config import Config
from .errors import (
    BudgetDeniedError,
    GitHubMutationError,
    GitHubReadError,
    IntraForkInvariantError,
    StructuralIncapabilityError,
)
from .outbound_safety import normalize_outbound_text
from .persistence import Database

# GAP-1(c): short backoff before the single idempotent-read retry on the
# RequestTimeout class (resilience rule: max sensible, not a storm).
# Module-level so tests can zero it via monkeypatch.
_READ_RETRY_BACKOFF_S = 2.0


@dataclass(frozen=True)
class PullRef:
    number: int
    node_id: str
    state: str
    draft: bool
    base_repo_full_name: str
    head_repo_full_name: str
    merged: bool = False
    merge_commit_sha: str | None = None
    html_url: str = ""


class GitHubClient(Protocol):
    """Thin typed surface over githubkit. Production impl: GithubkitClient."""

    def create_pull(self, owner: str, repo: str, *, title: str, head: str,
                    base: str, body: str, draft: bool) -> PullRef: ...
    def update_pull_state(self, owner: str, repo: str, pull_number: int,
                          *, state: str) -> PullRef: ...
    def get_pull(self, owner: str, repo: str, pull_number: int) -> PullRef: ...
    def create_fork(self, owner: str, repo: str) -> dict[str, Any]: ...
    def create_issue_comment(self, owner: str, repo: str, issue_number: int,
                             *, body: str) -> dict[str, Any]: ...
    def create_review_comment_reply(self, owner: str, repo: str, pull_number: int,
                                    comment_id: int, *, body: str) -> dict[str, Any]: ...
    def list_review_comments(self, owner: str, repo: str,
                             pull_number: int) -> list[dict[str, Any]]: ...
    def list_pr_reviews(self, owner: str, repo: str,
                        pull_number: int) -> list[dict[str, Any]]: ...
    def list_timeline_events(self, owner: str, repo: str,
                             issue_number: int) -> list[dict[str, Any]]: ...
    def list_repos_for_authenticated_user(self, *, type: str, sort: str,
                                          per_page: int) -> list[dict[str, Any]]: ...
    def get_commit(self, owner: str, repo: str, ref: str) -> dict[str, Any]: ...
    def list_commits(self, owner: str, repo: str, *, sha: str,
                     per_page: int) -> list[dict[str, Any]]: ...
    def search_issues(self, query: str) -> list[dict[str, Any]]: ...
    def get_repo_file(self, owner: str, repo: str, path: str) -> str | None: ...
    def get_repo_default_branch(self, owner: str, repo: str) -> str: ...
    def rate_headers(self) -> tuple[int | None, int | None]: ...


class GithubkitClient:
    """githubkit-backed implementation. Constructed lazily so the mocked CI
    lane never imports a live transport."""

    def __init__(self, token: str, timeout_s: float = 30.0) -> None:
        from githubkit import GitHub

        self._gh = GitHub(token, timeout=timeout_s)
        self._last_headers: dict[str, str] = {}

    def _capture(self, resp: Any) -> Any:
        headers = getattr(resp, "headers", None)
        if headers is not None:
            self._last_headers = dict(headers)
        return resp

    @staticmethod
    def _pull_ref(p: Any) -> PullRef:
        return PullRef(
            number=p.number,
            node_id=p.node_id,
            state=p.state,
            draft=bool(p.draft),
            base_repo_full_name=p.base.repo.full_name,
            head_repo_full_name=p.head.repo.full_name if p.head.repo else "",
            merged=bool(getattr(p, "merged", False)),
            merge_commit_sha=p.merge_commit_sha,
            html_url=p.html_url,
        )

    def create_pull(self, owner: str, repo: str, *, title: str, head: str,
                    base: str, body: str, draft: bool) -> PullRef:
        resp = self._capture(self._gh.rest.pulls.create(
            owner, repo, title=title, head=head, base=base, body=body, draft=draft,
        ))
        return self._pull_ref(resp.parsed_data)

    def update_pull_state(self, owner: str, repo: str, pull_number: int,
                          *, state: str) -> PullRef:
        resp = self._capture(self._gh.rest.pulls.update(owner, repo, pull_number, state=state))
        return self._pull_ref(resp.parsed_data)

    def get_pull(self, owner: str, repo: str, pull_number: int) -> PullRef:
        resp = self._capture(self._gh.rest.pulls.get(owner, repo, pull_number))
        return self._pull_ref(resp.parsed_data)

    def create_fork(self, owner: str, repo: str) -> dict[str, Any]:
        resp = self._capture(self._gh.rest.repos.create_fork(owner, repo))
        return resp.parsed_data.model_dump()

    def create_issue_comment(self, owner: str, repo: str, issue_number: int,
                             *, body: str) -> dict[str, Any]:
        resp = self._capture(
            self._gh.rest.issues.create_comment(owner, repo, issue_number, body=body)
        )
        return resp.parsed_data.model_dump()

    def create_review_comment_reply(self, owner: str, repo: str, pull_number: int,
                                    comment_id: int, *, body: str) -> dict[str, Any]:
        resp = self._capture(self._gh.rest.pulls.create_reply_for_review_comment(
            owner, repo, pull_number, comment_id, body=body,
        ))
        return resp.parsed_data.model_dump()

    def list_review_comments(self, owner: str, repo: str,
                             pull_number: int) -> list[dict[str, Any]]:
        resp = self._capture(self._gh.rest.pulls.list_review_comments(owner, repo, pull_number))
        return [c.model_dump() for c in resp.parsed_data]

    def list_pr_reviews(self, owner: str, repo: str,
                        pull_number: int) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews — githubkit
        0.15.5 rest/pulls.py:2729; PullRequestReview.state/user/body/id
        (models/group_0414.py)."""
        resp = self._capture(self._gh.rest.pulls.list_reviews(
            owner, repo, pull_number, per_page=100,
        ))
        return [r.model_dump() for r in resp.parsed_data]

    def list_timeline_events(self, owner: str, repo: str,
                             issue_number: int) -> list[dict[str, Any]]:
        resp = self._capture(self._gh.rest.issues.list_events_for_timeline(
            owner, repo, issue_number, per_page=100,
        ))
        return [e.model_dump() for e in resp.parsed_data]

    def list_repos_for_authenticated_user(self, *, type: str, sort: str,
                                          per_page: int) -> list[dict[str, Any]]:
        """GET /user/repos — githubkit 0.15.5 rest/repos.py:21979
        (list_for_authenticated_user; type/sort/per_page params confirmed);
        Repository model fields stargazers_count/topics/pushed_at/fork/archived
        per models/group_0020.py."""
        resp = self._capture(self._gh.rest.repos.list_for_authenticated_user(
            type=type, sort=sort, per_page=per_page,
        ))
        return [r.model_dump() for r in resp.parsed_data]

    def get_commit(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        resp = self._capture(self._gh.rest.repos.get_commit(owner, repo, ref))
        return resp.parsed_data.model_dump()

    def list_commits(self, owner: str, repo: str, *, sha: str,
                     per_page: int) -> list[dict[str, Any]]:
        resp = self._capture(self._gh.rest.repos.list_commits(
            owner, repo, sha=sha, per_page=per_page,
        ))
        return [c.model_dump() for c in resp.parsed_data]

    def search_issues(self, query: str) -> list[dict[str, Any]]:
        resp = self._capture(self._gh.rest.search.issues_and_pull_requests(
            q=query, advanced_search="true",
        ))
        return [i.model_dump() for i in resp.parsed_data.items]

    def get_repo_file(self, owner: str, repo: str, path: str) -> str | None:
        """GET /repos/{owner}/{repo}/contents/{path}; base64-decoded file content,
        None on 404 (githubkit 0.15.5 rest/repos.py:9619, ContentFile model)."""
        import base64

        from githubkit.exception import RequestFailed

        try:
            resp = self._capture(self._gh.rest.repos.get_content(owner, repo, path))
        except RequestFailed as exc:
            if getattr(exc.response, "status_code", None) == 404:
                return None
            raise
        data = resp.parsed_data
        content = getattr(data, "content", None)
        if not content:
            return None
        return base64.b64decode(content).decode("utf-8", errors="replace")

    def get_repo_default_branch(self, owner: str, repo: str) -> str:
        """GET /repos/{owner}/{repo} → FullRepository.default_branch (I-1 fix:
        the upstream PR base must be the repo's ACTUAL default branch, never an
        assumed "main"). Ground source: githubkit 0.15.5 installed source —
        rest/repos.py:1529 (`repos.get` → Response[FullRepository]),
        models/group_0187.py:93 (`default_branch: str = Field()`)."""
        resp = self._capture(self._gh.rest.repos.get(owner, repo))
        return resp.parsed_data.default_branch

    def rate_headers(self) -> tuple[int | None, int | None]:
        rem = self._last_headers.get("x-ratelimit-remaining")
        reset = self._last_headers.get("x-ratelimit-reset")
        return (int(rem) if rem is not None else None,
                int(reset) if reset is not None else None)


class GitHubGateway:
    def __init__(
        self,
        client: GitHubClient,
        db: Database,
        budget: BudgetTracker,
        config: Config,
        *,
        agent_login: str,
        fork_owner: str,
    ) -> None:
        self.client = client
        self.db = db
        self.budget = budget
        self.config = config
        self.agent_login = agent_login
        self.fork_owner = fork_owner

    # -- mutation wrapper (C5: authorize → intent → call → confirmed|failed) --

    def _mutate(
        self,
        *,
        category: str,
        kind: str,
        endpoint: str,
        contribution_id: str | None,
        call: Callable[[], Any],
        summary: str,
        object_id_of: Callable[[Any], str | None] | None = None,
        target: dict[str, Any] | None = None,
    ) -> Any:
        auth = self.budget.authorize(
            category, kind=kind, endpoint=endpoint, contribution_id=contribution_id,
        )
        if not auth.granted:
            raise BudgetDeniedError(
                f"{endpoint} denied: {auth.reason} (wait {auth.wait_seconds:.0f}s)"
            )
        target = target or {}
        with self.db.transaction():
            self.db.append_audit(
                actor="agent", phase="intent", endpoint=endpoint,
                contribution_id=contribution_id,
                outcome={"summary": summary, **target},
                rate_state=self.budget.rate_state(),
            )
        try:
            result = call()
        except Exception as exc:
            with self.db.transaction():
                self.db.append_audit(
                    actor="agent", phase="failed", endpoint=endpoint,
                    contribution_id=contribution_id,
                    outcome={"summary": summary, "error": str(exc), **target},
                    rate_state=self.budget.rate_state(),
                )
            raise GitHubMutationError(f"{endpoint} failed: {exc}") from exc
        remaining, reset = self.client.rate_headers()
        self.budget.record_rate_headers(remaining, reset)
        object_id = object_id_of(result) if object_id_of is not None else None
        with self.db.transaction():
            self.db.append_audit(
                actor="agent", phase="confirmed", endpoint=endpoint,
                contribution_id=contribution_id,
                outcome={"summary": summary, "result": _result_summary(result), **target},
                rate_state={"remaining": remaining, "reset": reset},
                github_object_id=object_id,
            )
        return result

    # -- reads ----------------------------------------------------------------
    #
    # GAP-1 (live-smoke): every read goes through _read(), which converts
    # githubkit transport errors into the typed, retriable GitHubReadError and
    # gives the timeout class exactly ONE retry with a short backoff (reads
    # are idempotent — one retry, never a storm). Ground truth: githubkit
    # 0.15.5 installed source, core.py:344-350 (sync `_request`:
    # httpx.TimeoutException → RequestTimeout, any other transport error →
    # RequestError) and response.py:103-106 (same wrapping on the parse path);
    # RequestFailed/RequestTimeout are RequestError subclasses
    # (exception.py:39,53).

    def _read(self, what: str, call: Callable[[], Any]) -> Any:
        """Wrap a C5 read. Lazy exception import keeps the mocked CI lane's
        module-import path transport-free, matching GithubkitClient."""
        from githubkit.exception import RequestError, RequestTimeout

        try:
            return call()
        except RequestTimeout:
            time.sleep(_READ_RETRY_BACKOFF_S)
            try:
                return call()
            except RequestError as exc:
                raise GitHubReadError(
                    f"{what} failed after one retry: {exc!r}"
                ) from exc
        except RequestError as exc:
            raise GitHubReadError(f"{what} failed: {exc!r}") from exc

    def search_issues(self, query: str) -> list[dict[str, Any]]:
        return self._read("search issues",
                          lambda: self.client.search_issues(query))

    def get_pr(self, owner: str, repo: str, pull_number: int) -> PullRef:
        return self._read(
            f"get PR {owner}/{repo}#{pull_number}",
            lambda: self.client.get_pull(owner, repo, pull_number))

    def list_review_comments(self, owner: str, repo: str,
                             pull_number: int) -> list[dict[str, Any]]:
        return self._read(
            f"list review comments {owner}/{repo}#{pull_number}",
            lambda: self.client.list_review_comments(owner, repo, pull_number))

    def list_pr_reviews(self, owner: str, repo: str,
                        pull_number: int) -> list[dict[str, Any]]:
        """Review states for changes-requested detection (§2[6], §6 re-entry)."""
        return self._read(
            f"list PR reviews {owner}/{repo}#{pull_number}",
            lambda: self.client.list_pr_reviews(owner, repo, pull_number))

    def list_own_repos(self) -> list[dict[str, Any]]:
        """Profile-Growth Engine read (§2[7]): the user's own repos only."""
        return self._read(
            "list own repos",
            lambda: self.client.list_repos_for_authenticated_user(
                type="owner", sort="pushed", per_page=100,
            ))

    def get_commit(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        return self._read(
            f"get commit {owner}/{repo}@{ref[:12]}",
            lambda: self.client.get_commit(owner, repo, ref))

    def list_commits(self, owner: str, repo: str, *, sha: str,
                     per_page: int = 50) -> list[dict[str, Any]]:
        return self._read(
            f"list commits {owner}/{repo}@{sha}",
            lambda: self.client.list_commits(owner, repo, sha=sha,
                                             per_page=per_page))

    def get_timeline_events(self, owner: str, repo: str,
                            issue_number: int) -> list[dict[str, Any]]:
        return self._read(
            f"get timeline {owner}/{repo}#{issue_number}",
            lambda: self.client.list_timeline_events(owner, repo, issue_number))

    def get_repo_file(self, owner: str, repo: str, path: str) -> str | None:
        return self._read(
            f"get file {owner}/{repo}:{path}",
            lambda: self.client.get_repo_file(owner, repo, path))

    def get_repo_default_branch(self, owner: str, repo: str) -> str:
        """I-1 (audit step 6): resolve the repo's actual default branch for the
        upstream PR base — repos with master/develop/trunk defaults would
        otherwise get a PR against a wrong or nonexistent base. READ only —
        no budget/audit, matching the other gateway reads."""
        return self._read(
            f"get default branch {owner}/{repo}",
            lambda: self.client.get_repo_default_branch(owner, repo))

    # -- idempotency read-check (FM1: never retry a mutation blind) -----------

    def mutation_landed(self, probe: Callable[[], bool]) -> bool:
        """Run a read-side probe to decide whether a mutation already landed
        before any retry is attempted."""
        try:
            return probe()
        except Exception:
            return False

    # -- mutations -------------------------------------------------------------

    def fork_repo(self, upstream_owner: str, upstream_repo: str,
                  *, contribution_id: str | None = None) -> dict[str, Any]:
        return self._mutate(
            category="content_creation", kind="fork_create",
            endpoint="POST /repos/{owner}/{repo}/forks",
            contribution_id=contribution_id,
            call=lambda: self.client.create_fork(upstream_owner, upstream_repo),
            summary=f"fork {upstream_owner}/{upstream_repo}",
            object_id_of=lambda r: str(r["id"]) if isinstance(r, dict) and "id" in r else None,
        )

    def create_draft_pr_on_fork(
        self,
        *,
        fork_full_name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        contribution_id: str | None = None,
    ) -> PullRef:
        """Intra-fork draft PR (F-03). GitHub defaults a fork PR's base to the
        upstream parent (community #11729) — base is explicit and the response
        is asserted intra-fork. Violation → abort + audit, never proceed."""
        owner, repo = fork_full_name.split("/", 1)
        pr: PullRef = self._mutate(
            category="content_creation", kind="fork_draft_pr",
            endpoint="POST /repos/{fork_owner}/{fork}/pulls",
            contribution_id=contribution_id,
            call=lambda: self.client.create_pull(
                owner, repo, title=title, head=head_branch, base=base_branch,
                body=body, draft=True,
            ),
            summary=f"intra-fork draft PR on {fork_full_name} ({head_branch} -> {base_branch})",
            object_id_of=lambda r: r.node_id if isinstance(r, PullRef) else None,
            target={"target_repo": fork_full_name},
        )
        if not (pr.base_repo_full_name == pr.head_repo_full_name == fork_full_name):
            with self.db.transaction():
                self.db.append_audit(
                    actor="agent", phase="failed",
                    endpoint="invariant:intra-fork(F-03)",
                    contribution_id=contribution_id,
                    outcome={
                        "summary": "intra-fork invariant violated; aborting",
                        "base_repo": pr.base_repo_full_name,
                        "head_repo": pr.head_repo_full_name,
                        "expected": fork_full_name,
                        "pr_number": pr.number,
                    },
                )
            raise IntraForkInvariantError(
                f"draft PR #{pr.number} violates intra-fork invariant: "
                f"base.repo={pr.base_repo_full_name!r}, head.repo={pr.head_repo_full_name!r}, "
                f"expected both == {fork_full_name!r}"
            )
        return pr

    def close_fork_draft_pr(self, *, fork_full_name: str, pull_number: int,
                            contribution_id: str | None = None) -> PullRef:
        owner, repo = fork_full_name.split("/", 1)
        return self._mutate(
            category="content_creation", kind="fork_draft_close",
            endpoint="PATCH /repos/{fork_owner}/{fork}/pulls/{pull_number}",
            contribution_id=contribution_id,
            call=lambda: self.client.update_pull_state(
                owner, repo, pull_number, state="closed",
            ),
            summary=f"close fork draft PR #{pull_number} on {fork_full_name}",
            object_id_of=lambda r: r.node_id if isinstance(r, PullRef) else None,
            target={"target_repo": fork_full_name, "target_issue": pull_number},
        )

    def create_upstream_pr(
        self,
        *,
        upstream_full_name: str,
        fork_owner_login: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        contribution_id: str | None = None,
    ) -> PullRef:
        """Second PR of the two-PR model (F-04): head='user:branch'."""
        owner, repo = upstream_full_name.split("/", 1)
        return self._mutate(
            category="content_creation", kind="upstream_pr",
            endpoint="POST /repos/{upstream_owner}/{upstream}/pulls",
            contribution_id=contribution_id,
            call=lambda: self.client.create_pull(
                owner, repo, title=title,
                head=f"{fork_owner_login}:{head_branch}",
                base=base_branch, body=body, draft=False,
            ),
            summary=f"upstream PR on {upstream_full_name} from {fork_owner_login}:{head_branch}",
            object_id_of=lambda r: r.node_id if isinstance(r, PullRef) else None,
            target={"target_repo": upstream_full_name},
        )

    def _assert_comment_capability(self, *, target_repo_full_name: str, body: str,
                                   operation: str) -> None:
        """Structural incapability (C4 v2.1, sign-off C-1): the gateway's comment
        surface is restricted to upstream repos and can never carry an
        approval-class command. There is intentionally NO label-add method at
        all — the agent's awaiting-approval marker lives in the draft PR
        title/body, never a label mutation (see C-2 coarse rule).

        M-2 hardening (audit step 6): the owner-repo refusal below is the
        load-bearing boundary. The command-body check is defence-in-depth and
        is deliberately conservative: the body is NFKC-normalized with
        zero-width characters stripped (so a fullwidth solidus or a
        zero-width-split command cannot evade it), and approval-class
        tokens are refused ANYWHERE in the body, not just as a line prefix."""
        owner = target_repo_full_name.split("/", 1)[0]
        if owner.lower() == self.fork_owner.lower():
            raise StructuralIncapabilityError(
                f"{operation} targeting {target_repo_full_name!r} refused: the gateway "
                f"cannot comment on repos owned by the fork owner {self.fork_owner!r} "
                "(no agent comment may ever land on a fork draft PR — C4 v2.1)"
            )
        normalized = normalize_outbound_text(body)
        for token in (self.config.comment_approve, self.config.comment_reject,
                      self.config.comment_approve_reply, self.config.comment_reject_reply):
            if token and token in normalized:
                raise StructuralIncapabilityError(
                    f"{operation} refused: body contains the approval-class command "
                    f"token {token!r} — the gateway is structurally incapable "
                    "of emitting approval signals (C4 v2.1, M-2)"
                )

    def comment(self, *, repo_full_name: str, issue_number: int, body: str,
                contribution_id: str | None = None) -> dict[str, Any]:
        self._assert_comment_capability(
            target_repo_full_name=repo_full_name, body=body, operation="comment",
        )
        owner, repo = repo_full_name.split("/", 1)
        return self._mutate(
            category="content_creation", kind="comment",
            endpoint="POST /repos/{owner}/{repo}/issues/{issue_number}/comments",
            contribution_id=contribution_id,
            call=lambda: self.client.create_issue_comment(
                owner, repo, issue_number, body=body,
            ),
            summary=f"comment on {repo_full_name}#{issue_number}",
            object_id_of=lambda r: str(r["id"]) if isinstance(r, dict) and "id" in r else None,
            target={"target_repo": repo_full_name, "target_issue": issue_number},
        )

    def reply_to_review_comment(self, owner: str, repo: str, pull_number: int,
                                comment_id: int, body: str,
                                *, contribution_id: str | None = None) -> dict[str, Any]:
        """ADR §10.1 corrected signature — pull_number is required."""
        self._assert_comment_capability(
            target_repo_full_name=f"{owner}/{repo}", body=body, operation="review reply",
        )
        return self._mutate(
            category="content_creation", kind="review_reply",
            endpoint="POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies",
            contribution_id=contribution_id,
            call=lambda: self.client.create_review_comment_reply(
                owner, repo, pull_number, comment_id, body=body,
            ),
            summary=f"reply to review comment {comment_id} on {owner}/{repo}#{pull_number}",
            object_id_of=lambda r: str(r["id"]) if isinstance(r, dict) and "id" in r else None,
            target={"target_repo": f"{owner}/{repo}", "target_issue": pull_number},
        )


def _result_summary(result: Any) -> dict[str, Any]:
    if isinstance(result, PullRef):
        return {"pr_number": result.number, "state": result.state, "draft": result.draft}
    if isinstance(result, dict):
        keep = {k: result[k] for k in ("id", "number", "full_name", "html_url") if k in result}
        return keep or {"type": "dict"}
    return {"type": type(result).__name__}
