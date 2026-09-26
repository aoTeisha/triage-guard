"""Shared helper used across the node modules."""

from __future__ import annotations

from typing import Any

from app import crm_client
from app.budgets import CRM_WRITEBACK_RETRY_MINUTES
from app.deterministic import audit, audit_denial, now_iso, release_authorized
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
    return {
        "actor_role": actor_role,
        "control_state": State.CASE_CLOSED.value,
        "clinical_status": ClinicalStatus.PATIENT_RELEASED.value,
        "released_at": released_at,
        "release_reason": reason,
        "audit_log": [writeback,
                      audit(state.case_id, State.CASE_CLOSED, "sign_release",
                            f"release signed: {reason}", Transition.RELEASE)],
    }
