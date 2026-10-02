"""One symbolic engine or the model down: the case keeps going, through a human
where a check could not run, and never past a check that could not run.

- OPA down: the payload cannot be proven clean, so the model is skipped and the
  case settles on the nurse's acuity, the same as a classifier outage.
- Prolog down: the safety check cannot answer, so a charge nurse gets
  the case and asks for the check again once the engine is back. No acuity change
  is required, because the input did not cause the failure.
"""

from __future__ import annotations

import pytest
from langgraph.types import Command

from app.actors import acuity_classifier, normalizer, safety
from app.budgets import MAX_CORRECTION_ROUNDS
from app.labels import Transition
from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.states import State
from app.symbolic import prolog
from app.verification import check_trace
from tests.conftest import transitions

CASE = {
    "case_id": "case-down",
    "channel": "website",
    "national_id": "300000010",
    "nurse_proposed_acuity": 3,
    "chief_complaint": "limb_injury",
    "vitals": {"hr": 78, "bp": "118/76", "spo2": 99, "temp_c": 36.7},
    "free_text": "Rolled ankle on the stairs.",
}


def _opa_down(mp):
    mp.delenv("OPA_URL", raising=False)      # no sidecar either: nothing can answer
    mp.setenv("OPA_BIN", "/nonexistent/opa")


def _prolog_down(mp):
    def no_engine():
        raise RuntimeError("swipl not reachable")
    mp.setattr(prolog, "_engine", no_engine)


def _safety_rules_down(mp):
    """Only Prolog's safety query fails; `may_resolve_gate` still answers, so a
    charge nurse can still answer the gate."""
    mp.setattr(prolog, "safety_violations", lambda case: ([], "prolog: safety rules not reachable"))


def _resume(graph, thread, response):
    return hydrate(graph.invoke(Command(resume=response), {"configurable": {"thread_id": thread}}))


def _pending(graph, thread):
    snap = graph.get_state({"configurable": {"thread_id": thread}})
    interrupts = [i for task in snap.tasks for i in task.interrupts]
    return dict(interrupts[0].value) if interrupts else None


# ---- OPA ---------------------------------------------------------------------


def test_opa_down_skips_the_model_and_queues_on_the_nurses_acuity(run, monkeypatch):
    calls = []
    monkeypatch.setattr(acuity_classifier, "classify", lambda payload: calls.append(payload))
    _opa_down(monkeypatch)

    state, pending, _ = run(CASE)

    assert calls == []                                   # the model never saw the payload
    assert pending.get("waiting_room")                   # in the queue, not at a gate
    assert state["control_state"] == State.MONITORING.value
    assert state["acuity"] == CASE["nurse_proposed_acuity"]
    assert state["gate_disabled"] is True
    assert "opa" in state["degraded"]
    assert "acuity_classifier" not in state["degraded"]  # the model is fine, it was skipped
    assert Transition.PRIVACY_GATE_DOWN in transitions(state)
    assert Transition.V_HALT_PII not in transitions(state)
    assert check_trace(state["audit_log"]).passed


def test_opa_down_still_halts_on_an_identifier_the_regex_finds(run, monkeypatch):
    """Only OPA's half of the check is missing; a leak the regex scan can see is
    still a leak, and still stops the line."""
    real = normalizer.build_model_payload
    monkeypatch.setattr(normalizer, "build_model_payload",
                        lambda *a, **k: {**real(*a, **k), "chief_complaint": "id 123456789"})
    _opa_down(monkeypatch)

    state, _, _ = run(CASE)

    assert state["control_state"] == State.AGENT_FAILED.value
    assert Transition.V_HALT_PII in transitions(state)


# ---- Prolog --------------------------------------------------------------------


@pytest.mark.parametrize("take_down", [_prolog_down, _safety_rules_down])
def test_a_safety_engine_down_sends_the_case_to_the_validator_down_gate(run, monkeypatch, take_down):
    take_down(monkeypatch)

    state, pending, _ = run(CASE)

    assert pending is not None
    assert pending["gate"] == "validator_down"
    assert pending["options"] == ["revalidate", "escalate_further", "clear_by_shift_lead"]
    assert "prolog" in state["degraded"]
    assert "safety_validation" not in state["degraded"]   # the engine is named instead
    assert Transition.V_EXHAUSTED_SAFETY in transitions(state)
    assert state["safety_passed"] is False


