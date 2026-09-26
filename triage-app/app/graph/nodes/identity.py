"""resolving_identity — CRM lookup (CRM_FOUND, CRM_NEW, DUPLICATE_CASE, AF_DB)."""

from __future__ import annotations

from typing import Any

from app.crm_client import fetch_patient, fetch_patient_by_national_id
from app.deterministic import audit
from app.esi import age_band
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Transition
from app.states import State
from app.symbolic.datalog import find_duplicate_active_case
from app.verification import verify_patient_record


def resolving_identity(state: TriageState) -> dict[str, Any]:
    """Trade the national id the nurse typed for the internal one the CRM holds.

    The only crossing point between the two identifiers. Non-critical and
    fail-open: a DB outage degrades to intake-only data rather than stopping the
    line, and a patient the CRM has no record of continues as a new patient with
    no internal id (no history, and no duplicate check to run).
    """
    try:
        # First pass: trade the typed number for the internal id. Later passes (a
        # re-file) have no national id left, so they refresh by the internal one.
        result = (fetch_patient_by_national_id(state.national_id, timeout=1.0)
                  if state.national_id
                  else fetch_patient(state.stable_patient_id or "", timeout=1.0))
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
                                Transition.AF_DB)],
        }

    check = verify_patient_record(record)
    if not check.passed:
        return {
            "control_state": State.RESOLVING_IDENTITY.value,
            "retry_count": _bump(state, "crm"),
            "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                                "discard_output", "; ".join(check.violations),
                                Transition.V_RETRY)],
        }

    found = status == "found"
    if found:
        # Imported here, not at module scope: `app.runner` imports
        # `app.graph` (for `build_graph`), and this module is imported while
        # `app.graph` is still assembling itself — a module-scope import
        # here would be circular. Same reasoning as the lazy pyswip import
        # in app/symbolic/prolog.py.
        from app.runner import all_case_summaries

        duplicate = find_duplicate_active_case(
            record.get("stable_patient_id"), all_case_summaries(exclude_case_id=state.case_id)
        )
        if duplicate:
            return {
                "control_state": State.INPUT_REJECTED.value,
                "audit_log": [audit(state.case_id, State.INPUT_REJECTED, "notify_user",
                                    f"case already open: {duplicate}", Transition.DUPLICATE_CASE)],
            }
    internal_id = ((record or {}).get("stable_patient_id") if found
                   else None) or state.stable_patient_id
    # What enters the case from the record: the internal id, the history with
    # name and date of birth stripped, and the age band derived from that date
    # (SPECIFICATION.md § Identity resolution). The date itself does not.
    history = ({k: v for k, v in (record or {}).items()
                if k not in ("name", "date_of_birth", "national_id", "stable_patient_id")}
               if found else None)
    band = None
    if found and (record or {}).get("date_of_birth"):
        try:
            band = age_band(record["date_of_birth"])
        except ValueError:
            band = None      # an unreadable date: no band, and decision point D will say so
    return {
        "control_state": State.RESOLVING_IDENTITY.value,
        "crm_status": status,
        "stable_patient_id": internal_id,
        "age_band": band,
        # The national id has served its one purpose. Keeping it would put an
        # identifier in every checkpoint and audit record (I11).
        "national_id": None,
        "raw_payload": {k: v for k, v in state.raw_payload.items() if k != "national_id"},
        "patient_history": history,
        "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                            "fetch_patient_data",
                            f"record found, resolved to {internal_id}" if found
                            else "new patient, no history",
                            Transition.CRM_FOUND if found else Transition.CRM_NEW)],
    }


def crm_fallback(state: TriageState, reason: str = "") -> dict[str, Any]:
    """AF_DB — the CRM client raised rather than returning a status.

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
                            Transition.AF_DB)],
    }
