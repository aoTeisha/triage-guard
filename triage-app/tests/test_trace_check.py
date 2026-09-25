"""The post-run trace check: reads a finished audit log in order and reports
where a safety rule broke. Unit tests use hand-built logs; the
integration tests at the bottom run real cases through the graph and require
every one of them to pass.
"""

from __future__ import annotations

from langgraph.types import Command

from app.budgets import MAX_CORRECTION_ROUNDS
from app.labels import Transition as T
from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.verification import check_trace


def _record(t: T | None) -> dict:
    """One well-formed audit record, as `deterministic.audit` builds it."""
    record = {"at": "2026-09-25T00:00:00+00:00", "case_id": "c1", "control_state": "x",
              "action": "a", "explanation": "e", "transition": t.value if t else None}
    if t == T.BLK:
        record["denying_layer"] = "OPA (authorization)"
    return record


def _log(*transitions: T | None) -> list[dict]:
    return [_record(t) for t in transitions]


CLEAN = (T.ENTRY, T.PAYLOAD_CLEAN, T.ACUITY_PROPOSED, T.SAFETY_PASSED,
         T.CLEARED_TO_QUEUE, T.TIMER_RUNNING)


def test_an_empty_log_passes():
    assert check_trace([]).passed


def test_the_clean_path_passes():
    assert check_trace(_log(*CLEAN, T.MOVE_CONFIRMED)).passed


def test_records_without_a_transition_are_ignored():
    assert check_trace(_log(None, *CLEAN, None)).passed


def test_queueing_without_a_safety_pass_is_a_bypass():
    result = check_trace(_log(T.ENTRY, T.ACUITY_PROPOSED, T.CLEARED_TO_QUEUE))
    assert not result.passed
    assert result.structural
    assert result.violations[0].startswith("record 2:")
    assert "no bypass" in result.violations[0]


def test_queueing_on_a_failed_verdict_is_a_bypass():
    result = check_trace(_log(T.SAFETY_PASSED, T.SAFETY_FAILED, T.CLEARED_TO_QUEUE))
    assert not result.passed


def test_a_major_gap_needs_a_human_answer_before_the_queue():
    unanswered = _log(T.ACUITY_GAP_MAJOR, T.SAFETY_PASSED, T.CLEARED_TO_QUEUE)
    answered = _log(T.ACUITY_GAP_MAJOR, T.ESCALATION_RECORDED, T.GATE_ACUITY_RESOLVED,
                    T.SAFETY_PASSED, T.CLEARED_TO_QUEUE)
    assert not check_trace(unanswered).passed
    assert check_trace(answered).passed


def test_a_corrected_safety_failure_passes_once_revalidated():
    log = _log(T.SAFETY_FAILED, T.ESCALATION_RECORDED, T.GATE_SAFETY_CORRECTED,
               T.SAFETY_PASSED, T.CLEARED_TO_QUEUE)
    assert check_trace(log).passed


def test_moving_before_the_queue_is_a_bypass():
    result = check_trace(_log(T.SAFETY_PASSED, T.MOVE_CONFIRMED))
    assert not result.passed
    assert "record 1:" in result.violations[0]


def test_a_second_treatment_start_is_flagged():
    result = check_trace(_log(*CLEAN, T.MOVE_CONFIRMED, T.MOVE_CONFIRMED))
    assert not result.passed
    assert any("single treatment start" in v for v in result.violations)


def test_refused_attempts_are_not_moves():
    assert check_trace(_log(*CLEAN, T.BLK, T.MOVE_CONFIRMED, T.BLK)).passed


def test_a_rerun_starts_a_new_triage():
    # Safety passed in the first triage only; after the re-file the case
    # moves without being re-validated and re-queued.
    stale = _log(*CLEAN, T.REASSESSMENT_DUE, T.FRONT_DOOR_RERUN, T.MOVE_CONFIRMED)
    fresh = _log(*CLEAN, T.REASSESSMENT_DUE, T.FRONT_DOOR_RERUN,
                 T.SAFETY_PASSED, T.CLEARED_TO_QUEUE, T.MOVE_CONFIRMED)
    assert not check_trace(stale).passed
    assert check_trace(fresh).passed


