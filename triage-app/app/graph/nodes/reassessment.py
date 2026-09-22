"""reassessment_required and awaiting_reassessment_submission — re-enters
case intake after a reassessment timer fires, but only once a nurse actually
re-files with fresh observations.

Split into two nodes, the same way `monitoring`/`awaiting_reassessment` is in
`app/graph/nodes/terminal.py`: `reassessment_required` commits
`control_state` first, then `awaiting_reassessment_submission` pauses. A
node's state changes only commit once the node finishes running, and the
pause node doesn't finish until resumed — potentially much later — so
splitting them is what makes the commit visible immediately instead of only
after the nurse responds.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.budgets import REASSESSMENT_REMINDER_DELAY_MINUTES
from app.deterministic import assign_order_key, audit
from app.graph.nodes._shared import is_release, release_case
from app.graph.state import TriageState
from app.labels import Arrow
from app.monitor import timers
from app.states import ClinicalStatus, State


def reassessment_required(state: TriageState) -> dict[str, Any]:
    """Commits the state, and starts the clock on the nurse.

    The reminder is scheduled here rather than in the pause node for the same
    reason `monitoring` schedules the reassessment timer rather than
    `awaiting_reassessment` doing it: the pause node doesn't finish running
    until it is resumed, so anything it scheduled would only land after the
    wait it was meant to cover.
    """
    waits = state.refile_waits + 1
    due_at = timers.due_in(REASSESSMENT_REMINDER_DELAY_MINUTES)
    # ponytail: one rung, straight to the charge nurse, where the approval gate
    # has a two-step ladder. Add a second rung if reminders go unanswered in
    # practice — the recipient lookup in `fire.notify` is where it would go.
    timers.schedule(timers.connection(), case_id=state.case_id,
                     kind="reassessment_reminder", schedule_seq=waits, due_at=due_at)

    return {
        "control_state": State.REASSESSMENT_REQUIRED.value,
        "clinical_status": ClinicalStatus.REASSESSMENT_REQUIRED.value,
        "refile_waits": waits,
        "audit_log": [
            audit(state.case_id, State.REASSESSMENT_REQUIRED, "start_reassessment_timer",
                  "awaiting nurse re-file; reminder scheduled"),
        ],
    }


def awaiting_reassessment_submission(state: TriageState) -> dict[str, Any]:
    """The re-filing pause: freezes until a nurse submits fresh clinical
    observations (`POST /reassess/{case_id}` in intake-channel).

    Only the fields a nurse can actually change get overwritten. Everything
    else in `raw_payload` is carried over, and that is load-bearing rather
    than tidiness: `free_text` and `case_id` are in `REQUIRED_FIELDS` and are
    only ever supplied by the original submission, so building a payload from
    scratch here would send every re-file to `missing_fields_requested` and
    end the run for a patient who is physically still in the waiting room.
    """
    submitted = interrupt({"case_id": state.case_id, "reassessment_pending": True})
    if is_release(submitted):
        return release_case(state, submitted, State.REASSESSMENT_REQUIRED)

    fresh_payload = {
        **state.raw_payload,
        "nurse_proposed_acuity": submitted.get("nurse_proposed_acuity"),
        "chief_complaint": submitted.get("chief_complaint"),
        "vitals": submitted.get("vitals"),
    }

    return {
        "control_state": State.REASSESSMENT_REQUIRED.value,
        "raw_payload": fresh_payload,
        # A re-file starts a new triage. Values from the last one must not count:
        # a stale `acuity` skipped the gate on a big gap, and a stale `approved`
        # would let a move through (I4, I5). `order_key` is kept on purpose (I2).
        "acuity": None,
        "acuity_source": None,
        "human_decision": None,
        "escalation_reason": None,
        "safety_verdict": None,
        "safety_passed": False,
        "approved": False,
        "correction_rounds": 0,
        "senior_required": False,
        # Until the new acuity settles, the case queues by the nurse's new value,
        # as on first intake. Their acuity really changed, so I2 allows it.
        "order_key": assign_order_key(submitted.get("nurse_proposed_acuity"), state.arrival_time)
        if submitted.get("nurse_proposed_acuity") is not None else state.order_key,
        "audit_log": [
            audit(state.case_id, State.REASSESSMENT_REQUIRED, "emit_event_log",
                  "nurse re-filed with fresh observations", Arrow.FRONT_DOOR_RERUN),
        ],
    }
