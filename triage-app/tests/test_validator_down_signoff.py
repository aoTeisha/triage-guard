"""A safety check that cannot run never keeps a patient from treatment: the case
goes to a human, and a shift lead may clear it to the queue without the check.
Only a check that could not run can be cleared this way; a real safety failure
still needs a correction.
"""

from __future__ import annotations

from langgraph.types import Command

from app.deterministic import move_authorized
from app.labels import Transition
from app.runner import config_for, hydrate
from app.states import State
from app.symbolic import prolog
from app.verification import check_trace
from tests.conftest import transitions
from tests.test_component_down import CASE, _pending, _prolog_down, _safety_rules_down

CLEAR = "clear_by_shift_lead"


def _resume(graph, thread, **payload):
    return hydrate(graph.invoke(Command(resume=payload), config_for(thread)))


def _at_validator_down_gate(run, monkeypatch, take_down=_safety_rules_down):
    take_down(monkeypatch)
    _, pending, thread = run(CASE)
    assert pending["gate"] == "validator_down"
    return thread


def test_a_shift_lead_clears_a_case_the_safety_check_could_not_reach(graph, run, monkeypatch):
    thread = _at_validator_down_gate(run, monkeypatch)

    state = _resume(graph, thread, decision=CLEAR, resolver_role="shift_lead")

    assert state["control_state"] == State.MONITORING.value
    assert state["clinical_status"] == "waiting"
    assert state["safety_passed"] is False          # never claimed as checked
    assert state["safety_waived"] is True
    assert "safety_signoff" in state["degraded"]
    assert Transition.GATE_SAFETY_WAIVED in transitions(state)
    assert check_trace(state["audit_log"]).passed


def test_with_prolog_down_too_a_shift_lead_still_clears(graph, run, monkeypatch):
    thread = _at_validator_down_gate(run, monkeypatch, _prolog_down)

    state = _resume(graph, thread, decision=CLEAR, resolver_role="shift_lead")

    assert state["control_state"] == State.MONITORING.value


def test_a_charge_nurse_cannot_clear_without_the_check(graph, run, monkeypatch):
    thread = _at_validator_down_gate(run, monkeypatch)

    state = _resume(graph, thread, decision=CLEAR, resolver_role="charge_nurse")

    assert Transition.GATE_SAFETY_WAIVED not in transitions(state)
    assert transitions(state)[-1] == Transition.BLK
    assert _pending(graph, thread)["gate"] == "validator_down"


def test_a_real_safety_failure_cannot_be_cleared(graph, run, monkeypatch):
    monkeypatch.setattr(prolog, "safety_violations", lambda c: (["acuity missing"], None))
    _, pending, thread = run(CASE)
    assert pending["gate"] == "safety_fail"

    state = _resume(graph, thread, decision=CLEAR, resolver_role="shift_lead")

    assert Transition.GATE_SAFETY_WAIVED not in transitions(state)
    assert _pending(graph, thread)["gate"] == "safety_fail"


def test_a_cleared_case_can_start_treatment(graph, run, monkeypatch):
    thread = _at_validator_down_gate(run, monkeypatch)
    _resume(graph, thread, decision=CLEAR, resolver_role="shift_lead")

    moved = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    assert moved["clinical_status"] == "treatment_started"
    assert check_trace(moved["audit_log"]).passed


def test_move_accepts_a_signed_waiver_in_place_of_a_pass():
    assert move_authorized(False, True, "nurse", safety_waived=True)[0]
    assert not move_authorized(False, True, "nurse")[0]
    assert not move_authorized(False, False, "nurse", safety_waived=True)[0]


def test_the_trace_check_refuses_a_waiver_after_a_real_failure():
    def rec(t):
        return {"at": "x", "case_id": "c", "control_state": "s", "action": "a",
                "explanation": "e", "transition": t}
    log = [rec(Transition.SAFETY_FAILED), rec(Transition.GATE_SAFETY_WAIVED),
           rec(Transition.CLEARED_TO_QUEUE)]

    assert not check_trace(log).passed
