"""monitoring, agent_failed, action_denied.

`monitoring` and its follow-up `awaiting_reassessment` are the waiting-room
pause: a case sits here until a reassessment timer fires or a nurse reports
a change. `agent_failed` and `action_denied` are terminal states that end
the run outright.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.types import interrupt

from app.budgets import REASSESSMENT_INTERVAL_MINUTES
from app.deterministic import audit
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
    band = state.acuity or 5
    due_at = (
        datetime.now(timezone.utc)
        + timedelta(minutes=REASSESSMENT_INTERVAL_MINUTES.get(band, REASSESSMENT_INTERVAL_MINUTES[5]))
    ).isoformat()
    timers.schedule(timers.connection(), case_id=state.case_id, kind="reassessment",
                     cycle=state.reassessment_cycle, due_at=due_at)

    return {
        "control_state": State.MONITORING.value,
        "approved": True,
        "clinical_status": ClinicalStatus.WAITING.value,
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
    # of some unrelated earlier audit row that also has no fire_id.
    if fire_id and any(rec.get("fire_id") == fire_id for rec in state.audit_log):
        return {"control_state": State.MONITORING.value}

    update: dict[str, Any] = {
        "control_state": State.MONITORING.value,
        "reassessment_cycle": state.reassessment_cycle + 1,
        "audit_log": [
            audit(state.case_id, State.MONITORING, "emit_event_log",
                  f"reassessment timer fired: {fired.get('event')}", Arrow.REASSESSMENT_DUE,
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
    """Halts the run. The only way out is an AGENT_RECOVERED event, which
    re-enters the graph at whichever stage originally failed.
    """
    return {
        "control_state": State.AGENT_FAILED.value,
        "audit_log": [audit(state.case_id, State.AGENT_FAILED, "alert_technician",
                            f"halted at {state.failed_stage or 'unknown stage'}; "
                            "awaiting AGENT_RECOVERED", Arrow.AF_RECOVER)],
    }


def action_denied(state: TriageState) -> dict[str, Any]:
    """Records that an attempted action was refused. Writes no other case
    state — the case stays exactly where it was, and a later event is what
    re-enters the graph, not this node.
    """
    return {
        "control_state": State.ACTION_DENIED.value,
        "audit_log": [audit(state.case_id, State.ACTION_DENIED, "emit_event_log",
                            "attempted action refused; case did not move", Arrow.BLK)],
    }