@pytest.mark.parametrize("take_down", [_prolog_down, _safety_rules_down])
def test_revalidate_once_the_engine_is_back_queues_the_case(graph, run, monkeypatch, take_down):
    with monkeypatch.context() as down:
        take_down(down)
        _, pending, thread = run(CASE)
    assert pending["gate"] == "validator_down"

    state = _resume(graph, thread, {"decision": "revalidate", "resolver_role": "charge_nurse"})

    assert state["control_state"] == State.MONITORING.value
    assert state["safety_passed"] is True
    assert Transition.GATE_REVALIDATE in transitions(state)
    assert Transition.SAFETY_PASSED in transitions(state)
    assert check_trace(state["audit_log"]).passed


def test_revalidate_while_still_down_comes_back_to_the_gate(graph, run, monkeypatch):
    _prolog_down(monkeypatch)
    # Prolog also authorizes the resolver; keep that half up so the nurse can answer.
    monkeypatch.setattr(prolog, "may_resolve_gate", lambda role, senior_required=False: (True, "ok"))
    _, pending, thread = run(CASE)

    _resume(graph, thread, {"decision": "revalidate", "resolver_role": "charge_nurse"})

    again = _pending(graph, thread)
    assert again is not None and again["gate"] == "validator_down"
    snap = graph.get_state({"configurable": {"thread_id": thread}})
    assert snap.values["correction_rounds"] == 1


def test_a_long_outage_hands_the_case_to_a_shift_lead(graph, run, monkeypatch):
    _prolog_down(monkeypatch)
    monkeypatch.setattr(prolog, "may_resolve_gate", lambda role, senior_required=False: (True, "ok"))
    _, _, thread = run(CASE)

    for _ in range(MAX_CORRECTION_ROUNDS + 1):
        _resume(graph, thread, {"decision": "revalidate", "resolver_role": "charge_nurse"})
        snap = graph.get_state({"configurable": {"thread_id": thread}})
        if snap.values.get("senior_required"):
            break

    assert snap.values.get("senior_required") is True
    assert _pending(graph, thread)["required_role"] == "shift_lead"


# ---- the validator itself --------------------------------------------------------


def test_the_validator_names_the_engine_that_could_not_answer(monkeypatch):
    monkeypatch.setattr(prolog, "safety_violations", lambda c: ([], "prolog: no engine"))
    with pytest.raises(safety.ValidatorUnavailable) as caught:
        safety.validate({"acuity": 3, "acuity_source": "system", "triage_records": [], "payload": {}})
    assert caught.value.engines == ["prolog"]


# ---- a re-file starts a new triage ------------------------------------------------

REFILE = {"nurse_proposed_acuity": 3, "chief_complaint": "chest_pain",
          "vitals": {"hr": 90, "bp": "120/80", "spo2": 98, "temp_c": 37.0}}


def _refile(graph, thread):
    graph.invoke(Command(resume={"event": "REASSESSMENT_TIMEOUT", "fire_id": "f1"}), config_for(thread))
    return hydrate(graph.invoke(Command(resume=REFILE), config_for(thread)))


def _leak_on_refile(monkeypatch):
    real = normalizer.build_model_payload
    monkeypatch.setattr(normalizer, "build_model_payload",
                        lambda *a, **k: {**real(*a, **k), "chief_complaint": "id 123456789"})


def _after_last(trail, label):
    return trail[len(trail) - trail[::-1].index(label):]


@pytest.mark.parametrize("opa_down_first", [False, True])
def test_a_leak_on_a_refile_halts_whatever_the_last_triage_left(graph, run, monkeypatch, opa_down_first):
    with pytest.MonkeyPatch.context() as mp:
        if opa_down_first:
            _opa_down(mp)
        _, _, thread = run(DEMO_CASES["clean"])
    calls = []
    monkeypatch.setattr(acuity_classifier, "classify", lambda p: calls.append(p))
    _leak_on_refile(monkeypatch)

    result = _refile(graph, thread)

    trail = transitions(result)
    assert Transition.V_HALT_PII in trail
    after = _after_last(trail, Transition.V_HALT_PII)
    assert Transition.CLEARED_TO_QUEUE not in after
    assert Transition.SAFETY_PASSED not in after
    assert calls == []
    assert result["redacted_payload"] == {}


def test_a_refile_with_everything_up_turns_the_confidence_gate_back_on(graph, run, monkeypatch):
    with pytest.MonkeyPatch.context() as mp:
        _opa_down(mp)
        state, _, thread = run(DEMO_CASES["clean"])
    assert state["gate_disabled"] and state["payload_unverified"]

    result = _refile(graph, thread)

    assert result["gate_disabled"] is False
    assert result["payload_unverified"] is False