def test_a_human_answer_from_the_previous_triage_does_not_carry_over():
    log = _log(T.ACUITY_GAP_MAJOR, T.GATE_ACUITY_RESOLVED, T.SAFETY_PASSED, T.CLEARED_TO_QUEUE,
               T.FRONT_DOOR_RERUN, T.ACUITY_GAP_MAJOR, T.SAFETY_PASSED, T.CLEARED_TO_QUEUE)
    assert not check_trace(log).passed


# ---- correct, then revalidate ------------------------------------------------

def test_a_failure_that_passes_without_a_correction_is_flagged():
    # An earlier acuity answer counts as "a human answered", but the failure
    # itself still needs its own correction.
    log = _log(T.ACUITY_GAP_MAJOR, T.GATE_ACUITY_RESOLVED, T.SAFETY_FAILED,
               T.SAFETY_PASSED, T.CLEARED_TO_QUEUE)
    result = check_trace(log)
    assert not result.passed
    assert any("correct then revalidate" in v for v in result.violations)


def test_a_correction_before_a_second_failure_does_not_count():
    log = _log(T.SAFETY_FAILED, T.GATE_SAFETY_CORRECTED, T.SAFETY_FAILED,
               T.SAFETY_PASSED, T.CLEARED_TO_QUEUE)
    assert any("correct then revalidate" in v for v in check_trace(log).violations)


# ---- bounded correction loop --------------------------------------------------

def _rounds(n: int) -> tuple[T, ...]:
    return (T.GATE_SAFETY_CORRECTED, T.SAFETY_FAILED) * n


def test_revalidating_past_the_round_limit_is_flagged():
    log = _log(T.SAFETY_FAILED, *_rounds(MAX_CORRECTION_ROUNDS + 1))
    result = check_trace(log)
    assert not result.passed
    assert any("bounded correction loop" in v for v in result.violations)


def test_the_round_limit_then_a_senior_passes():
    log = _log(T.SAFETY_FAILED, *_rounds(MAX_CORRECTION_ROUNDS),
               T.GATE_SAFETY_CORRECTED, T.SENIOR_ESCALATION, T.BLK,
               *_rounds(2), T.GATE_SAFETY_CORRECTED, T.SAFETY_PASSED, T.CLEARED_TO_QUEUE)
    result = check_trace(log)
    assert result.passed, result.violations


# ---- audit record structure ---------------------------------------------------

def test_a_record_missing_a_field_is_flagged():
    log = _log(*CLEAN)
    del log[1]["explanation"]
    result = check_trace(log)
    assert result.violations == ("record 1: audit record: missing explanation",)


def test_a_refusal_without_its_layer_is_flagged():
    log = _log(*CLEAN, T.BLK)
    del log[-1]["denying_layer"]
    assert any("audit record" in v and "denying_layer" in v for v in check_trace(log).violations)


def test_a_record_from_another_case_is_flagged():
    log = _log(*CLEAN)
    log[2]["case_id"] = "c2"
    assert any("audit record" in v and "c2" in v for v in check_trace(log).violations)


# ---- nothing changes after close ---------------------------------------------

def test_a_write_after_release_is_flagged():
    result = check_trace(_log(*CLEAN, T.RELEASE, T.MOVE_CONFIRMED))
    assert any("closed case" in v for v in result.violations)


def test_refusals_after_release_are_fine():
    assert check_trace(_log(*CLEAN, T.RELEASE, T.BLK, T.BLK)).passed


# ---- real runs through the graph ---------------------------------------------

def _resume(graph, thread, payload):
    return hydrate(graph.invoke(Command(resume=payload), config_for(thread)))


def test_every_demo_case_passes(run):
    for case in DEMO_CASES.values():
        state, _, _ = run(case)
        assert check_trace(state["audit_log"]).passed, (case["case_id"], check_trace(state["audit_log"]).violations)


