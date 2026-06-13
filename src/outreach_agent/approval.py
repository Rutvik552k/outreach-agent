"""Approval protocol — contract C4 (V2 amended v2.1/v2.2, F-05, F-15).

Parses issue timeline events from the fork draft PR, applies the actor-binding
rule, and provides the atomic pre-publish gate. Label and `/approve` comment
are both first-class and verified identically (F-15).

Actor binding (V2 amended v2.1 — ADR rev. table, sign-off v2.1-signoff.md):
the original rule `actor.login != agent_oauth_login` is structurally
unsatisfiable (the OAuth token acts AS the user), so the binding is:
  1. Owner check: actor.login == fork_owner.
  2. Not-agent-originated — two layered controls:
     - Structural incapability (C-1): GitHubGateway has no label-add method
       and its comment surface refuses fork-owner targets and approval-class
       commands (enforced by tests/test_no_client_outside_gateway.py).
     - Audit cross-check (C-2), implemented here over the draft's full
       lifetime:
       * Comment signals — exact GitHub-object-id set membership. VERIFIED
         from installed githubkit 0.15.5 source: the comment-create mutation
         returns IssueComment.id ("Unique identifier of the issue comment",
         models/group_0055.py:33) and the timeline `commented` event carries
         the same id space (TimelineCommentEvent.id, models/group_0389.py:35).
       * Label signals — UNVERIFIED correlation: the label-add mutation
         returns list[Label] whose id is the repo label-definition id
         (models/group_0048.py:26), NOT the timeline LabeledIssueEvent.id
         (models/group_0374.py:28). Per ADR §10.4 the documented coarse
         fail-closed fallback applies: ANY agent-confirmed label mutation
         targeting the draft ⇒ the whole draft is ineligible.
       * Any ambiguity (missing object id, unparseable target) ⇒ fail-closed
         abort + audit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

from .config import Config
from .github_gateway import PullRef
from .persistence import Database

SignalKind = Literal["label", "comment"]


@dataclass(frozen=True)
class ApprovalSignal:
    kind: SignalKind
    actor: str
    event_id: str
    detail: str


@dataclass(frozen=True)
class RejectionSignal:
    kind: str
    actor: str
    event_id: str
    detail: str


@dataclass(frozen=True)
class SignalScan:
    approval: ApprovalSignal | None
    rejection: RejectionSignal | None
    violations: tuple[str, ...]  # actor-binding violations, audited by caller


def _actor_login(event: dict[str, Any]) -> str:
    actor = event.get("actor") or event.get("user") or {}
    return actor.get("login", "") if isinstance(actor, dict) else str(actor)


def _event_id(event: dict[str, Any]) -> str:
    return str(event.get("id") or event.get("node_id") or "")


def _actor_valid(actor: str, *, fork_owner: str) -> bool:
    # v2.1: owner check only. The not-agent-originated half is the structural
    # incapability (C-1) + the C-2 audit cross-check, not a login comparison.
    return bool(actor) and actor == fork_owner


def scan_timeline(
    events: list[dict[str, Any]],
    *,
    fork_owner: str,
    config: Config,
) -> SignalScan:
    """Scan fork-draft-PR timeline events for approval/rejection signals.

    Both signal paths (label, slash-comment) run the identical actor check
    (F-15). Signals whose actor fails binding are recorded as violations and
    NEVER counted as approval (V2).
    """
    approval: ApprovalSignal | None = None
    rejection: RejectionSignal | None = None
    violations: list[str] = []
    label_active = False
    label_event: dict[str, Any] | None = None

    for event in events:
        etype = event.get("event")
        actor = _actor_login(event)

        if etype == "labeled" and (event.get("label") or {}).get("name") == config.label_approve:
            if _actor_valid(actor, fork_owner=fork_owner):
                label_active, label_event = True, event
            else:
                violations.append(
                    f"label {config.label_approve} applied by invalid actor {actor!r} "
                    f"(fork_owner={fork_owner!r}) — rejected (V2)"
                )
        elif etype == "unlabeled" and (event.get("label") or {}).get("name") == config.label_approve:
            label_active, label_event = False, None
        elif etype == "labeled" and (event.get("label") or {}).get("name") == config.label_reject:
            if _actor_valid(actor, fork_owner=fork_owner):
                rejection = RejectionSignal("label", actor, _event_id(event), config.label_reject)
        elif etype == "commented":
            body = (event.get("body") or "").strip()
            # Exact first-token match: "/approve-reply ..." (a §2[6] reply
            # command that belongs on the UPSTREAM PR) must never read as a
            # draft approval here — prefix matching would accept it.
            first_token = body.split()[0] if body.split() else ""
            if first_token == config.comment_approve:
                if _actor_valid(actor, fork_owner=fork_owner):
                    approval = approval or ApprovalSignal(
                        "comment", actor, _event_id(event), body.splitlines()[0]
                    )
                else:
                    violations.append(
                        f"/approve comment by invalid actor {actor!r} — rejected (V2)"
                    )
            elif first_token == config.comment_reject:
                if _actor_valid(actor, fork_owner=fork_owner):
                    rejection = RejectionSignal(
                        "comment", actor, _event_id(event), body.splitlines()[0]
                    )

    if label_active and label_event is not None:
        label_signal = ApprovalSignal(
            "label", _actor_login(label_event), _event_id(label_event), config.label_approve
        )
        approval = approval or label_signal

    return SignalScan(approval=approval, rejection=rejection, violations=tuple(violations))


@dataclass(frozen=True)
class CrossCheckResult:
    eligible: bool
    ambiguous: bool
    reason: str


_LABEL_ENDPOINT_MARK = "/labels"
_COMMENT_ENDPOINT_MARK = "/comments"


def cross_check_signal(
    db: Database,
    *,
    contribution_id: str,
    fork_full_name: str,
    draft_pr_number: int,
    signal: ApprovalSignal,
) -> CrossCheckResult:
    """C-2 audit cross-check over the draft's entire lifetime.

    Scans agent-confirmed label/comment mutations in the hash-chained audit
    log. The agent has no legitimate reason to ever mutate the draft's labels
    or comment on it (C4 v2.2), so any such event makes the draft ineligible.
    Matching keys per the sign-off Q3 precedence: exact github_object_id set
    membership for comments (verified id space), coarse rule for labels
    (correlation UNVERIFIED per §10.4). Any ambiguity ⇒ fail-closed.
    """
    rows = db.conn.execute(
        "SELECT endpoint, outcome_json, github_object_id FROM audit_log"
        " WHERE actor='agent' AND phase='confirmed' AND contribution_id=?"
        " ORDER BY seq",
        (contribution_id,),
    ).fetchall()

    agent_comment_ids: set[str] = set()
    for row in rows:
        endpoint: str = row["endpoint"]
        is_label = _LABEL_ENDPOINT_MARK in endpoint
        is_comment = _COMMENT_ENDPOINT_MARK in endpoint and "replies" not in endpoint
        if not (is_label or is_comment):
            continue
        try:
            outcome = json.loads(row["outcome_json"])
        except ValueError:
            return CrossCheckResult(
                False, True,
                "agent label/comment mutation with unparseable outcome — fail-closed (C-2)",
            )
        target_repo = outcome.get("target_repo")
        target_issue = outcome.get("target_issue")
        if target_repo is None or target_issue is None:
            return CrossCheckResult(
                False, True,
                f"agent mutation {endpoint!r} lacks a recorded target — "
                "ambiguous match, fail-closed abort (C-2)",
            )
        if not (target_repo == fork_full_name and target_issue == draft_pr_number):
            continue
        if is_label:
            return CrossCheckResult(
                False, False,
                "agent-confirmed label mutation on the draft — whole draft ineligible "
                "(C-2 coarse rule; label-event-id correlation UNVERIFIED per §10.4)",
            )
        object_id = row["github_object_id"]
        if object_id is None:
            return CrossCheckResult(
                False, True,
                "agent-confirmed comment mutation on the draft has no github_object_id — "
                "ambiguous match, fail-closed abort (C-2)",
            )
        agent_comment_ids.add(str(object_id))

    if signal.kind == "comment" and str(signal.event_id) in agent_comment_ids:
        return CrossCheckResult(
            False, False,
            f"approval comment event id {signal.event_id} is in the agent-confirmed "
            "mutation id set — agent-originated signal rejected (C-2 exact membership)",
        )
    if agent_comment_ids:
        return CrossCheckResult(
            False, False,
            "agent-confirmed comment mutation(s) on the draft — whole draft ineligible "
            "(C4 v2.2: the agent has no legitimate reason to comment on the draft)",
        )
    return CrossCheckResult(True, False, "no agent label/comment mutations on the draft")


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    signal: ApprovalSignal | None = None


def pre_publish_gate(
    *,
    db: Database,
    config: Config,
    contribution_id: str,
    fork_owner: str,
    fork_full_name: str,
    draft_pr_number: int,
    fetch_timeline: Callable[[], list[dict[str, Any]]],
    fetch_draft_pr: Callable[[], PullRef],
    policy_recheck: Callable[[], bool],
) -> GateResult:
    """Atomic pre-publish gate (F-05). In ONE transaction immediately before
    upstream PR creation, re-validate ALL of:
      1. approval signal still present (label or /approve),
      2. actor binding holds (V2 v2.1: owner check + C-2 audit cross-check),
      3. fork draft PR still open (user close = rejection, F-12),
      4. policy re-check passes (FM5; verdict TTL ignored here).
    Any failure → gate fails, publish aborts, rejection audited.
    """
    with db.transaction():
        events = fetch_timeline()
        scan = scan_timeline(events, fork_owner=fork_owner, config=config)
        for violation in scan.violations:
            db.append_audit(
                actor="agent", phase="info", endpoint="gate:actor-binding-violation",
                contribution_id=contribution_id, outcome={"detail": violation},
            )

        def _fail(reason: str) -> GateResult:
            db.append_audit(
                actor="agent", phase="failed", endpoint="gate:pre-publish(F-05)",
                contribution_id=contribution_id, outcome={"reason": reason},
            )
            return GateResult(False, reason)

        if scan.rejection is not None:
            return _fail(f"rejection signal present: {scan.rejection.detail} "
                         f"by {scan.rejection.actor}")
        if scan.approval is None:
            return _fail("no valid approval signal at publish time "
                         "(removed, never given, or actor-binding failed)")

        cross = cross_check_signal(
            db, contribution_id=contribution_id, fork_full_name=fork_full_name,
            draft_pr_number=draft_pr_number, signal=scan.approval,
        )
        if not cross.eligible:
            db.append_audit(
                actor="agent", phase="info", endpoint="gate:audit-cross-check(C-2)",
                contribution_id=contribution_id,
                outcome={"reason": cross.reason, "ambiguous": cross.ambiguous},
            )
            return _fail(f"audit cross-check rejected the signal: {cross.reason}")

        draft = fetch_draft_pr()
        if draft.state != "open":
            return _fail(f"fork draft PR is {draft.state!r} — user close = rejection (F-12)")

        if not policy_recheck():
            return _fail("policy re-check failed at publish time (FM5)")

        db.append_audit(
            actor="user", phase="confirmed", endpoint="gate:pre-publish(F-05)",
            contribution_id=contribution_id,
            outcome={
                "signal_kind": scan.approval.kind,
                "signal_actor": scan.approval.actor,
                "github_event_id": scan.approval.event_id,
                "cross_check": cross.reason,
            },
        )
        return GateResult(True, "approved", scan.approval)
