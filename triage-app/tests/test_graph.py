"""The four documented intake paths, end to end, on mocks."""

from __future__ import annotations

from app.graph import routers
from app.graph.state import TriageState
from app.labels import Route, Transition
from app.mock_cases import DEMO_CASES
from app.states import State
from tests.conftest import transitions


def test_route_after_identity_denies_a_rejected_duplicate():
    state = TriageState(case_id="c1", control_state=State.INPUT_REJECTED)
    assert routers.route_after_identity(state) == Route.DENIED


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


def test_clean_case_walks_the_documented_transitions_in_order(run):
    """The audit trail should record every major step of a clean case's
    journey through the graph, in order, so it can be checked against
    expected behavior.
    """
    state, _, _ = run(DEMO_CASES["clean"])
    trail = transitions(state)

    # Every agent-driven step (parsing intake, classifying acuity, validating
    # safety, escalating to a human, monitoring the wait) is traced as an
    # "invoke" / "result" pair of audit entries. The CRM lookup and the
    # payload-redaction step aren't agents — they're plain on-entry actions —
    # but they're recorded the same way for the same traceability.
    assert trail[:4] == [Transition.ENTRY, Transition.NORMALIZED,
                         Transition.RUN_VALIDATOR, Transition.SUBMISSION_VALID]
    assert trail.index(Transition.LOOKUP) < trail.index(
        Transition.CRM_FOUND if Transition.CRM_FOUND in trail else Transition.AF_DB)
    assert trail.index(Transition.BUILD_PAYLOAD) < trail.index(Transition.PAYLOAD_CLEAN)
    assert trail.index(Transition.RUN_CLASSIFIER) < trail.index(Transition.ACUITY_PROPOSED)
    assert trail[-2:] == [Transition.CLEARED_TO_QUEUE, Transition.TIMER_RUNNING]


def test_missing_fields_stops_at_the_request(run):
    state, _, _ = run(DEMO_CASES["missing"])

    assert state["control_state"] == State.MISSING_FIELDS_REQUESTED.value
    assert transitions(state)[-1] == Transition.MISSING_FIELDS
    assert "nurse_proposed_acuity" in state["missing_fields"]


def test_missing_fields_case_never_reaches_the_model(run):
    """No acuity is proposed for an incomplete submission."""
    state, _, _ = run(DEMO_CASES["missing"])

    assert state["system_proposed_acuity"] is None
    assert state["redacted_payload"] == {}


def test_unusable_submission_stops(run):
    state, _, _ = run(DEMO_CASES["failed"])

    assert state["control_state"] == State.SUBMISSION_FAILED.value
    assert transitions(state)[-1] == Transition.SUBMISSION_UNUSABLE


def test_injection_is_rejected_before_anything_is_classified(run):
    """The spec's forbidden sequence is 'injection reaches the model'."""
    state, _, _ = run(DEMO_CASES["injection"])

    assert state["control_state"] == State.INPUT_REJECTED.value
    assert transitions(state)[-1] == Transition.INVALID_INPUT
    assert state["redacted_payload"] == {}
    assert state["system_proposed_acuity"] is None
    assert Transition.ACUITY_PROPOSED not in transitions(state)


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
    for identifier in ("name", "national_id", "stable_patient_id", "date_of_birth", "dob", "phone"):
        assert identifier not in payload
    # I12's other half, and I4's independence: no prose, and not the nurse's answer.
    for not_for_the_model in ("free_text", "nurse_proposed_acuity", "channel"):
        assert not_for_the_model not in payload
    assert payload["chief_complaint"] == "chest_pain"


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
    assert Transition.FIELDS_RESUBMITTED in transitions(result)
    assert graph.get_state(config_for(thread)).next == ("awaiting_reassessment",)  # queued


def test_completing_intake_cannot_overwrite_the_patient_id(graph, run):
    """A fields resubmission fills what the parser asked for and nothing else.
    An identity the parser never asked about is ignored and recorded, so a
    second submission cannot re-point a case at another patient.
    """
    from langgraph.types import Command

    from app.runner import config_for, hydrate

    _, _, thread = run(DEMO_CASES["missing"])

    result = hydrate(graph.invoke(
        Command(resume={"national_id": "999999999", "nurse_proposed_acuity": 3,
                        "vitals": {"hr": 90, "bp": "120/80", "spo2": 98, "temp_c": 36.8}}),
        config_for(thread),
    ))

    fix = next(r for r in result["audit_log"] if r["action"] == "fields_submitted")
    assert fix["ignored_fields"] == ["national_id"]
    # Neither applied nor written down anywhere, and the case is not carrying a
    # national id at all by the time identity resolution has run (I11).
    assert "999999999" not in repr(result)
    assert result["national_id"] is None


def test_a_mistyped_acuity_is_fixable_in_place(graph, run):
    """I4/I13 at the front door: an acuity of 7 is present but unusable, so the
    case waits for the nurse rather than being rejected, and their corrected
    number replaces it. The fix pause used to accept only *empty* fields, which
    left a mistyped value unfixable.
    """
    from langgraph.types import Command

    from app.runner import config_for, hydrate

    first, pending, thread = run({**DEMO_CASES["clean"], "nurse_proposed_acuity": 7})

    assert pending["intake_fix_pending"] is True
    assert first["missing_fields"] == ["nurse_proposed_acuity"]
    assert first["order_key"] is None                 # never keyed at a level that does not exist

    result = hydrate(graph.invoke(
        Command(resume={"nurse_proposed_acuity": 2}), config_for(thread)))

    assert result["raw_payload"]["nurse_proposed_acuity"] == 2
    assert result["arrival_time"] == first["arrival_time"]
    assert tuple(result["order_key"])[0] == 2
