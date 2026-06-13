from __future__ import annotations

import pytest

from outreach_agent.errors import IllegalTransitionError
from outreach_agent.persistence import Database
from outreach_agent.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    ContributionStore,
    State,
    assert_transition,
)


@pytest.fixture
def store(db: Database) -> ContributionStore:
    return ContributionStore(db)


def _advance(store: ContributionStore, cid: str, *states: State) -> None:
    for s in states:
        store.transition(cid, s)


def test_state_machine_illegal_transition(store: ContributionStore) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    with pytest.raises(IllegalTransitionError):
        store.transition(cid, State.MERGED)
    assert store.get_state(cid) is State.DISCOVERED  # rolled back, unchanged


def test_terminal_states_have_no_exits(store: ContributionStore) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(store, cid, State.SCORED, State.POLICY_CLEARED,
             State.WORKFLOW_FILE_TOUCH_UNSUPPORTED)
    for target in State:
        with pytest.raises(IllegalTransitionError):
            store.transition(cid, target)


def test_full_happy_path_to_graph_credited(store: ContributionStore) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(
        store, cid,
        State.SCORED, State.POLICY_CLEARED, State.PREPARED, State.CI_GREEN,
        State.DRAFT_ON_FORK, State.APPROVED, State.UPSTREAM_OPEN,
        State.REVIEW_LOOP, State.MERGED, State.GRAPH_VERIFY, State.GRAPH_CREDITED,
    )
    assert store.get_state(cid) is State.GRAPH_CREDITED


def test_graph_verify_branch_missing(store: ContributionStore) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(store, cid, State.SCORED, State.POLICY_CLEARED, State.PREPARED,
             State.CI_GREEN, State.DRAFT_ON_FORK, State.APPROVED,
             State.UPSTREAM_OPEN, State.MERGED, State.GRAPH_VERIFY,
             State.GRAPH_MISSING)
    assert store.get_state(cid) is State.GRAPH_MISSING


def test_llm_blocked_reverts_to_policy_cleared(store: ContributionStore) -> None:
    """F-13/FM9: LLM failure mid-prep → llm-blocked, re-enterable to policy-cleared."""
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(store, cid, State.SCORED, State.POLICY_CLEARED, State.LLM_BLOCKED,
             State.POLICY_CLEARED)
    assert store.get_state(cid) is State.POLICY_CLEARED


def test_sandbox_unfit_distinct_from_ci_failed(store: ContributionStore) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(store, cid, State.SCORED, State.POLICY_CLEARED, State.PREPARED,
             State.SANDBOX_UNFIT)
    assert store.get_state(cid) is State.SANDBOX_UNFIT


def test_review_loop_changes_requested_reenters_prepared(store: ContributionStore) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(store, cid, State.SCORED, State.POLICY_CLEARED, State.PREPARED,
             State.CI_GREEN, State.DRAFT_ON_FORK, State.APPROVED,
             State.UPSTREAM_OPEN, State.REVIEW_LOOP, State.PREPARED)
    assert store.get_state(cid) is State.PREPARED


def test_upstream_unavailable_reachable_from_review(store: ContributionStore) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(store, cid, State.SCORED, State.POLICY_CLEARED, State.PREPARED,
             State.CI_GREEN, State.DRAFT_ON_FORK, State.APPROVED,
             State.UPSTREAM_OPEN, State.REVIEW_LOOP, State.UPSTREAM_UNAVAILABLE)
    assert store.get_state(cid) is State.UPSTREAM_UNAVAILABLE


def test_transitions_audited_in_same_transaction(store: ContributionStore,
                                                 db: Database) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    store.transition(cid, State.SCORED, reason="score=0.8")
    rows = db.conn.execute(
        "SELECT outcome_json FROM audit_log WHERE endpoint='state:transition'"
        " AND contribution_id=?", (cid,)
    ).fetchall()
    assert len(rows) == 1
    assert '"to":"scored"' in rows[0]["outcome_json"].replace(" ", "")
    db.verify_chains()


def test_two_pr_model_fields_persisted(store: ContributionStore, db: Database) -> None:
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    _advance(store, cid, State.SCORED, State.POLICY_CLEARED, State.PREPARED,
             State.CI_GREEN)
    store.transition(cid, State.DRAFT_ON_FORK,
                     fields={"fork_draft_pr_number": 7, "fork_full_name": "rutvik/some-lib",
                             "branch": "agent/123-fix"})
    store.transition(cid, State.APPROVED)
    store.transition(cid, State.UPSTREAM_OPEN, fields={"upstream_pr_number": 991})
    row = db.conn.execute(
        "SELECT fork_draft_pr_number, upstream_pr_number FROM contributions"
        " WHERE contribution_id=?", (cid,)
    ).fetchone()
    assert row["fork_draft_pr_number"] == 7
    assert row["upstream_pr_number"] == 991


def test_every_transition_target_is_a_known_state() -> None:
    for source, targets in TRANSITIONS.items():
        assert isinstance(source, State)
        for t in targets:
            assert isinstance(t, State)
    for terminal in TERMINAL_STATES:
        assert TRANSITIONS[terminal] == frozenset()
    assert_transition(State.MERGED, State.GRAPH_VERIFY)