def test_a_real_treatment_move_passes(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])
    state = _resume(graph, thread, {"event": "MOVE_REQUESTED", "actor_role": "charge_nurse"})
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_a_replayed_move_still_passes_because_it_is_refused(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])
    _resume(graph, thread, {"event": "MOVE_REQUESTED", "actor_role": "charge_nurse"})
    state = _resume(graph, thread, {"event": "MOVE_REQUESTED", "actor_role": "charge_nurse"})
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_a_low_confidence_case_resolved_at_the_gate_passes(graph, run, monkeypatch):
    from app.actors import acuity_classifier
    from app.schemas import AcuityProposal

    monkeypatch.setattr(acuity_classifier, "classify", lambda payload: AcuityProposal(
        system_proposed_acuity=2, confidence=0.31, acuity_source="system", rationale="mock: unsure"))
    # Nurse agrees with the classifier (both 2), so the only reason to gate is
    # the low confidence, not an acuity gap.
    case = dict(DEMO_CASES["clean"], case_id="case-conf", nurse_proposed_acuity=2)
    _, pending, thread = run(case)
    assert pending and pending["gate"] == "low_confidence"
    state = _resume(graph, thread, {"decision": "use_system_acuity", "resolver_role": "charge_nurse"})
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_a_corrected_safety_failure_passes(graph, run, monkeypatch):
    from app.actors import safety
    from app.schemas import SafetyVerdict

    monkeypatch.setattr(safety, "validate", lambda case: SafetyVerdict(verdict="fail", reasons=["unsafe"]))
    _, _, thread = run(DEMO_CASES["clean"])
    monkeypatch.undo()   # the corrected case passes safety
    state = _resume(graph, thread, {"decision": "corrected", "resolver_role": "charge_nurse",
                                    "corrections": {"acuity": 2}})
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_an_exhausted_correction_loop_handed_to_a_senior_passes(graph, run, monkeypatch):
    from app.actors import safety
    from app.schemas import SafetyVerdict

    monkeypatch.setattr(safety, "validate", lambda case: SafetyVerdict(verdict="fail", reasons=["unsafe"]))
    _, _, thread = run(DEMO_CASES["clean"])
    for i in range(6):   # alternate 1 and 2 so every round is a real correction
        state = _resume(graph, thread, {"decision": "corrected", "resolver_role": "charge_nurse",
                                        "corrections": {"acuity": 1 + i % 2}})
        if state.get("senior_required"):
            break
    assert state["senior_required"] is True
    monkeypatch.undo()   # the shift lead's correction now passes safety
    state = _resume(graph, thread, {"decision": "corrected", "resolver_role": "shift_lead",
                                    "corrections": {"acuity": 4}})
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_a_refiled_case_moved_after_its_new_triage_passes(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])
    _resume(graph, thread, {"event": "REASSESSMENT_TIMEOUT", "fire_id": "f1"})
    _resume(graph, thread, {"nurse_proposed_acuity": 2, "chief_complaint": "worse chest pain",
                            "vitals": {"hr": 118, "bp": "150/95", "spo2": 94, "temp_c": 37.4}})
    state = _resume(graph, thread, {"event": "MOVE_REQUESTED", "actor_role": "charge_nurse"})
    assert state["clinical_status"] == "treatment_started"
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_escalate_further_then_a_shift_lead_correction_passes(graph, run, monkeypatch):
    from app.actors import safety
    from app.schemas import SafetyVerdict

    monkeypatch.setattr(safety, "validate", lambda case: SafetyVerdict(verdict="fail", reasons=["unsafe"]))
    _, _, thread = run(DEMO_CASES["clean"])
    _resume(graph, thread, {"decision": "escalate_further", "resolver_role": "charge_nurse"})
    monkeypatch.undo()
    state = _resume(graph, thread, {"decision": "corrected", "resolver_role": "shift_lead",
                                    "corrections": {"acuity": 2}})
    assert state["control_state"] == "monitoring"
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_a_crashed_validator_corrected_at_the_gate_passes(graph, run, monkeypatch):
    from app.actors import safety

    def down(case):
        raise safety.ValidatorUnavailable("engine down")

    monkeypatch.setattr(safety, "validate", down)
    _, pending, thread = run(DEMO_CASES["clean"])
    assert pending and pending["gate"] == "safety_fail"
    monkeypatch.undo()
    state = _resume(graph, thread, {"decision": "corrected", "resolver_role": "charge_nurse",
                                    "corrections": {"acuity": 2}})
    assert T.V_EXHAUSTED_SAFETY in [r["transition"] for r in state["audit_log"]]
    assert state["control_state"] == "monitoring"
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_a_released_case_passes(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])
    state = _resume(graph, thread, {"event": "RELEASE_REQUESTED", "reason": "discharge",
                                    "actor_role": "charge_nurse"})
    assert state["control_state"] == "case_closed"
    result = check_trace(state["audit_log"])
    assert result.passed, result.violations
