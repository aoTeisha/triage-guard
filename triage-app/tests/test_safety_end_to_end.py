"""Safety rules 2 and 3 through the real graph, not only against hand-built facts.

The graph holds `acuity_source` as an `AcuitySource`. These rules once never
fired in a running case, because the enum reached Prolog as its name
(`'AcuitySource.HUMAN_CONFIRMED'`), and the unit tests in
test_safety_validation.py, which pass plain strings, could not see it. So each
rule here is made to fire by a case that really runs, and each legitimate settle
(agreement, the classifier-down fallback, a decision at the gate) is shown still
reaching the queue.
"""

from __future__ import annotations

from langgraph.types import Command

from app.actors import acuity_classifier
from app.graph.nodes import classify
from app.labels import Transition
from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.schemas import AcuityProposal
from app.states import AcuitySource, State
from app.verification import check_trace
from tests.conftest import transitions

CASE = {
    "case_id": "case-e2e",
    "channel": "website",
    "national_id": "300000012",
    "nurse_proposed_acuity": 3,
    "chief_complaint": "limb_injury",
    "vitals": {"hr": 78, "bp": "118/76", "spo2": 99, "temp_c": 36.7},
    "free_text": "Rolled ankle on the stairs.",
}


def _model_proposes(monkeypatch, level: int) -> None:
    monkeypatch.setattr(acuity_classifier, "classify", lambda payload: AcuityProposal(
        system_proposed_acuity=level, confidence=0.95, acuity_source="system",
        rationale="stub"))


def _settle_as(monkeypatch, source: AcuitySource, transition: Transition) -> None:
    """Make the deterministic settle hand back the wrong label, as a regression would."""
    monkeypatch.setattr(classify, "resolve_acuity",
                        lambda nurse, system: (nurse, source, transition))


def _queued(state, pending) -> None:
    assert pending == {"case_id": state["case_id"], "waiting_room": True}
    assert state["control_state"] == State.MONITORING.value
    assert state["safety_passed"] is True
    assert Transition.SAFETY_PASSED in transitions(state)
    assert check_trace(state["audit_log"]).passed


# ---- the rules fire in a running case -----------------------------------------


def test_rule_3_fires_on_a_human_confirmed_level_no_one_decided(run, monkeypatch):
    _model_proposes(monkeypatch, 3)
    _settle_as(monkeypatch, AcuitySource.HUMAN_CONFIRMED, Transition.ACUITY_AGREE)

    state, pending, _ = run(CASE)

    assert pending["gate"] == "safety_fail"
    assert Transition.SAFETY_FAILED in transitions(state)
    assert state["safety_verdict"]["reasons"] == [
        "acuity_source is human_confirmed, but this triage's audit log holds no "
        "charge-nurse decision at the gate (I3)"]


def test_rule_2_fires_on_an_automatic_settle_over_a_major_gap(run, monkeypatch):
    _model_proposes(monkeypatch, 5)                      # nurse 3, gap 2
    _settle_as(monkeypatch, AcuitySource.AUTO_RESOLVED, Transition.ACUITY_GAP_MINOR)

    state, pending, _ = run(CASE)

    assert pending["gate"] == "safety_fail"
    assert Transition.SAFETY_FAILED in transitions(state)
    assert any("acuity_source is auto_resolved" in reason and "gap was 2" in reason
               for reason in state["safety_verdict"]["reasons"])


# ---- every legitimate settle still reaches the queue --------------------------


def test_an_agreed_level_reaches_the_queue(run, monkeypatch):
    _model_proposes(monkeypatch, 3)

    state, pending, _ = run(CASE)

    _queued(state, pending)
    assert state["acuity_source"] == AcuitySource.AGREED
    assert Transition.ACUITY_AGREE in transitions(state)


def test_the_nurses_level_reaches_the_queue_when_the_classifier_is_down(run, monkeypatch):
    def down(payload):
        raise RuntimeError("classifier unreachable")
    monkeypatch.setattr(acuity_classifier, "classify", down)

    state, pending, _ = run(CASE)

    _queued(state, pending)
    assert state["acuity"] == CASE["nurse_proposed_acuity"]
    assert state["acuity_source"] == AcuitySource.NURSE_FALLBACK
    assert "acuity_classifier" in state["degraded"]


def test_a_level_decided_at_the_gate_reaches_the_queue(graph, run, monkeypatch):
    _model_proposes(monkeypatch, 1)                      # nurse 3, gap 2: the charge nurse decides
    _, pending, thread = run(CASE)
    assert pending["gate"] == "discrepancy"

    state = hydrate(graph.invoke(
        Command(resume={"decision": "use_nurse_acuity", "resolver_role": "charge_nurse"}),
        config_for(thread)))

    assert state["acuity_source"] == AcuitySource.HUMAN_CONFIRMED
    assert Transition.GATE_ACUITY_RESOLVED in transitions(state)
    assert state["control_state"] == State.MONITORING.value
    assert state["safety_passed"] is True
    assert check_trace(state["audit_log"]).passed


# ---- a re-file is a new triage for rule 5 too ----------------------------------

REFILE = {
    "nurse_proposed_acuity": 2,
    "chief_complaint": "chest_pain",
    "vitals": {"hr": 110, "bp": "120/80", "spo2": 96, "temp_c": 37.0},
}


def _refile(graph, thread: str, refile: dict = REFILE):
    graph.invoke(Command(resume={"event": "REASSESSMENT_TIMEOUT", "fire_id": "f1"}),
                 config_for(thread))
    result = graph.invoke(Command(resume=refile), config_for(thread))
    return hydrate(result), (dict(result["__interrupt__"][0].value)
                             if result.get("__interrupt__") else None)


def test_a_classifier_outage_in_an_earlier_triage_does_not_fail_the_refile(
    graph, run, monkeypatch
):
    """The classifier was down for the first triage and is back for the re-file.
    `degraded` still names it (it is history), but this triage has a real
    proposal and no fallback, so rule 5 has nothing to object to."""
    with monkeypatch.context() as down:
        down.setattr(acuity_classifier, "classify", lambda payload: None)
        state, pending, thread = run(DEMO_CASES["clean"])
    assert state["acuity_source"] == AcuitySource.NURSE_FALLBACK
    assert pending == {"case_id": state["case_id"], "waiting_room": True}

    state, pending = _refile(graph, thread)

    assert state["system_proposed_acuity"] is not None
    assert "acuity_classifier" in state["degraded"]       # history, kept
    assert state["safety_verdict"]["verdict"] == "pass", state["safety_verdict"]
    _queued(state, pending)


def test_a_refile_that_falls_back_drops_the_last_triages_classifier_output(
    graph, run, monkeypatch
):
    """The fallback writes no gap, confidence or danger-zone annotation, so the
    re-file has to clear them or the board shows the last triage's."""
    _model_proposes(monkeypatch, 2)
    state, _, thread = run(DEMO_CASES["clean"])
    assert state["acuity_gap"] is not None and state["confidence"] is not None

    monkeypatch.setattr(acuity_classifier, "classify", lambda payload: None)
    state, pending = _refile(graph, thread)

    _queued(state, pending)
    assert state["acuity_source"] == AcuitySource.NURSE_FALLBACK
    assert state["system_proposed_acuity"] is None
    assert state["acuity_gap"] is None
    assert state["confidence"] is None
    assert state["danger_zone_vitals"] == []
