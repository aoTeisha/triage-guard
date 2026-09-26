"""Tests for the low-confidence confirmation gate: when a case's safety
verdict passed but the classifier's confidence in its proposed acuity was
too low, the case still gets escalated to a charge nurse for confirmation.

Escalating to a human happens for three separate reasons across the graph: a
failed safety verdict, low classifier confidence (covered here), or a policy
hit (not yet implemented). This file covers only the confidence-based one.
"""

from __future__ import annotations

import pytest

from app.actors import safety
from app.budgets import CONFIDENCE_THRESHOLD, confidence_ok
from app.graph import TriageState
from app.graph.routers import route_verdict
from app.labels import Route, Transition
from app.runner import hydrate
from app.schemas import AcuityProposal
from app.states import State
from tests.conftest import transitions

# Nurse proposes acuity 2, the mock classifier also proposes 2 — a gap of 0,
# so the case sails past the nurse/system discrepancy check and reaches
# verdict_proposed. From there, classifier confidence alone decides whether
# it stops for confirmation.
SURE_CASE = {
    "case_id": "case-conf",
    "channel": "website",
    "national_id": "300000011",
    "nurse_proposed_acuity": 2,
    "chief_complaint": "chest tightness",
    "vitals": {"hr": 101, "bp": "146/90", "spo2": 96, "temp_c": 37.0},
    "free_text": "Pressure in the chest for two hours.",
}


# ---- the guard itself ---------------------------------------------------------


def test_confidence_at_the_threshold_is_good_enough():
    """Confidence exactly equal to the threshold is good enough — the check
    is `>=`, not strictly `>`.
    """
    assert confidence_ok(CONFIDENCE_THRESHOLD) is True


def test_confidence_below_the_threshold_is_not():
    assert confidence_ok(CONFIDENCE_THRESHOLD - 0.01) is False


def test_a_missing_confidence_is_not_treated_as_low():
    """A missing confidence value means no model proposal was made at all
    (classifier outage) — not an unsure one — so this guard doesn't apply
    and defaults to passing.
    """
    assert confidence_ok(None) is True


def test_the_guard_is_skipped_while_the_gate_is_disabled():
    """During a classifier outage, the discrepancy check is disabled and
    there's no classifier proposal to check confidence on — running the
    confidence check anyway would incorrectly escalate every degraded case
    to a charge nurse.
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

    # Reaching the queue is a real pause (a waiting-room interrupt, not a
    # gate awaiting a human decision), so `pending` is a waiting-room payload
    # here, not `None`.
    assert pending == {"case_id": SURE_CASE["case_id"], "waiting_room": True}
    assert state["control_state"] == State.MONITORING.value
    assert Transition.ESCALATION_NEEDED not in transitions(state)


def test_an_unsure_classifier_pauses_for_confirmation(run, monkeypatch):
    """Safety validation passed, but the classifier wasn't confident enough
    in its proposed acuity, so the case pauses for a charge nurse to confirm.
    """
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
    assert Transition.ESCALATION_NEEDED in transitions(state)
    # Got here by passing safety validation but failing the confidence check
    # — distinct from arriving here via a failed safety verdict instead.
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
    """During a classifier outage, the case already falls back to the
    nurse's own acuity with the discrepancy gate disabled. Adding a
    confidence check must not undo that by re-escalating every case anyway.
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


# ---- a charge nurse's gate response gets recorded ---------------------------


def test_a_gate_response_is_recorded_before_it_is_applied(graph, run, monkeypatch):
    """A charge nurse's decision at the gate gets logged as its own audit
    entry, separate from whatever it causes to happen next.
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

    _, _, thread = run(SURE_CASE)
    resumed = hydrate(graph.invoke(
        Command(resume={"decision": "use_nurse_acuity", "resolver_role": "charge_nurse"}),
        {"configurable": {"thread_id": thread}},
    ))

    trail = transitions(resumed)
    assert Transition.ESCALATION_RECORDED in trail
    assert trail.index(Transition.ESCALATION_NEEDED) < trail.index(Transition.ESCALATION_RECORDED)


def test_a_refused_response_is_still_recorded(graph, run):
    """A denial must not erase the evidence that someone answered."""
    from langgraph.types import Command

    from tests.test_gates import GAP_CASE

    _, _, thread = run(GAP_CASE)
    denied = hydrate(graph.invoke(
        Command(resume={"decision": "use_system_acuity", "resolver_role": "porter"}),
        {"configurable": {"thread_id": thread}},
    ))

    trail = transitions(denied)
    assert Transition.ESCALATION_RECORDED in trail and Transition.BLK in trail
    assert trail.index(Transition.ESCALATION_RECORDED) < trail.index(Transition.BLK)
