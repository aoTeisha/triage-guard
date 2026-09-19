"""The four documented intake paths, end to end, on mocks."""

from __future__ import annotations

from app.mock_cases import DEMO_CASES
from app.states import State
from tests.conftest import arrows


def test_clean_case_reaches_the_queue(run):
    state, pending, _ = run(DEMO_CASES["clean"])

    # Reaching the queue is a real pause: nobody answers it directly, a
    # reassessment timer firing does, so it's a "waiting_room" interrupt
    # rather than a gate awaiting a human decision.
    assert pending == {"case_id": DEMO_CASES["clean"]["case_id"], "waiting_room": True}
    assert state["control_state"] == State.MONITORING.value
    assert state["safety_passed"] is True
    assert state["approved"] is True
    assert state["clinical_status"] == "waiting"


def test_clean_case_walks_the_documented_arrows_in_order(run):
    """The audit trail should record every major step of a clean case's
    journey through the graph, in order, so it can be checked against
    expected behavior.
    """
    state, _, _ = run(DEMO_CASES["clean"])
    trail = arrows(state)

    # Every agent-driven step (parsing intake, classifying acuity, validating
    # safety, escalating to a human, monitoring the wait) is traced as an
    # "invoke" / "result" pair of audit entries. The CRM lookup and the
    # payload-redaction step aren't agents — they're plain on-entry actions —
    # but they're recorded the same way for the same traceability.
    assert trail[:4] == ["1a", "2", "3", "4"]
    assert trail.index("4b") < trail.index("4b·found" if "4b·found" in trail else "AF·db")
    assert trail.index("5") < trail.index("6")     # build payload -> payload clean
    assert trail.index("7") < trail.index("8")     # invoke classifier -> acuity proposed
    assert trail[-2:] == ["11·pass", "13"]         # cleared to queue, timer running


def test_missing_fields_stops_at_the_request(run):
    state, _, _ = run(DEMO_CASES["missing"])

    assert state["control_state"] == State.MISSING_FIELDS_REQUESTED.value
    assert arrows(state)[-1] == "16"
    assert "nurse_proposed_acuity" in state["missing_fields"]


def test_missing_fields_case_never_reaches_the_model(run):
    """No acuity is proposed for an incomplete submission."""
    state, _, _ = run(DEMO_CASES["missing"])

    assert state["system_proposed_acuity"] is None
    assert state["redacted_payload"] == {}


def test_unusable_submission_stops(run):
    state, _, _ = run(DEMO_CASES["failed"])

    assert state["control_state"] == State.SUBMISSION_FAILED.value
    assert arrows(state)[-1] == "17"


def test_injection_is_rejected_before_anything_is_classified(run):
    """The spec's forbidden sequence is 'injection reaches the model'."""
    state, _, _ = run(DEMO_CASES["injection"])

    assert state["control_state"] == State.INPUT_REJECTED.value
    assert arrows(state)[-1] == "18"
    assert state["redacted_payload"] == {}
    assert state["system_proposed_acuity"] is None
    assert "8" not in arrows(state)


def test_missing_fields_case_gets_no_invented_queue_position(run):
    """`nurse_proposed_acuity` is mandatory and never inferred, so a case without
    one cannot be given an order_key — and must not be given a guessed acuity to fake it.
    """
    state, _, _ = run(DEMO_CASES["missing"])
    assert state["order_key"] is None


def test_no_identifier_reaches_the_model_payload(run):
    """The privacy invariant, checked on the real payload the classifier saw."""
    state, _, _ = run(DEMO_CASES["clean"])
    payload = state["redacted_payload"]

    assert payload["case_id"] == "case-0001"
    for identifier in ("name", "stable_patient_id", "date_of_birth", "dob", "phone"):
        assert identifier not in payload


def test_every_audit_record_carries_case_and_state(run):
    """An audit row that cannot be attributed to a case is not an audit row."""
    state, _, _ = run(DEMO_CASES["clean"])

    for row in state["audit_log"]:
        assert row["case_id"] == "case-0001"
        assert row["control_state"]
        assert row["at"]


def test_missing_fields_pause_and_the_same_case_continues(graph, run):
    """I10 + I2: the case waits for the fields instead of ending, then continues
    as the same case with its original arrival time.
    """
    from langgraph.types import Command

    from app.runner import config_for, hydrate

    first, pending, thread = run(DEMO_CASES["missing"])
    assert pending["intake_fix_pending"] is True

    result = hydrate(graph.invoke(
        Command(resume={"nurse_proposed_acuity": 3,
                        "vitals": {"hr": 90, "bp": "120/80", "spo2": 98, "temp_c": 36.8}}),
        config_for(thread),
    ))

    assert result["arrival_time"] == first["arrival_time"]
    assert "1b.x" in arrows(result)
    assert graph.get_state(config_for(thread)).next == ("awaiting_reassessment",)  # queued


def test_completing_intake_cannot_overwrite_the_patient_id(graph, run):
    """Review fix 1: /fields fills only empty fields. An attempt to change an
    existing identity is ignored and recorded, never applied.
    """
    from langgraph.types import Command

    from app.runner import config_for, hydrate

    first, _, thread = run(DEMO_CASES["missing"])
    original_id = first["raw_payload"]["stable_patient_id"]

    result = hydrate(graph.invoke(
        Command(resume={"stable_patient_id": "SOMEONE-ELSE", "nurse_proposed_acuity": 3,
                        "vitals": {"hr": 90, "bp": "120/80", "spo2": 98, "temp_c": 36.8}}),
        config_for(thread),
    ))

    assert result["raw_payload"]["stable_patient_id"] == original_id
    fix = next(r for r in result["audit_log"] if r["action"] == "fields_submitted")
    assert fix["ignored_fields"] == ["stable_patient_id"]
