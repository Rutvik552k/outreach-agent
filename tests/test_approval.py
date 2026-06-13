from __future__ import annotations

from typing import Any

import pytest

from conftest import FORK, FORK_OWNER, make_pull_ref
from outreach_agent.approval import (
    ApprovalSignal,
    cross_check_signal,
    pre_publish_gate,
    scan_timeline,
)
from outreach_agent.config import Config
from outreach_agent.persistence import Database

DRAFT_PR = 7
CONTRIB = "c1"


def _label_event(name: str, actor: str, event: str = "labeled",
                 event_id: int = 100) -> dict[str, Any]:
    return {"event": event, "actor": {"login": actor},
            "label": {"name": name}, "id": event_id}


def _comment_event(body: str, actor: str, event_id: int = 200) -> dict[str, Any]:
    return {"event": "commented", "actor": {"login": actor},
            "body": body, "id": event_id}


def _agent_confirmed(db: Database, *, endpoint: str, outcome: dict[str, Any],
                     github_object_id: str | None = None) -> None:
    with db.transaction():
        db.append_audit(
            actor="agent", phase="confirmed", endpoint=endpoint,
            contribution_id=CONTRIB, outcome=outcome,
            github_object_id=github_object_id,
        )


@pytest.fixture
def gate_kwargs(db: Database, config: Config) -> dict[str, Any]:
    return dict(
        db=db, config=config, contribution_id=CONTRIB,
        fork_owner=FORK_OWNER, fork_full_name=FORK, draft_pr_number=DRAFT_PR,
        fetch_draft_pr=lambda: make_pull_ref(state="open"),
        policy_recheck=lambda: True,
    )


def test_label_approval_accepted(config: Config) -> None:
    scan = scan_timeline(
        [_label_event("agent:approve-upstream", FORK_OWNER)],
        fork_owner=FORK_OWNER, config=config,
    )
    assert scan.approval is not None and scan.approval.kind == "label"
    assert scan.approval.actor == FORK_OWNER


def test_slash_approve_comment_accepted_first_class(config: Config) -> None:
    """F-15: /approve comment is first-class, verified identically to the label."""
    scan = scan_timeline(
        [_comment_event("/approve looks good", FORK_OWNER)],
        fork_owner=FORK_OWNER, config=config,
    )
    assert scan.approval is not None and scan.approval.kind == "comment"


def test_third_party_actor_rejected(config: Config) -> None:
    """V2: actor != fork_owner (e.g., a drive-by collaborator) → rejected."""
    scan = scan_timeline(
        [_label_event("agent:approve-upstream", "random-passerby"),
         _comment_event("/approve", "random-passerby")],
        fork_owner=FORK_OWNER, config=config,
    )
    assert scan.approval is None
    assert len(scan.violations) == 2


def test_label_removed_after_detection_aborts_publish(gate_kwargs: dict[str, Any],
                                                      db: Database) -> None:
    """F-05: approval label applied then removed before publish → gate aborts."""
    timeline = [
        _label_event("agent:approve-upstream", FORK_OWNER, "labeled", 1),
        _label_event("agent:approve-upstream", FORK_OWNER, "unlabeled", 2),
    ]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is False
    assert "no valid approval signal" in result.reason
    audited = db.conn.execute(
        "SELECT phase FROM audit_log WHERE endpoint='gate:pre-publish(F-05)'"
    ).fetchone()
    assert audited["phase"] == "failed"


def test_gate_passes_with_valid_label(gate_kwargs: dict[str, Any], db: Database) -> None:
    timeline = [_label_event("agent:approve-upstream", FORK_OWNER)]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is True
    confirmed = db.conn.execute(
        "SELECT actor, outcome_json FROM audit_log WHERE endpoint='gate:pre-publish(F-05)'"
    ).fetchone()
    assert confirmed["actor"] == "user"
    assert FORK_OWNER in confirmed["outcome_json"]


def test_gate_aborts_when_draft_pr_closed(gate_kwargs: dict[str, Any]) -> None:
    """F-12: user closing the draft PR = rejection."""
    gate_kwargs["fetch_draft_pr"] = lambda: make_pull_ref(state="closed")
    timeline = [_label_event("agent:approve-upstream", FORK_OWNER)]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is False
    assert "closed" in result.reason


