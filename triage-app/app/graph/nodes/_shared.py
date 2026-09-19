"""Shared helper used across the node modules."""

from __future__ import annotations

from typing import Any

from app.deterministic import audit, audit_denial, now_iso, release_authorized
from app.events import Event
from app.graph.state import TriageState
from app.labels import Arrow
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
        return {"actor_role": actor_role, "audit_log": [audit_denial(state.case_id, at, why)]}
    return {
        "actor_role": actor_role,
        "control_state": State.CASE_CLOSED.value,
        "clinical_status": ClinicalStatus.PATIENT_RELEASED.value,
        "released_at": now_iso(),
        "release_reason": reason,
        "audit_log": [audit(state.case_id, State.CASE_CLOSED, "sign_release",
                            f"release signed: {reason}", Arrow.RELEASE)],
    }
