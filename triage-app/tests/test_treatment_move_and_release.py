"""Moving a patient into treatment, and releasing them, from the waiting-room
pause (docs/SPECIFICATION.md, manual-edit rule + release rule). Minimal
version: wired only from the `awaiting_reassessment` pause — see
docs/superpowers/plans/2026-09-17-treatment-move-and-release/findings.md for
why the human-gate and re-file pauses are out of scope.
"""

from __future__ import annotations

from langgraph.types import Command

from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.states import State
from tests.conftest import arrows


def _resume(graph, thread: str, **payload):
    return hydrate(graph.invoke(Command(resume=payload), config_for(thread)))


def test_charge_nurse_moves_a_waiting_patient_to_treatment(graph, run):
    case = DEMO_CASES["clean"]
    _, pending, thread = run(case)
    assert pending == {"case_id": case["case_id"], "waiting_room": True}

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="charge_nurse")

    assert result["clinical_status"] == "treatment_started"
    assert result["control_state"] == State.MONITORING.value
    assert "19" in arrows(result)        # Arrow.MOVE_CONFIRMED
    snapshot = graph.get_state(config_for(thread))
    assert snapshot.next, "case stays parked at the same pause, ready for release next"


def test_a_plain_nurse_can_also_start_treatment(graph, run):
    """move_authorized allows nurse or charge_nurse or shift_lead — unlike release."""
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")
    assert result["clinical_status"] == "treatment_started"


def test_charge_nurse_releases_a_waiting_patient(graph, run):
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)

    result = _resume(graph, thread, event="RELEASE_REQUESTED",
                      reason="discharge", actor_role="charge_nurse")

    assert result["control_state"] == State.CASE_CLOSED.value
    assert result["clinical_status"] == "patient_released"
    assert result["release_reason"] == "discharge"
    assert "REL" in arrows(result)
    snapshot = graph.get_state(config_for(thread))
    assert not snapshot.next, "the run must actually end, not stay paused"


def test_release_after_treatment_started_also_works(graph, run):
    """The demo path: waiting -> treatment started -> released, with no
    formal_validation step in between (release is state-independent).
    """
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    result = _resume(graph, thread, event="RELEASE_REQUESTED",
                      reason="discharge", actor_role="charge_nurse")

    assert result["clinical_status"] == "patient_released"
    assert result["control_state"] == State.CASE_CLOSED.value


def test_release_refuses_a_plain_nurse(graph, run):
    """Unlike an unauthorized gate resolution (test_denial.py), this denial
    must NOT end the run at `action_denied` — that would permanently drop a
    still-waiting patient out of the reassessment safety net over a routine
    mis-typed actor_role. The case stays parked, retriable by a legitimate
    follow-up.
    """
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)

    result = _resume(graph, thread, event="RELEASE_REQUESTED",
                      reason="discharge", actor_role="nurse")

    assert result.get("clinical_status") != "patient_released"
    assert result["control_state"] == State.MONITORING.value
    assert "BLK" in arrows(result)
    snapshot = graph.get_state(config_for(thread))
    assert snapshot.next, "a denied release must not end the run"


def test_a_denied_release_can_be_retried_by_a_charge_nurse(graph, run):
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _resume(graph, thread, event="RELEASE_REQUESTED", reason="discharge", actor_role="nurse")

    result = _resume(graph, thread, event="RELEASE_REQUESTED",
                      reason="discharge", actor_role="charge_nurse")

    assert result["clinical_status"] == "patient_released"


def test_a_stale_reassessment_timer_after_treatment_started_does_not_revert_status(graph, run):
    """The timer scheduled at queue-entry keeps ticking independently of a
    later move. If it fires after treatment has started, it must not knock
    clinical_status back to reassessment_required.
    """
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    result = _resume(graph, thread, event="REASSESSMENT_TIMEOUT", fire_id="stale-1")

    assert result["clinical_status"] == "treatment_started"
    assert result["control_state"] == State.MONITORING.value
    snapshot = graph.get_state(config_for(thread))
    assert snapshot.next, "must stay parked, not fall through to reassessment_required"


def test_a_duplicate_move_requested_after_treatment_started_is_denied(graph, run):
    """A replayed/duplicate MOVE_REQUESTED for a patient already in
    treatment must not silently re-confirm (a second Arrow.MOVE_CONFIRMED
    row with no error) — it must be denied and the case must stay parked.
    """
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="charge_nurse")

    assert result["clinical_status"] == "treatment_started"
    assert result["control_state"] == State.MONITORING.value
    assert arrows(result).count("19") == 1        # no duplicate MOVE_CONFIRMED
    assert "BLK" in arrows(result)
    snapshot = graph.get_state(config_for(thread))
    assert snapshot.next, "must stay parked, not silently re-confirm or end the run"