def test_gate_aborts_on_policy_recheck_failure(gate_kwargs: dict[str, Any]) -> None:
    """FM5: policy re-check inside the gate; verdict TTL is ignored here."""
    gate_kwargs["policy_recheck"] = lambda: False
    timeline = [_label_event("agent:approve-upstream", FORK_OWNER)]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is False
    assert "policy re-check" in result.reason


def test_gate_aborts_on_rejection_signal(gate_kwargs: dict[str, Any]) -> None:
    timeline = [
        _label_event("agent:approve-upstream", FORK_OWNER),
        _comment_event("/reject not this one", FORK_OWNER),
    ]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is False
    assert "rejection signal" in result.reason


# -- C-2 audit cross-check (sign-off blockers) --------------------------------


def test_agent_labeled_draft_rejected(gate_kwargs: dict[str, Any], db: Database) -> None:
    """C-2 coarse rule: an agent-confirmed label mutation targeting the draft
    makes the WHOLE draft ineligible (label-event-id correlation UNVERIFIED)."""
    _agent_confirmed(
        db,
        endpoint="POST /repos/{owner}/{repo}/issues/{issue_number}/labels",
        outcome={"summary": "bug: label add", "target_repo": FORK,
                 "target_issue": DRAFT_PR},
        github_object_id="555",
    )
    timeline = [_label_event("agent:approve-upstream", FORK_OWNER)]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is False
    assert "coarse rule" in result.reason
    cross_audit = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint='gate:audit-cross-check(C-2)'"
    ).fetchone()
    assert cross_audit is not None


def test_agent_comment_object_id_match_rejected(gate_kwargs: dict[str, Any],
                                                db: Database) -> None:
    """C-2 primary key: approval-comment event id ∈ agent-confirmed mutation
    id set ⇒ agent-originated signal rejected (exact membership)."""
    _agent_confirmed(
        db,
        endpoint="POST /repos/{owner}/{repo}/issues/{issue_number}/comments",
        outcome={"summary": "bug: comment", "target_repo": FORK,
                 "target_issue": DRAFT_PR},
        github_object_id="31337",
    )
    timeline = [_comment_event("/approve", FORK_OWNER, event_id=31337)]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is False
    assert "agent-originated" in result.reason or "ineligible" in result.reason


def test_ambiguous_match_aborts(gate_kwargs: dict[str, Any], db: Database) -> None:
    """C-2 fail-closed: an agent comment mutation on the draft with no
    github_object_id is an ambiguous match → abort + audit."""
    _agent_confirmed(
        db,
        endpoint="POST /repos/{owner}/{repo}/issues/{issue_number}/comments",
        outcome={"summary": "bug: comment", "target_repo": FORK,
                 "target_issue": DRAFT_PR},
        github_object_id=None,
    )
    timeline = [_comment_event("/approve", FORK_OWNER, event_id=42)]
    result = pre_publish_gate(fetch_timeline=lambda: timeline, **gate_kwargs)
    assert result.passed is False
    assert "fail-closed" in result.reason or "ambiguous" in result.reason
    cross_audit = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint='gate:audit-cross-check(C-2)'"
    ).fetchone()
    assert "true" in cross_audit["outcome_json"]  # ambiguous: true


def test_missing_target_is_ambiguous_fail_closed(db: Database) -> None:
    _agent_confirmed(
        db,
        endpoint="POST /repos/{owner}/{repo}/issues/{issue_number}/labels",
        outcome={"summary": "no target recorded"},
    )
    cross = cross_check_signal(
        db, contribution_id=CONTRIB, fork_full_name=FORK,
        draft_pr_number=DRAFT_PR,
        signal=ApprovalSignal("label", FORK_OWNER, "1", "agent:approve-upstream"),
    )
    assert cross.eligible is False and cross.ambiguous is True


def test_agent_mutation_on_other_target_does_not_poison_draft(db: Database) -> None:
    """Comments on the upstream issue (triage) are legitimate and must not
    make the draft ineligible."""
    _agent_confirmed(
        db,
        endpoint="POST /repos/{owner}/{repo}/issues/{issue_number}/comments",
        outcome={"summary": "triage comment", "target_repo": "acme/some-lib",
                 "target_issue": 123},
        github_object_id="777",
    )
    cross = cross_check_signal(
        db, contribution_id=CONTRIB, fork_full_name=FORK,
        draft_pr_number=DRAFT_PR,
        signal=ApprovalSignal("comment", FORK_OWNER, "888", "/approve"),
    )
    assert cross.eligible is True
