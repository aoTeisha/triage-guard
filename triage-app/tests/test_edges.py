"""The compiled graph vs docs/SPECIFICATION.md § Transitions.

These tests are the reason the migration was worth doing. Under the old CrewAI
Flow the set of legal moves existed only as a convention spread across decorators,
so there was nothing to assert against the spec. Here the edge map is a declared
object, and the spec table can be checked line by line.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.graph import UNIMPLEMENTED_STATES, build_graph, routers
from app.states import State

DEGRADE_NODES = {"classifier_fallback", "safety_fallback"}
DIAGRAMS_MD = Path(__file__).resolve().parents[2] / "docs" / "diagrams.md"


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
    """SUBMISSION_VALID / MISSING_FIELDS / SUBMISSION_UNUSABLE / INVALID_INPUT."""
    targets = {t for s, t in edges() if s == State.PARSING.value}
    assert targets == {
        State.DATA_PARSED.value,
        State.MISSING_FIELDS_REQUESTED.value,
        State.SUBMISSION_FAILED.value,
        State.INPUT_REJECTED.value,
    }


def test_data_parsed_leads_to_identity_lookup():
    """LOOKUP."""
    assert (State.DATA_PARSED.value, State.RESOLVING_IDENTITY.value) in edges()


def test_redaction_halts_on_a_leak_and_skips_the_model_when_opa_is_down():
    """PAYLOAD_CLEAN forward, V_HALT_PII sideways, PRIVACY_GATE_DOWN around the
    model to the nurse's acuity. A found leak still halts; nothing else does."""
    targets = {t for s, t in edges() if s == State.REDACTING_ROUTING.value}
    assert targets == {
        State.CLASSIFYING.value,
        State.AGENT_FAILED.value,
        State.REDACTING_ROUTING.value,     # V_RETRY self-loop
        "classifier_fallback",             # OPA down: payload not proven clean
    }


def test_classifier_exhaustion_degrades_instead_of_halting():
    """AF_CLASSIFIER is fail-open: the case continues on nurse acuity."""
    assert (State.CLASSIFYING.value, "classifier_fallback") in edges()
    assert ("classifier_fallback", State.SAFETY_VALIDATING.value) in edges()
    assert (State.CLASSIFYING.value, State.AGENT_FAILED.value) not in edges()


def test_safety_exhaustion_routes_to_a_human_not_a_halt():
    """AF_SAFETY: every case goes to a charge nurse so no-approval-bypass holds.

    The validator-down edge is the error handler's `goto`. It is drawn through
    `destinations=`, not the router, so it has to be checked here or the
    picture could lose it while the code still takes it."""
    assert (State.SAFETY_VALIDATING.value, "safety_fallback") in edges()
    assert ("safety_fallback", State.AWAITING_HUMAN_APPROVAL.value) in edges()


def test_safety_validation_has_exactly_three_ways_out():
    """Passed, failed, or validator down. There is no retry self-loop: a re-run
    after a crash is the node's retry policy, not a transition."""
    targets = {t for s, t in edges() if s == State.SAFETY_VALIDATING.value}
    assert targets == {
        State.VERDICT_PROPOSED.value,
        State.AWAITING_HUMAN_APPROVAL.value,
        "safety_fallback",
    }


def test_every_crash_goto_is_a_drawn_edge():
    """A handler's `goto` is not checked by LangGraph against declared edges, so
    check it here: each handler's target must be drawn out of its node."""
    from langgraph.errors import NodeError

    from app.graph import build
    from app.graph.state import TriageState

    for node, handler in build._ERROR_HANDLERS.items():
        state = TriageState(case_id="c", control_state=node.value)
        target = handler(state, NodeError(node=node.value, error=RuntimeError("x"))).goto
        assert (node.value, target) in edges(), (node.value, target)


def test_acuity_gap_has_exactly_two_destinations():
    """ACUITY_AGREE / ACUITY_GAP_MINOR settle and continue; ACUITY_GAP_MAJOR escalates.
    The bands are total and exclusive."""
    targets = {t for s, t in edges() if s == State.ACUITY_PROPOSED.value}
    assert targets == {
        State.SAFETY_VALIDATING.value,
        State.AWAITING_HUMAN_APPROVAL.value,
    }


def test_the_gate_returns_to_safety_or_queues_only_on_a_shift_lead_clear():
    """Correct-and-revalidate, never override a verdict.

    A resolved gate re-runs safety validation. Its one edge straight to the queue
    is a shift lead clearing a case whose safety check could not run, and the
    router takes it only on that answer.
    """
    targets = {t for s, t in edges() if s == State.AWAITING_HUMAN_APPROVAL.value}
    assert State.SAFETY_VALIDATING.value in targets
    assert State.MONITORING.value in targets
    assert "GATE_SAFETY_WAIVED" in inspect.getsource(routers.route_gate)


def test_a_refused_gate_loops_back_to_the_gate():
    """BLK at the approval gate: the attempt is refused and the case stays
    parked at the gate, resolvable — it does not end the run. Mirrors what
    the reassessment pause already does with its own denials.
    """
    assert (State.AWAITING_HUMAN_APPROVAL.value, State.AWAITING_HUMAN_APPROVAL.value) in edges()
    assert State.ACTION_DENIED.value not in {s for s, _ in edges()} | {t for _, t in edges()}


def test_monitoring_is_only_reachable_through_a_verdict_or_a_shift_lead_clear():
    """No path reaches the queue without a passing safety verdict, except a shift
    lead clearing a case whose safety check could not run."""
    into_monitoring = {s for s, t in edges() if t == State.MONITORING.value}
    assert into_monitoring == {State.VERDICT_PROPOSED.value, State.AWAITING_HUMAN_APPROVAL.value}


def test_graph_renders_a_complete_mermaid_diagram():
    """The diagram is generated from the declared edge set, so it cannot disagree
    with what runs — the failure mode the old CrewAI plot() had.
    """
    mermaid = build_graph().get_graph().draw_mermaid()
    for state in node_names():
        assert state in mermaid


def _diagram_pairs() -> set[tuple[str, str]]:
    """Every `a --> b` arrow in the stateDiagram block of docs/diagrams.md."""
    block = DIAGRAMS_MD.read_text().split("```mermaid\nstateDiagram-v2", 1)[1].split("```", 1)[0]
    return set(re.findall(r"^\s*(\S+)\s*-->\s*(\S+)", block, re.M))


def test_diagram_names_exactly_the_spec_states():
    """The hand-drawn diagram uses the spec's state names, all of them, and
    nothing else. A refusal is a self-loop, so `action_denied` is never drawn."""
    drawn = {n for pair in _diagram_pairs() for n in pair} - {"[*]"}
    assert drawn == {s.value for s in State} - {State.ACTION_DENIED.value}


def test_diagram_draws_every_wired_state_edge():
    """Every edge the graph wires between two spec states has an arrow."""
    spec = {s.value for s in State}
    wired = {(s, t) for s, t in edges() if s in spec and t in spec}
    assert wired <= _diagram_pairs(), wired - _diagram_pairs()
