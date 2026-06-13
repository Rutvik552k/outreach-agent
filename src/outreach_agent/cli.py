"""CLI entrypoint (ADR §3): discover / prepare / status / approve-sync /
report / profile / auth login / resume.

Startup sequence unchanged: config load → F-07 sync-root fail-fast → DB open →
FM12 chain verify → global-pause check. Network-backed commands construct the
githubkit client lazily through the C5 gateway only; the mocked CI lane tests
the command functions with fakes injected at the same seams.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any

from .budget import BudgetTracker
from .config import Config, load_config
from .errors import OutreachError
from .persistence import CHAIN_BREAK_PAUSE_PREFIX, Database
from .state_machine import ContributionStore, State


# DEF-006: read-only diagnostic commands allowed through a global pause (with
# a prominent banner). Everything mutating stays blocked.
READ_ONLY_COMMANDS = frozenset({"status", "report"})


def _is_chain_break_pause(reason: str) -> bool:
    # FM12/DEF-006 exception: integrity-failure pauses block even the
    # read-only commands — status/report would render numbers derived from
    # tampered rows as if they were facts, and operating on untrusted state
    # is worse than blindness. Only a minimal chain-status line is emitted.
    # persistence._pause_chain_break is the single writer of this prefix.
    return reason.startswith(CHAIN_BREAK_PAUSE_PREFIX)


def _force_utf8_io() -> None:
    """DEF-003: force UTF-8 on stdout/stderr so report literals (§ — ≥) render
    on cp1252 Windows consoles. `errors="replace"` keeps output lossless-safe
    on exotic terminals. Guarded: test harnesses swap in stream objects
    without ``reconfigure`` (e.g. pytest capsys)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="outreach-agent")
    parser.add_argument("--db-path", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    sub.add_parser("prepare")
    sub.add_parser("status")
    sub.add_parser("approve-sync")
    sub.add_parser("report")
    sub.add_parser("profile")
    sub.add_parser("resume")
    auth = sub.add_parser("auth")
    auth.add_argument("action", choices=["login"])
    return parser


def startup(db_path: Path | None = None) -> tuple[Config, Database]:
    """Config load → F-07 sync-root fail-fast → DB open → FM12 chain verify."""
    config = load_config(db_path=db_path)
    return config, Database(config.db_path)


def _build_gateway(db: Database, config: Config) -> Any:
    """Production wiring: keyring token → githubkit client → C5 gateway."""
    from .github_gateway import GithubkitClient, GitHubGateway
    from .tokens import KeyringTokenSource

    tokens = KeyringTokenSource()
    token = tokens.github_token()
    client = GithubkitClient(token, timeout_s=config.github_timeout_s)
    login = _resolve_login(db)
    return GitHubGateway(
        client, db, BudgetTracker(db, config), config,
        agent_login=login, fork_owner=login,
    )


def _resolve_login(db: Database) -> str:
    login = db.get_meta("github_login")
    if not login:
        raise OutreachError(
            "github_login not configured; set it once via "
            "`outreach-agent auth login` (stored in config_meta)"
        )
    return login


def _user_emails(db: Database) -> set[str]:
    """The user's connected/noreply emails (config_meta 'user_emails',
    comma/semicolon-separated, lowercased). Used by graph-verify to assert
    author-email credit; the raw CSV also feeds commit-author selection."""
    return {
        e.strip().lower()
        for e in (db.get_meta("user_emails") or "").replace(";", ",").split(",")
        if e.strip()
    }


def cmd_status(db: Database, config: Config) -> int:
    rows = db.conn.execute(
        "SELECT state, COUNT(*) AS n FROM contributions GROUP BY state ORDER BY state"
    ).fetchall()
    print("contribution states:")
    for row in rows:
        print(f"  {row['state']}: {row['n']}")
    if not rows:
        print("  (none)")
    today = db.conn.execute(
        "SELECT COUNT(*) AS n FROM rate_budget WHERE kind='upstream_pr'"
        " AND ts >= date('now')"
    ).fetchone()["n"]
    print(f"upstream PRs today: {today}/{config.upstream_pr_per_day}")
    pause = db.global_pause_reason()
    print(f"global pause: {pause or 'no'}")
    return 0


def cmd_report(db: Database, config: Config) -> int:
    from .report import build_report, render_report

    print(render_report(build_report(db, config), config))
    return 0


def cmd_resume(db: Database) -> int:
    reason = db.global_pause_reason()
    if not reason:
        print("no global pause active")
        return 0
    with db.transaction():
        db.clear_global_pause()
        db.append_audit(actor="user", phase="info", endpoint="cli:resume",
                        outcome={"cleared": reason})
    print(f"cleared global pause: {reason}")
    return 0


def cmd_discover(db: Database, config: Config) -> int:
    from .discovery import discover
    from .errors import GitHubReadError
    from .policy import preflight

    gateway = _build_gateway(db, config)
    candidates = discover(gateway, db, config)
    cleared = 0
    for candidate in candidates:
        # GAP-1(b): a transient read failure (e.g. one slow CONTRIBUTING.md
        # fetch) blocks THIS candidate for THIS run only and the loop
        # continues. It is deliberately NOT persisted to policy_verdicts —
        # preflight raises before its _persist step, so the 7-day TTL cache
        # is never poisoned by a retriable transport error.
        try:
            verdict_value = preflight(
                gateway, db, config,
                repo_full_name=candidate.repo_full_name,
                candidate_id=candidate.candidate_id,
            ).verdict
            suffix = ""
        except GitHubReadError as exc:
            verdict_value = "blocked"
            suffix = " — preflight-read-failed (retriable)"
            with db.transaction():
                db.append_audit(
                    actor="agent", phase="info",
                    endpoint="policy:preflight-read-failed",
                    outcome={"candidate_id": candidate.candidate_id,
                             "repo": candidate.repo_full_name,
                             "error": str(exc), "retriable": True},
                )
        if verdict_value == "cleared":
            cleared += 1
        print(f"  [{verdict_value:7}] {candidate.repo_full_name}"
              f"#{candidate.issue_number} score={candidate.score.total}{suffix}")
    print(f"{len(candidates)} candidates, {cleared} policy-cleared")
    return 0


def cmd_prepare(db: Database, config: Config) -> int:
    """Prepare the highest-scored cleared candidate (C8 sandbox mandatory),
    then submit it for approval: end state is draft-on-fork (GAP-3 — ci-green
    alone left submit_for_approval with no production caller)."""
    from .fix_generator import build_fix_generator
    from .prep import SystemGitRunner, prepare_contribution
    from .publisher import select_author_email, submit_for_approval
    from .sandbox import DockerSandboxRunner

    row = db.conn.execute(
        "SELECT c.* FROM candidates c"
        " JOIN policy_verdicts v ON v.candidate_id = c.candidate_id"
        " WHERE v.verdict='cleared'"
        " AND c.candidate_id NOT IN (SELECT candidate_id FROM contributions"
        "                            WHERE candidate_id IS NOT NULL)"
        " ORDER BY json_extract(c.score_json, '$.total') DESC LIMIT 1"
    ).fetchone()
    if row is None:
        print("no policy-cleared candidate available; run discover first")
        return 1

    gateway = _build_gateway(db, config)
    login = _resolve_login(db)
    repo = row["repo_full_name"].split("/", 1)[1]
    fork_full_name = f"{login}/{repo}"
    gateway.fork_repo(row["repo_full_name"].split("/", 1)[0], repo)

    store = ContributionStore(db)
    contribution_id = store.create(
        candidate_id=row["candidate_id"], repo_full_name=row["repo_full_name"],
    )
    store.transition(contribution_id, State.SCORED, reason="picked by prepare")
    store.transition(contribution_id, State.POLICY_CLEARED,
                     reason="pre-flight verdict cleared",
                     fields={"fork_full_name": fork_full_name})

    llm = _build_llm(db, config)  # NFR-7: backend-aware factory
    # NFR-3: push to the fork needs the keyring OAuth token (clone of the public
    # fork is unauth). The provider is read LAZILY by SystemGitRunner — only when
    # a push actually runs — so a missing token surfaces as a typed
    # CredentialError with remediation (KeyringTokenSource), never a traceback.
    from .tokens import KeyringTokenSource

    git = SystemGitRunner(token_provider=KeyringTokenSource().github_token)
    # ADR-002 §5: backend-selected fix generator (claude-code → Approach B
    # agentic-in-clone; anthropic → Approach A context-injection).
    fix_generator = build_fix_generator(config, git, llm=llm)
    sandbox = DockerSandboxRunner(
        image=config.image_for_stack(row["stack"]), cpus=config.sandbox_cpus,
        memory=config.sandbox_memory, pids_limit=config.sandbox_pids_limit,
    )
    work_root = Path.home() / ".outreach-agent" / "work"
    # ADR-002 §4: re-fetch the REAL issue title + body via the C5 gateway read
    # so fix generation is not blind (the candidates schema stores only the
    # URL). A Missing/None body is normalised to "" by the gateway.
    upstream_owner, upstream_repo = row["repo_full_name"].split("/", 1)
    issue = gateway.get_issue(upstream_owner, upstream_repo, row["issue_number"])
    result = prepare_contribution(
        db=db, store=store,
        llm=llm, fix_generator=fix_generator, sandbox=sandbox, git=git, config=config,
        contribution_id=contribution_id,
        fork_clone_url=f"https://github.com/{fork_full_name}.git",
        issue_title=issue["title"],
        issue_body=issue["body"],
        issue_number=row["issue_number"],
        issue_url=row["issue_url"],
        stack=row["stack"],
        work_root=work_root,
    )
    print(f"prepare: {result.state} — {result.detail}")
    if result.state != State.CI_GREEN or result.prepared is None:
        return 1

    # GAP-3: ci-green → draft-on-fork. submit_for_approval owns the full
    # publish-to-fork sequence — COMMIT the validated fix, push (prep never
    # pushes, so no double-push), then open the intra-fork draft PR. The draft's
    # base is the FORK's actual default branch (I-1 follow-up: never assume
    # "main"), resolved via the C5 read.
    #
    # ATTRIBUTION: the commit's AUTHOR email decides contribution-graph credit
    # (ADR-001 §2[5]/§6), so resolve it from the user's configured
    # connected/noreply emails. AttributionConfigError (typed, with remediation)
    # fires here if 'user_emails' is unset — the publish path never commits with
    # an unattributed identity.
    commit_author_email = select_author_email(db.get_meta("user_emails"))
    fork_default_branch = gateway.get_repo_default_branch(login, repo)
    draft_pr_number = submit_for_approval(
        db=db, store=store, gateway=gateway, git=git, config=config,
        contribution_id=contribution_id,
        prepared=result.prepared,
        fork_full_name=fork_full_name,
        fork_default_branch=fork_default_branch,
        upstream_full_name=row["repo_full_name"],
        commit_author_email=commit_author_email,
        commit_author_name=login,
        work_dir=work_root / contribution_id,
    )
    print(f"draft-on-fork: PR #{draft_pr_number} on {fork_full_name} "
          f"(base {fork_default_branch}) — review and approve on GitHub (C4)")
    return 0


def _build_llm(db: Database, config: Config) -> Any:
    """NFR-7: backend selection lives in build_llm_client. With the default
    llm_backend=claude-code no Anthropic API key is required — the
    missing-key CredentialError fires only when llm_backend=anthropic."""
    from .llm_gateway import LLMGateway, build_llm_client

    return LLMGateway(build_llm_client(config), db, config)


def cmd_approve_sync(db: Database, config: Config) -> int:
    from .policy import recheck_policy
    from .publisher import run_graph_verification, sync_approval, sync_outcome
    from .review_monitor import pending_review_drafts, run_review_monitor

    gateway = _build_gateway(db, config)
    store = ContributionStore(db)
    login = _resolve_login(db)
    handled = 0

    # I-1 (audit step 6): never assume "main" — resolve each upstream repo's
    # actual default branch via the C5 read, cached per repo within this run
    # so N drafts on the same repo cost a single lookup.
    default_branch_cache: dict[str, str] = {}

    def _upstream_default_branch(repo_full_name: str) -> str:
        if repo_full_name not in default_branch_cache:
            owner, repo = repo_full_name.split("/", 1)
            default_branch_cache[repo_full_name] = (
                gateway.get_repo_default_branch(owner, repo)
            )
        return default_branch_cache[repo_full_name]

    for row in db.conn.execute(
        "SELECT * FROM contributions WHERE state='draft-on-fork'"
    ).fetchall():
        prepared = json.loads(row["prepared_json"] or "{}")
        outcome = sync_approval(
            db=db, store=store, gateway=gateway, config=config,
            contribution_id=row["contribution_id"],
            fork_owner=login,
            fork_full_name=row["fork_full_name"],
            draft_pr_number=row["fork_draft_pr_number"],
            upstream_full_name=row["repo_full_name"],
            upstream_base_branch=_upstream_default_branch(row["repo_full_name"]),
            prepared_title=prepared.get("title", ""),
            prepared_body=prepared.get("body_md", ""),
            head_branch=row["branch"],
            policy_recheck=lambda r=row: recheck_policy(
                gateway, db, config,
                repo_full_name=r["repo_full_name"],
                candidate_id=r["candidate_id"],
            ),
        )
        print(f"  {row['contribution_id']}: {outcome.status} — {outcome.detail}")
        handled += 1

    rows = db.conn.execute(
        "SELECT * FROM contributions WHERE state IN ('upstream-open','review-loop')"
    ).fetchall()
    llm = _build_llm(db, config) if rows else None
    for row in rows:
        state = sync_outcome(
            db=db, store=store, gateway=gateway, config=config,
            contribution_id=row["contribution_id"],
            upstream_full_name=row["repo_full_name"],
            upstream_pr_number=row["upstream_pr_number"],
        )
        if state in (State.UPSTREAM_OPEN, State.REVIEW_LOOP):
            # §2[6]: poll review comments, draft responses, apply /approve-reply
            # signals, post approved replies, changes-requested re-entry.
            result = run_review_monitor(
                db=db, store=store, gateway=gateway, llm=llm, config=config,
                contribution_id=row["contribution_id"],
                upstream_full_name=row["repo_full_name"],
                upstream_pr_number=row["upstream_pr_number"],
                fork_owner=login,
            )
            print(f"  {row['contribution_id']}: now {state} — review monitor:"
                  f" {result.drafted} drafted, {result.posted} posted"
                  f"{', re-entered prep (changes-requested)' if result.reentered else ''}")
        else:
            print(f"  {row['contribution_id']}: now {state}")
        handled += 1

    # GAP-4: §6 graph-verify execution — run_graph_verification previously had
    # no production caller. It self-enforces the verify-after time (merged_at
    # + graph_verify_delay_h, the persistence shape sync_outcome wrote on
    # merge detection) and returns GRAPH_VERIFY when not yet due. A row still
    # in 'merged' means the merged→graph-verify transition did not land
    # (crash between the two transitions) — normalize it first, per §6.
    gv_rows = db.conn.execute(
        "SELECT * FROM contributions WHERE state IN ('merged','graph-verify')"
    ).fetchall()
    if gv_rows:
        user_emails = _user_emails(db)
        if not user_emails:
            # No verdict without ground truth: matching against a guessed
            # email set would mis-record graph-credited/graph-missing.
            print(f"graph-verify: {len(gv_rows)} contribution(s) pending but"
                  " config_meta 'user_emails' is not set — skipped. Store your"
                  " connected/noreply emails (comma-separated) under the"
                  " 'user_emails' key in config_meta.")
            with db.transaction():
                db.append_audit(
                    actor="agent", phase="info", endpoint="graph-verify:skipped",
                    outcome={"reason": "user_emails meta not set",
                             "pending": len(gv_rows)},
                )
        else:
            for row in gv_rows:
                cid = row["contribution_id"]
                if row["state"] == State.MERGED.value:
                    store.transition(
                        cid, State.GRAPH_VERIFY,
                        reason="resuming scheduled graph-verify (crash recovery)",
                    )
                state = run_graph_verification(
                    db=db, store=store, gateway=gateway, config=config,
                    contribution_id=cid,
                    upstream_full_name=row["repo_full_name"],
                    upstream_pr_number=row["upstream_pr_number"],
                    default_branch=_upstream_default_branch(row["repo_full_name"]),
                    user_emails=user_emails,
                )
                print(f"  {cid}: graph-verify — now {state}")
                handled += 1

    drafts = pending_review_drafts(db)
    if drafts:
        print(f"\npending review-response drafts ({len(drafts)}) — approve with"
              f" `{config.comment_approve_reply} <comment_id>` on the upstream PR:")
        for d in drafts:
            print(f"  {d['repo_full_name']}#{d['upstream_pr_number']}"
                  f" comment {d['upstream_comment_id']} by {d['author_login']}:")
            print(f"    >> {d['body'][:120]}")
            print(f"    draft: {d['draft_response'][:200]}")

    print(f"approve-sync: {handled} contribution(s) processed")
    return 0


def cmd_profile(db: Database, config: Config) -> int:
    """FR-5 / AC-7: produce the three profile-growth artifacts."""
    from .profile_growth import list_profile_actions, run_profile_growth

    gateway = _build_gateway(db, config)
    login = _resolve_login(db)
    llm = _build_llm(db, config)
    result = run_profile_growth(
        db=db, gateway=gateway, llm=llm, config=config, login=login,
    )
    by_id = {a["action_id"]: a for a in list_profile_actions(db)}
    for action_id in (result.readme_action_id, result.pinned_action_id,
                      result.cadence_action_id):
        action = by_id[action_id]
        print(f"\n=== {action['kind']} ({action_id}) ===")
        payload = action["payload"]
        text = payload.get("proposal_md") or payload.get("text_md") \
            or payload.get("week_plan_md") or ""
        print(text)
    print("\nprofile: 3 proposal(s) recorded in profile_actions (state=proposed).")
    print("Applying the README proposal is a mutation — it flows through the"
          " approval + publisher path, never automatically.")
    return 0


def cmd_auth_login(db: Database, config: Config) -> int:
    from .oauth import run_login_flow
    from .tokens import KeyringTokenSource, exchange_oauth_code, fetch_login

    tokens = KeyringTokenSource()
    client_id = tokens.oauth_client_id()
    client_secret = tokens.oauth_client_secret()
    minted: dict[str, str] = {}

    def _store(token: str) -> None:
        tokens.store_github_token(token)
        minted["token"] = token

    run_login_flow(
        config=config,
        client_id=client_id,
        exchange=lambda *, code, code_verifier, redirect_uri: exchange_oauth_code(
            client_id=client_id, client_secret=client_secret, code=code,
            code_verifier=code_verifier, redirect_uri=redirect_uri,
        ),
        open_browser=lambda url: webbrowser.open(url),
        store_token=_store,
    )
    print("GitHub token stored in Windows Credential Manager (NFR-3).")
    # GAP-2: resolve and persist github_login — _resolve_login() depends on
    # this meta key, and `auth login` previously never wrote it. set_meta is
    # an upsert, so a manual bootstrap row is overwritten cleanly.
    login = fetch_login(minted["token"])
    with db.transaction():
        db.set_meta("github_login", login)
        db.append_audit(
            actor="user", phase="info", endpoint="cli:auth-login",
            outcome={"github_login": login},
        )
    print(f"github_login set to {login!r} (config_meta).")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_io()
    args = build_parser().parse_args(argv)
    try:
        config, db = startup(args.db_path)
    except OutreachError as exc:
        print(f"refusing to start: {exc.problem.title}\n{exc.problem.detail}",
              file=sys.stderr)
        return 2
    try:
        pause = db.global_pause_reason()
        if pause and args.command != "resume":
            # `resume` stays exempt in both branches: it is the documented
            # un-pause, and on a *currently* broken chain startup() already
            # raised ChainIntegrityError above — so resume only ever executes
            # against state that re-verified at open.
            if _is_chain_break_pause(pause):
                # FM12: integrity-failure pause — block EVERYTHING, including
                # the read-only commands (rendering status/report from
                # untrusted state is worse than blindness). Minimal
                # chain-status line only.
                print(f"agent is globally paused: {pause}", file=sys.stderr)
                print(
                    "chain-status: audit hash-chain integrity failure (FM12) — "
                    "all commands blocked until the chain verifies at startup "
                    "and the pause is cleared via `outreach-agent resume`",
                    file=sys.stderr,
                )
                return 3
            if args.command in READ_ONLY_COMMANDS:
                # DEF-006: read-only diagnostics allowed, prominently bannered
                # so the operator can *read why* the agent is paused.
                banner = "!" * 64
                print(banner, file=sys.stderr)
                print(f"!! GLOBAL PAUSE ACTIVE: {pause}", file=sys.stderr)
                print("!! read-only view — all mutating commands are blocked;"
                      " clear with `outreach-agent resume`", file=sys.stderr)
                print(banner, file=sys.stderr)
            else:
                print(f"agent is globally paused: {pause}", file=sys.stderr)
                return 3
        if args.command == "status":
            return cmd_status(db, config)
        if args.command == "report":
            return cmd_report(db, config)
        if args.command == "resume":
            return cmd_resume(db)
        if args.command == "discover":
            return cmd_discover(db, config)
        if args.command == "prepare":
            return cmd_prepare(db, config)
        if args.command == "approve-sync":
            return cmd_approve_sync(db, config)
        if args.command == "profile":
            return cmd_profile(db, config)
        if args.command == "auth":
            return cmd_auth_login(db, config)
        print(f"unknown command {args.command}", file=sys.stderr)
        return 2
    except OutreachError as exc:
        print(f"{exc.problem.title}: {exc.problem.detail}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
