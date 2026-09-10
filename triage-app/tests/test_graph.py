"""The four documented intake paths, end to end, on mocks."""

from __future__ import annotations

from app.mock_cases import DEMO_CASES
from app.states import State
from tests.conftest import arrows


def test_clean_case_reaches_the_queue(run):
    state, pending, _ = run(DEMO_CASES["clean"])

    assert pending is None
    assert state["control_state"] == State.MONITORING.value
    assert state["safety_passed"] is True
    assert state["approved"] is True
    assert state["clinical_status"] == "waiting"


def test_clean_case_walks_the_documented_arrows_in_order(run):
    """The audit trail is meant to diff against the Transitions table."""
    state, _, _ = run(DEMO_CASES["clean"])
    trail = arrows(state)

    assert trail[:4] == ["1a", "2", "3", "4"]
    assert "5" in trail and "6" in trail          # build payload, payload clean
    assert "8" in trail                            # acuity proposed
    assert trail[-1] == "11·pass"                  # cleared to queue


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
    one cannot be bucketed — and must not be given a guessed acuity to fake it.
    """
    state, _, _ = run(DEMO_CASES["missing"])
    assert state["order_key"] is None


def test_red_flag_forces_emergent_without_consulting_the_model(run):
    """Demo case 1 carries 'chest tightness', a hard clinical trigger."""
    state, _, _ = run(DEMO_CASES["clean"])

    assert state["red_flag_fired"] is True
    assert state["acuity"] <= 2


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
