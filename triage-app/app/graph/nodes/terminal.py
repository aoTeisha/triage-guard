"""monitoring, agent_failed, action_denied — the three run-ending terminals."""

from __future__ import annotations

from typing import Any

from app.deterministic import audit
from app.graph.state import TriageState
from app.labels import Arrow
from app.states import ClinicalStatus, State


def monitoring(state: TriageState) -> dict[str, Any]:
    """Cleared to the queue. This slice ends here."""
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


def agent_failed(state: TriageState) -> dict[str, Any]:
    """Halt. Leaves only on AGENT_RECOVERED, which re-enters at the failed stage."""
    return {
        "control_state": State.AGENT_FAILED.value,
        "audit_log": [audit(state.case_id, State.AGENT_FAILED, "alert_technician",
                            f"halted at {state.failed_stage or 'unknown stage'}; "
                            "awaiting AGENT_RECOVERED", Arrow.AF_RECOVER)],
    }


def action_denied(state: TriageState) -> dict[str, Any]:
    """The BLK row. Writes no case state — only the refusal record.

    The case stays exactly where it was; a later event re-enters the graph.
    """
    return {
        "control_state": State.ACTION_DENIED.value,
        "audit_log": [audit(state.case_id, State.ACTION_DENIED, "emit_event_log",
                            "attempted action refused; case did not move", Arrow.BLK)],
    }
