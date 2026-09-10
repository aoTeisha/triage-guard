"""The compiled graph vs docs/SPECIFICATION.md § Transitions.

These tests are the reason the migration was worth doing. Under the old CrewAI
Flow the set of legal moves existed only as a convention spread across decorators,
so there was nothing to assert against the spec. Here the edge map is a declared
object, and the spec table can be checked line by line.
"""

from __future__ import annotations

from app.graph import UNIMPLEMENTED_STATES, build_graph
from app.states import State

DEGRADE_NODES = {"classifier_fallback", "safety_fallback"}


def _graph():
    return build_graph().get_graph()


def node_names() -> set[str]:
    return {n for n in _graph().nodes if n not in {"__start__", "__end__"}}


def edges() -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in _graph().edges}


def test_every_spec_state_is_either_a_node_or_declared_unimplemented():
    """No control state may be silently absent.

    If someone adds a state to the spec and forgets to wire it, this fails —
    which is the point. The alternative is a spec that quietly drifts ahead of
    the code.
    """
    wired = node_names() - DEGRADE_NODES
    for state in State:
        assert state.value in wired or state in UNIMPLEMENTED_STATES, (
            f"{state.value} is in the spec but neither wired nor declared unimplemented"
        )


def test_unimplemented_states_are_actually_absent():
    """The escape hatch cannot be used to hide a state that IS wired."""
    for state in UNIMPLEMENTED_STATES:
        assert state.value not in node_names()


def test_intake_fans_out_to_exactly_the_four_documented_outcomes():
    """Arrows 4 / 16 / 17 / 18."""
    targets = {t for s, t in edges() if s == State.PARSING.value}
    assert targets == {
        State.DATA_PARSED.value,
        State.MISSING_FIELDS_REQUESTED.value,
        State.SUBMISSION_FAILED.value,
        State.INPUT_REJECTED.value,
    }


def test_data_parsed_leads_to_identity_lookup():
    """Arrow 4b."""
    assert (State.DATA_PARSED.value, State.RESOLVING_IDENTITY.value) in edges()


def test_redaction_can_halt_but_classification_is_the_only_way_forward():
    """Arrow 6 forward, V·halt·PII sideways. Critical-closed: no third option."""
    targets = {t for s, t in edges() if s == State.REDACTING_ROUTING.value}
    assert targets == {
        State.CLASSIFYING.value,
        State.AGENT_FAILED.value,
        State.REDACTING_ROUTING.value,     # V·retry self-loop
    }


def test_classifier_exhaustion_degrades_instead_of_halting():
    """AF·classifier is fail-open: the case continues on nurse acuity."""
    assert (State.CLASSIFYING.value, "classifier_fallback") in edges()
    assert ("classifier_fallback", State.SAFETY_VALIDATING.value) in edges()
    assert (State.CLASSIFYING.value, State.AGENT_FAILED.value) not in edges()


def test_safety_exhaustion_routes_to_a_human_not_a_halt():
    """AF·safety: every case goes to a charge nurse so no-approval-bypass holds."""
    assert (State.SAFETY_VALIDATING.value, "safety_fallback") in edges()
    assert ("safety_fallback", State.AWAITING_HUMAN_APPROVAL.value) in edges()


def test_acuity_gap_has_exactly_two_destinations():
    """9a/9b settle and continue; 9c escalates. The bands are total and exclusive."""
    targets = {t for s, t in edges() if s == State.ACUITY_PROPOSED.value}
    assert targets == {
        State.SAFETY_VALIDATING.value,
        State.AWAITING_HUMAN_APPROVAL.value,
    }


def test_the_gate_returns_to_safety_never_straight_to_monitoring():
    """Correct-and-revalidate, never override.

    A resolved gate must re-run safety validation. An edge from the gate directly
    to monitoring would be an approval bypass.
    """
    targets = {t for s, t in edges() if s == State.AWAITING_HUMAN_APPROVAL.value}
    assert State.SAFETY_VALIDATING.value in targets
    assert State.MONITORING.value not in targets


def test_denial_is_reachable_and_terminal():
    """BLK: the attempt is refused and the case does not move on."""
    assert (State.AWAITING_HUMAN_APPROVAL.value, State.ACTION_DENIED.value) in edges()
    assert {t for s, t in edges() if s == State.ACTION_DENIED.value} == {"__end__"}


def test_monitoring_is_only_reachable_through_a_passing_verdict():
    """No path may reach the queue without passing safety validation."""
    into_monitoring = {s for s, t in edges() if t == State.MONITORING.value}
    assert into_monitoring == {State.VERDICT_PROPOSED.value}


def test_graph_renders_a_complete_mermaid_diagram():
    """The diagram is generated from the declared edge set, so it cannot disagree
    with what runs — the failure mode the old CrewAI plot() had.
    """
    mermaid = build_graph().get_graph().draw_mermaid()
    for state in node_names():
        assert state in mermaid
