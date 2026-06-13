"""Exhaustive illegal-transition matrix for the state machine (ADR §6).

The existing test_state_machine.py covers happy/representative paths. This file
closes the gap: it asserts the FULL cartesian product of (source, target) states
against the declared TRANSITIONS table — every pair not in the table raises, and
every pair in the table is accepted by assert_transition. This is the
property-style guard the architecture-critique closure (F-04/F-05 two-PR model)
relies on: no illegal edge can sneak in unnoticed.
"""

from __future__ import annotations

import itertools

import pytest

from outreach_agent.errors import IllegalTransitionError
from outreach_agent.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    State,
    assert_transition,
)

ALL_STATES = list(State)
ALL_PAIRS = list(itertools.product(ALL_STATES, ALL_STATES))


@pytest.mark.parametrize("source, target", ALL_PAIRS, ids=[f"{s}->{t}" for s, t in ALL_PAIRS])
def test_assert_transition_matches_declared_table_exactly(source: State, target: State) -> None:
    """For every (source, target): assert_transition accepts iff target is in
    TRANSITIONS[source] AND source is not terminal — and raises otherwise."""
    legal = (source not in TERMINAL_STATES) and (target in TRANSITIONS.get(source, frozenset()))
    if legal:
        assert_transition(source, target)  # must not raise
    else:
        with pytest.raises(IllegalTransitionError):
            assert_transition(source, target)


def test_every_state_appears_as_a_transitions_key() -> None:
    """No state may be missing from the table — a missing key would make every
    outgoing transition from it illegal by accident rather than by design."""
    missing = [s for s in State if s not in TRANSITIONS]
    assert missing == [], f"states missing from TRANSITIONS table: {missing}"


def test_terminal_states_are_exactly_the_keys_with_no_exits() -> None:
    """TERMINAL_STATES must equal the set of states whose transition set is empty
    — otherwise a 'terminal' state could still have a declared exit, or a dead-end
    state could be reachable-from yet not marked terminal."""
    empty_exit = {s for s in State if TRANSITIONS.get(s, frozenset()) == frozenset()}
    assert empty_exit == set(TERMINAL_STATES)


def test_no_terminal_state_is_a_transition_target_of_itself() -> None:
    for s in TERMINAL_STATES:
        assert s not in TRANSITIONS.get(s, frozenset())


def test_illegal_transition_from_terminal_is_rejected_even_if_target_legal_elsewhere() -> None:
    """A terminal state must reject ALL targets, including states that are valid
    targets from other sources (e.g. ERROR, which most states can reach)."""
    for terminal in TERMINAL_STATES:
        with pytest.raises(IllegalTransitionError):
            assert_transition(terminal, State.ERROR)


def test_self_loops_are_illegal_unless_explicitly_declared() -> None:
    for s in State:
        declared = s in TRANSITIONS.get(s, frozenset())
        if not declared:
            with pytest.raises(IllegalTransitionError):
                assert_transition(s, s)


def test_upstream_open_cannot_reach_graph_states_directly() -> None:
    """F-02: graph-verify is only reachable via MERGED, never straight from
    upstream-open — guards against skipping the post-merge verification state."""
    for graph_state in (State.GRAPH_VERIFY, State.GRAPH_CREDITED, State.GRAPH_MISSING):
        with pytest.raises(IllegalTransitionError):
            assert_transition(State.UPSTREAM_OPEN, graph_state)


def test_approved_cannot_skip_to_merged() -> None:
    """The two-PR model (F-04): approval must go through upstream-open; it cannot
    jump straight to merged without an upstream PR existing."""
    with pytest.raises(IllegalTransitionError):
        assert_transition(State.APPROVED, State.MERGED)
