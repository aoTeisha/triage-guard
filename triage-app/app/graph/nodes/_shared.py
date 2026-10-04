"""Shared helper used across the node modules."""

from __future__ import annotations

from typing import Any

from app import crm_client
from app.budgets import CRM_WRITEBACK_RETRY_MINUTES
from app.deterministic import (
    SHIFT_LEAD_STANDS_IN,
    audit,
    audit_denial,
    move_authorized,
    now_iso,
    release_authorized,
)
from app.events import Event
from app.graph.state import TriageState
from app.guards import is_esi_level
from app.labels import Transition
from app.monitor import timers
from app.states import ClinicalStatus, State


def _bump(state: TriageState, agent: str) -> dict[str, int]:
    """Increment this agent's shared retry counter (§ retry_budget_left)."""
    return {agent: state.retry_count.get(agent, 0) + 1}


def is_release(answer: dict[str, Any] | None) -> bool:
    """True if a pause was answered with a release request rather than its own answer."""
    return (answer or {}).get("event") == Event.RELEASE_REQUESTED.value


def release_case(state: TriageState, answer: dict[str, Any], at: State) -> dict[str, Any]:
    """Release from any pause (I9): a valid reason and a charge role close the
    case; otherwise the attempt is refused and the case stays where it was.
    """
    actor_role = answer.get("actor_role", state.actor_role)
    reason = answer.get("reason", "")
    authorized, why = release_authorized(reason, actor_role)
    if not authorized:
        return {"actor_role": actor_role,
                "audit_log": [audit_denial(state.case_id, at, why, layer="OPA (authorization)")]}
    released_at = now_iso()
    # The visit's data reaches the CRM (I17): now if it can, or by the sweeper's
    # retry if the CRM is down. Recorded *before* the release record, because a
    # closed case admits nothing but refusals after it (I20).
    values = state.model_dump() | {"released_at": released_at}
    if not state.stable_patient_id:
        writeback = audit(state.case_id, State.CASE_CLOSED, "crm_writeback_skipped",
                          "no CRM record for this patient: nothing to write the visit to")
    elif not is_esi_level(state.acuity):
        # Released before triage settled (left from the intake fix, or from
        # recovery). A visit with no level would poison the history every later
        # case for this patient is judged on, so nothing is written.
        writeback = audit(state.case_id, State.CASE_CLOSED, "crm_writeback_skipped",
                          "released before an acuity was settled: no visit to record")
    elif crm_client.patch_patient(state.stable_patient_id,
                                  {"new_visit": crm_client.visit_record(values)}, timeout=2.0) == "ok":
        writeback = audit(state.case_id, State.CASE_CLOSED, "crm_updated",
                          "visit written to the CRM")
    else:
        timers.schedule(timers.connection(), case_id=state.case_id, kind="crm_writeback",
                        schedule_seq=0, due_at=timers.due_in(CRM_WRITEBACK_RETRY_MINUTES))
        writeback = audit(state.case_id, State.CASE_CLOSED, "crm_writeback_deferred",
                          f"CRM unreachable; the visit will be retried every "
                          f"{CRM_WRITEBACK_RETRY_MINUTES} min until it lands")
    # OPA could not answer and a shift lead signed instead: say so, and mark the
    # case as having run without it.
    stood_in = why.startswith(SHIFT_LEAD_STANDS_IN)
    return {
        "actor_role": actor_role,
        "control_state": State.CASE_CLOSED.value,
        "clinical_status": ClinicalStatus.PATIENT_RELEASED.value,
        "released_at": released_at,
        "release_reason": reason,
        "degraded": ["opa_signoff"] if stood_in else [],
        "audit_log": [writeback,
                      audit(state.case_id, State.CASE_CLOSED, "sign_release",
                            f"release signed: {reason}" + (f" ({why})" if stood_in else ""),
                            Transition.RELEASE, engines=[] if stood_in else ["OPA"])],
    }


def _positions(raw: Any) -> list[int]:
    """The queue positions in a move request, as a sorted list of ints. Anything
    else is dropped: a position is a number, and nothing else may reach the log."""
    if not isinstance(raw, (list, tuple)):
        return []
    return sorted({p for p in raw if isinstance(p, int) and not isinstance(p, bool)})


def move_case(state: TriageState, answer: dict[str, Any], at: State) -> dict[str, Any]:
    """Move into treatment (I5, MOVE_CONFIRMED), from the waiting-room pause or
    the re-filing pause: OPA's `move` rule or, with OPA down, a shift lead.

    The queue facts come from the board's server, which computes them over the
    global queue (`board.ordering.ahead_of`): `skipped_positions` are those still
    in line ahead of this patient, and an out-of-order move needs a `skip_reason`
    from the fixed set. Both are recorded on the move. A caller with no queue
    (a single-case CLI run) sends none, and nothing is skipped.

    The caller sets anything the source pause needs on top (`control_state`).
    """
    actor_role = answer.get("actor_role", state.actor_role)
    skipped = _positions(answer.get("skipped_positions"))
    skip_reason = answer.get("skip_reason") or None
    authorized, why = move_authorized(state.safety_passed, state.approved, actor_role,
                                      state.safety_waived, skipped=skipped, skip_reason=skip_reason)
    if not authorized:
        return {"actor_role": actor_role,
                "audit_log": [audit_denial(state.case_id, at, why, layer="OPA (authorization)")]}
    stood_in = why.startswith(SHIFT_LEAD_STANDS_IN)   # OPA down, a shift lead signed
    explanation = "move to treatment confirmed"
    queue: dict[str, Any] = {}
    if isinstance(answer.get("queue_position"), int):
        queue["queue_position"] = answer["queue_position"]
    if skipped:
        # Authorized, so the reason is one of the fixed set: safe to log.
        explanation += (f", ahead of {', '.join(f'#{p}' for p in skipped)}"
                        f" ({skip_reason})")
        queue |= {"skipped_positions": skipped, "skip_reason": skip_reason}
    if stood_in:
        explanation += f" ({why})"
    return {
        "actor_role": actor_role,
        "clinical_status": ClinicalStatus.TREATMENT_STARTED.value,
        "treatment_started_at": now_iso(),
        "degraded": ["opa_signoff"] if stood_in else [],
        "audit_log": [audit(state.case_id, at, "emit_event_log", explanation,
                            Transition.MOVE_CONFIRMED, engines=[] if stood_in else ["OPA"],
                            **queue)],
    }
