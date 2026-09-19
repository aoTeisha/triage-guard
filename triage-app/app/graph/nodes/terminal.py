"""monitoring, agent_failed.

`monitoring` and its follow-up `awaiting_reassessment` are the waiting-room
pause: a case sits here until a reassessment timer fires or a nurse reports
a change. `agent_failed` halts a case; `awaiting_recovery` holds it until the
technician reports AGENT_RECOVERED.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.budgets import REASSESSMENT_INTERVAL_MINUTES
from app.deterministic import audit, audit_denial, move_authorized, now_iso, release_authorized
from app.events import Event
from app.graph.state import TriageState
from app.labels import Arrow
from app.monitor import timers
from app.states import ClinicalStatus, State


def monitoring(state: TriageState) -> dict[str, Any]:
    """Cleared to the queue: starts the reassessment timer and records that
    the case is now waiting.

    Kept as a separate node from the actual wait (`awaiting_reassessment`) so
    this audit entry is visible the moment the case reaches the queue,
    rather than only after the timer eventually fires. A node's state
    changes only commit once the node finishes running, and
    `awaiting_reassessment` doesn't finish until it's resumed — potentially
    much later.
    """
    # A queued case with no acuity is broken; re-look now rather than on the
    # least urgent interval (I16).
    minutes = REASSESSMENT_INTERVAL_MINUTES[state.acuity] if state.acuity else 0
    due_at = timers.due_in(minutes)
    timers.schedule(timers.connection(), case_id=state.case_id, kind="reassessment",
                     cycle=state.reassessment_cycle, due_at=due_at)

    return {
        "control_state": State.MONITORING.value,
        "approved": True,
        "clinical_status": ClinicalStatus.WAITING.value,
        "waiting_started_at": now_iso(),
        "audit_log": [
            audit(state.case_id, State.MONITORING, "emit_event_log",
                  "cleared to queue", Arrow.CLEARED_TO_QUEUE),
            audit(state.case_id, State.MONITORING, "start_reassessment_timer",
                  "queued, timer running", Arrow.TIMER_RUNNING),
        ],
    }


def awaiting_reassessment(state: TriageState) -> dict[str, Any]:
    """The waiting-room monitor's own pause: the case sits here until the
    background sweeper process delivers a `REASSESSMENT_TIMEOUT` event, or a
    nurse reports `DETERIORATION_DETECTED` via the board's `/deteriorated`
    endpoint. `control_state` stays `monitoring` throughout — there's no
    separate status for "waiting, but the timer already fired."

    De-dupes on `fire_id` as a safety net against a redundant resume being
    delivered twice: as soon as this node resumes and the case loops back
    around to its *next* `monitoring` pause, the sweeper's own "is this case
    still at the pause I dispatched to?" check can no longer tell the
    difference — only this node, by checking whether it already logged this
    exact `fire_id`, can catch a duplicate at that point.
    """
    fired = interrupt({"case_id": state.case_id, "waiting_room": True})
    fire_id = fired.get("fire_id")

    # Only compare fire_ids when there actually is one. A nurse-initiated
    # DETERIORATION_DETECTED event carries no fire_id at all (`None`), and
    # without this guard, `None == None` would make it look like a duplicate
    # of some unrelated earlier audit row that also has no fire_id. Runs
    # before the event branches below too — a redundant resume must
    # short-circuit regardless of which event it carries.
    if fire_id and any(rec.get("fire_id") == fire_id for rec in state.audit_log):
        return {"control_state": State.MONITORING.value}

    def denied(why: str) -> dict[str, Any]:
        # Denial stays parked, not ended: a mis-typed actor_role is routine
        # input from a UI button. The case must stay retriable by a
        # legitimate follow-up, not fall out of the reassessment safety net.
        # The approval gate treats its own refusals the same way.
        return {
            "actor_role": actor_role,
            "audit_log": [audit_denial(state.case_id, State.MONITORING, why)],
        }

    event = fired.get("event")
    actor_role = fired.get("actor_role", state.actor_role)

    if event == Event.MOVE_REQUESTED.value:
        if state.clinical_status == ClinicalStatus.TREATMENT_STARTED.value:
            # A duplicate/replayed move for a case already in treatment —
            # move_authorized has no notion of clinical_status, so without
            # this it would silently re-confirm and append a second
            # MOVE_CONFIRMED row with no error.
            return denied("move refused: already in treatment")
        authorized, why = move_authorized(state.safety_passed, state.approved, actor_role)
        if not authorized:
            return denied(why)
        return {
            "actor_role": actor_role,
            "clinical_status": ClinicalStatus.TREATMENT_STARTED.value,
            "treatment_started_at": now_iso(),
            "audit_log": [audit(state.case_id, State.MONITORING, "emit_event_log",
                                 "move to treatment confirmed", Arrow.MOVE_CONFIRMED)],
        }

    if event == Event.RELEASE_REQUESTED.value:
        reason = fired.get("reason", "")
        authorized, why = release_authorized(reason, actor_role)
        if not authorized:
            return denied(why)
        return {
            "actor_role": actor_role,
            "control_state": State.CASE_CLOSED.value,
            "clinical_status": ClinicalStatus.PATIENT_RELEASED.value,
            "released_at": now_iso(),
            "release_reason": reason,
            "audit_log": [audit(state.case_id, State.CASE_CLOSED, "sign_release",
                                 f"release signed: {reason}", Arrow.RELEASE)],
        }

    update: dict[str, Any] = {
        "control_state": State.MONITORING.value,
        "reassessment_cycle": state.reassessment_cycle + 1,
        "audit_log": [
            audit(state.case_id, State.MONITORING, "emit_event_log",
                  f"reassessment timer fired: {event}", Arrow.REASSESSMENT_DUE,
                  fire_id=fire_id),
        ],
    }
    if fired.get("timer_gap"):
        # Honest record that a window existed where nobody was watching this
        # case, surfaced on the case's own record — not just buried in the
        # timers table.
        update["flags"] = ["timer_gap"]
    return update


def agent_failed(state: TriageState) -> dict[str, Any]:
    """Halts the case and alerts the technician. `awaiting_recovery` then
    holds it until AGENT_RECOVERED, instead of ending the run (I10).
    """
    return {
        "control_state": State.AGENT_FAILED.value,
        "audit_log": [audit(state.case_id, State.AGENT_FAILED, "alert_technician",
                            f"halted at {state.failed_stage or 'unknown stage'}; "
                            "awaiting AGENT_RECOVERED", Arrow.AF_RECOVER)],
    }


def awaiting_recovery(state: TriageState) -> dict[str, Any]:
    """Wait for the technician's AGENT_RECOVERED, then re-enter at the stage
    that failed (arrow AF·recover). A case that is still broken there halts
    again; a critical step gets no retries.
    """
    interrupt({"case_id": state.case_id, "recovery_pending": True,
               "halted_at": state.failed_stage})
    return {
        "audit_log": [audit(state.case_id, State.AGENT_FAILED, "resume_at_failed_stage",
                            f"recovered; resuming at {state.failed_stage}",
                            Arrow.AF_RECOVER)],
    }
