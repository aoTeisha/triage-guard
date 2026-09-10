"""resolving_identity — CRM lookup (arrows 4b·found, 4b·new, AF·db)."""

from __future__ import annotations

from typing import Any

from app.crm_client import fetch_patient
from app.deterministic import audit
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Arrow
from app.states import State
from app.verification import verify_patient_record


def resolving_identity(state: TriageState) -> dict[str, Any]:
    """CRM lookup by stable patient ID. Non-critical and fail-open: a DB outage
    degrades to intake-only data rather than stopping the line.
    """
    try:
        result = fetch_patient(state.stable_patient_id or "", timeout=1.0)
        status, record = result.status, result.record
    except Exception:
        status, record = "db_error", None

    if status == "db_error":
        return {
            "control_state": State.RESOLVING_IDENTITY.value,
            "crm_status": status,
            "degraded": ["crm"],
            "flags": ["crm_down_intake_only"],
            "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                                "alert_technician", "DB unreachable, degraded",
                                Arrow.AF_DB)],
        }

    check = verify_patient_record(record)
    if not check.passed:
        return {
            "control_state": State.RESOLVING_IDENTITY.value,
            "retry_count": _bump(state, "crm"),
            "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                                "discard_output", "; ".join(check.violations),
                                Arrow.V_RETRY)],
        }

    found = status == "found"
    return {
        "control_state": State.RESOLVING_IDENTITY.value,
        "crm_status": status,
        "patient_history": check.checked if found else None,
        "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                            "fetch_patient_data",
                            "record found" if found else "new patient, no history",
                            Arrow.CRM_FOUND if found else Arrow.CRM_NEW)],
    }


def crm_fallback(state: TriageState, reason: str = "") -> dict[str, Any]:
    """AF·db — the CRM client raised rather than returning a status.

    Same outcome as the db_error branch inside `resolving_identity`: continue on
    intake-only data. Split into its own function so the error handler and the
    inline branch cannot drift apart.
    """
    return {
        "control_state": State.RESOLVING_IDENTITY.value,
        "crm_status": "db_error",
        "degraded": ["crm"],
        "flags": ["crm_down_intake_only"],
        "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                            "alert_technician",
                            "CRM unreachable, degraded"
                            + (f" ({reason})" if reason else ""),
                            Arrow.AF_DB)],
    }
