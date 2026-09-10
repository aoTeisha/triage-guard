"""Arrow 11 — the low-confidence confirmation gate.

`escalation_needed` = "verdict fail ∨ low confidence ∨ policy hit" is the
condition for invoking the Human Escalation agent anywhere in the graph, not a
guard local to one state. Its three disjuncts fire in three different places:
"verdict fail" on the 10·fail edge, "low confidence" here, "policy hit" nowhere
yet (undefined in the spec). These tests cover the middle one.
"""

from __future__ import annotations

import pytest

from app.actors import safety
from app.budgets import CONFIDENCE_THRESHOLD, confidence_ok
from app.graph import TriageState
from app.graph.routers import route_verdict
from app.labels import Route
from app.runner import hydrate
from app.schemas import AcuityProposal
from app.states import State
from tests.conftest import arrows

# Nurse 2, classifier 2 -> gap 0, so the case sails past the discrepancy gate and
# reaches verdict_proposed. Confidence is what decides whether it stops there.
SURE_CASE = {
    "case_id": "case-conf",
    "channel": "website",
    "stable_patient_id": "300000011",
    "nurse_proposed_acuity": 2,
    "chief_complaint": "chest tightness",
    "vitals": {"hr": 101, "bp": "146/90", "spo2": 96, "temp_c": 37.0},
    "free_text": "Pressure in the chest for two hours.",
}


# ---- the guard itself ---------------------------------------------------------


def test_confidence_at_the_threshold_is_good_enough():
    """`confidence >= threshold`, per the Guards table. Not strictly greater."""
    assert confidence_ok(CONFIDENCE_THRESHOLD) is True


def test_confidence_below_the_threshold_is_not():
    assert confidence_ok(CONFIDENCE_THRESHOLD - 0.01) is False


def test_a_missing_confidence_is_not_treated_as_low():
    """No confidence means no model proposal, not an unsure one. The Guards table
    marks this guard 'not applicable during a classifier outage'.
    """
    assert confidence_ok(None) is True


def test_the_guard_is_skipped_while_the_gate_is_disabled():
    """AF·classifier disables the discrepancy gate for the outage; a confidence
    check on a proposal that does not exist would send every degraded case to a
    charge nurse.
    """
    assert confidence_ok(0.01, gate_disabled=True) is True


# ---- the router ----------------------------------------------------------------


def test_a_sure_verdict_clears_to_the_queue():
    state = TriageState(case_id="t", confidence=0.95)
    assert route_verdict(state) is Route.CLEARED


def test_an_unsure_verdict_escalates():
    state = TriageState(case_id="t", confidence=0.10)
    assert route_verdict(state) is Route.ESCALATE


# ---- end to end ------------------------------------------------------------------


def test_a_confident_classifier_never_reaches_the_gate(run):
    """The mock proposes 0.81, comfortably above the default threshold."""
    state, pending, _ = run(SURE_CASE)

    assert pending is None
    assert state["control_state"] == State.MONITORING.value
    assert "11" not in arrows(state)


def test_an_unsure_classifier_pauses_for_confirmation(run, monkeypatch):
    """Arrow 11: safety passed, but the model was not sure enough."""
    from app.actors import acuity_classifier

    monkeypatch.setattr(
        acuity_classifier, "classify",
        lambda payload: AcuityProposal(
            system_proposed_acuity=2, confidence=0.31, acuity_source="system",
            rationale="mock: unsure",
        ),
    )

    state, pending, _ = run(SURE_CASE)

    assert pending is not None
    assert pending["gate"] == "low_confidence"
    assert "11" in arrows(state)
    # It got here on a *passing* verdict, which is what distinguishes 11 from 10·fail.
    assert state["safety_passed"] is True


def test_the_unsure_case_is_resolved_like_any_acuity_question(graph, run, monkeypatch):
    """Low confidence asks the same question a discrepancy does — which level is
    right — so it resolves through the same path.
    """
    from langgraph.types import Command

    from app.actors import acuity_classifier

    monkeypatch.setattr(
        acuity_classifier, "classify",
        lambda payload: AcuityProposal(
            system_proposed_acuity=2, confidence=0.31, acuity_source="system",
            rationale="mock: unsure",
        ),
    )

    _, pending, thread = run(SURE_CASE)
    assert pending is not None

    resumed = hydrate(graph.invoke(
        Command(resume={"decision": "use_system_acuity", "resolver_role": "charge_nurse"}),
        {"configurable": {"thread_id": thread}},
    ))

    assert resumed["control_state"] == State.MONITORING.value
    assert resumed["acuity_source"] == "human_confirmed"


def test_a_degraded_classifier_does_not_trip_the_confidence_gate(run, monkeypatch):
    """AF·classifier already routes to nurse acuity with the gate off. Adding a
    confidence check must not re-escalate every case during an outage.
    """
    from app.actors import acuity_classifier

    def unusable(payload):
        raise RuntimeError("classifier unreachable")

    monkeypatch.setattr(acuity_classifier, "classify", unusable)

    state, pending, _ = run(SURE_CASE)

    assert state["gate_disabled"] is True
    assert "acuity_classifier" in state["degraded"]
    # Degrades to the queue on nurse acuity, not into a human gate.
    assert state["control_state"] == State.MONITORING.value


# ---- arrow 12: the escalation agent's response is recorded ---------------------


def test_a_gate_response_is_recorded_before_it_is_applied(graph, run, monkeypatch):
    """Arrow 12 — the Human Escalation agent returning its proposal."""
    from langgraph.types import Command

    from app.actors import acuity_classifier

    monkeypatch.setattr(
        acuity_classifier, "classify",
        lambda payload: AcuityProposal(
            system_proposed_acuity=2, confidence=0.31, acuity_source="system",
            rationale="mock: unsure",
        ),
    )

    _, _, thread = run(SURE_CASE)
    resumed = hydrate(graph.invoke(
        Command(resume={"decision": "use_nurse_acuity", "resolver_role": "charge_nurse"}),
        {"configurable": {"thread_id": thread}},
    ))

    trail = arrows(resumed)
    assert "12" in trail
    assert trail.index("11") < trail.index("12")


def test_a_refused_response_is_still_recorded(graph, run):
    """A denial must not erase the evidence that someone answered."""
    from langgraph.types import Command

    from tests.test_gates import GAP_CASE

    _, _, thread = run(GAP_CASE)
    denied = hydrate(graph.invoke(
        Command(resume={"decision": "use_system_acuity", "resolver_role": "porter"}),
        {"configurable": {"thread_id": thread}},
    ))

    trail = arrows(denied)
    assert "12" in trail and "BLK" in trail
    assert trail.index("12") < trail.index("BLK")
