"""Moving a patient into treatment from the re-filing pause: their reassessment
fell due, but they are next in line and the treating clinician will assess them
anyway (docs/SPECIFICATION.md, `reassessment_required` + `MOVE_REQUESTED` row).

Same OPA move rule as the waiting-room move, over the last triage's
`safety_passed`/`approved`, which a due timer does not reset; only a re-file
starts a new triage.
"""

from __future__ import annotations

from langgraph.types import Command

from app.graph import build_graph
from app.labels import Transition
from app.mock_cases import DEMO_CASES
from app.monitor import timers
from app.runner import config_for, hydrate
from app.states import State
from app.verification import check_trace
from tests.conftest import transitions


def _resume(graph, thread: str, **payload):
    return hydrate(graph.invoke(Command(resume=payload), config_for(thread)))


def _due_for_reassessment(graph, run):
    """A clean case, queued, whose reassessment timer has fired."""
    case = DEMO_CASES["clean"]
    _, _, thread = run(case, thread=case["case_id"])
    _resume(graph, thread, event="REASSESSMENT_TIMEOUT", fire_id="f1")
    snapshot = graph.get_state(config_for(thread))
    assert snapshot.values["control_state"] == State.REASSESSMENT_REQUIRED.value
    assert "awaiting_reassessment_submission" in snapshot.next
    return thread


def _reminders(conn, thread: str) -> list[dict]:
    return [t for t in timers.all_rows(conn)
            if t["case_id"] == thread and t["kind"] == "reassessment_reminder"]


def test_the_move_is_a_drawn_edge_from_the_refile_pause():
    edges = {(e.source, e.target) for e in build_graph().get_graph().edges}
    assert ("awaiting_reassessment_submission", "awaiting_reassessment") in edges


def test_a_patient_due_for_reassessment_can_be_moved_to_treatment(graph, run):
    thread = _due_for_reassessment(graph, run)

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    assert result["clinical_status"] == "treatment_started"
    # Parked at the same treatment pause a waiting-room move leaves a case in,
    # so "treatment complete" and release work exactly as they do from there.
    assert result["control_state"] == State.MONITORING.value
    snapshot = graph.get_state(config_for(thread))
    assert "awaiting_reassessment" in snapshot.next
    move = next(r for r in result["audit_log"] if r.get("transition") == Transition.MOVE_CONFIRMED)
    assert move["control_state"] == State.REASSESSMENT_REQUIRED.value
    assert move["engines"] == ["OPA"]
    # No new triage was started: the stale payload was not re-parsed.
    assert Transition.FRONT_DOOR_RERUN not in transitions(result)


def test_moving_cancels_the_refile_reminder(graph, run, conn):
    thread = _due_for_reassessment(graph, run)
    assert [t["fire_state"] for t in _reminders(conn, thread)] == ["SCHEDULED"]

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="charge_nurse")

    assert [t["fire_state"] for t in _reminders(conn, thread)] == ["CANCELLED"]
    cancel = next(r for r in result["audit_log"] if r["action"] == "cancel_reminder")
    assert "1 re-file reminder(s) cancelled" in cancel["explanation"]


def test_a_refused_move_leaves_the_reminder_and_the_pause(graph, run, conn):
    thread = _due_for_reassessment(graph, run)

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="porter")

    assert result["clinical_status"] == "reassessment_required"
    assert result["control_state"] == State.REASSESSMENT_REQUIRED.value
    assert transitions(result)[-1] == Transition.BLK
    assert result["audit_log"][-1]["denying_layer"] == "OPA (authorization)"
    assert "porter" in result["audit_log"][-1]["explanation"]
    assert "awaiting_reassessment_submission" in graph.get_state(config_for(thread)).next
    assert [t["fire_state"] for t in _reminders(conn, thread)] == ["SCHEDULED"]

    # Still retriable, and a re-file still works after a refusal.
    refiled = _resume(graph, thread, nurse_proposed_acuity=3, chief_complaint="chest_pain",
                      vitals={"hr": 90, "bp": "120/80", "spo2": 98, "temp_c": 37.0})
    assert Transition.FRONT_DOOR_RERUN in transitions(refiled)


def test_opa_down_only_a_shift_lead_moves_from_the_refile_pause(graph, run, conn, outage_switch):
    thread = _due_for_reassessment(graph, run)
    outage_switch.set_down("opa", True)

    refused = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="charge_nurse")
    assert refused["clinical_status"] == "reassessment_required"
    assert "shift lead sign-off required" in refused["audit_log"][-1]["explanation"]

    moved = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="shift_lead")
    assert moved["clinical_status"] == "treatment_started"
    assert "opa_signoff" in moved["degraded"]
    assert moved["audit_log"][-1]["engines"] == []
    assert [t["fire_state"] for t in _reminders(conn, thread)] == ["CANCELLED"]


def test_a_case_without_a_cleared_triage_is_refused_at_the_refile_pause(graph, run):
    """Defense in depth: the last triage's flags are read, never assumed. Every
    real path here has them set; force them off and OPA must refuse."""
    thread = _due_for_reassessment(graph, run)
    graph.update_state(config_for(thread), {"safety_passed": False, "approved": False})

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="charge_nurse")

    assert result["clinical_status"] == "reassessment_required"
    assert "safety not passed" in result["audit_log"][-1]["explanation"]


def test_after_the_move_treatment_completes_and_releases_as_usual(graph, run, monkeypatch):
    from app import crm_client

    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")
    thread = _due_for_reassessment(graph, run)
    _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    # A replayed move is still refused once in treatment.
    replay = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")
    assert transitions(replay).count(Transition.MOVE_CONFIRMED) == 1
    # A stale timer does not knock the patient back to reassessment.
    stale = _resume(graph, thread, event="REASSESSMENT_TIMEOUT", fire_id="stale")
    assert stale["clinical_status"] == "treatment_started"

    treated = _resume(graph, thread, event="TREATMENT_COMPLETE", actor_role="nurse")
    assert treated["clinical_status"] == "formal_validation"
    released = _resume(graph, thread, event="RELEASE_REQUESTED", reason="discharge",
                       actor_role="charge_nurse")
    assert released["control_state"] == State.CASE_CLOSED.value
    result = check_trace(released["audit_log"])
    assert result.passed, result.violations


def test_the_trace_check_accepts_the_move_from_the_refile_pause(graph, run):
    thread = _due_for_reassessment(graph, run)
    state = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    result = check_trace(state["audit_log"])
    assert result.passed, result.violations


def test_a_deterioration_report_then_a_move_passes_the_trace_check(graph, run):
    case = DEMO_CASES["clean"]
    _, _, thread = run(case, thread=case["case_id"])
    _resume(graph, thread, event="DETERIORATION_DETECTED", signal="spo2 88", actor_role="nurse")
    state = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse")

    assert state["clinical_status"] == "treatment_started"
    assert check_trace(state["audit_log"]).passed
