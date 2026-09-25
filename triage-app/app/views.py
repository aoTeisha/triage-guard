"""State → browser projections. One definition of what a case looks like to a UI.

Two shapes live here, and nothing else:

    case_view(state, pending)   the detail view — was `channel/api.py::_view`,
                                now shared by intake-channel and the board
    CaseCard / card_from_state  the board's card DTO, a strict subset

Both are *projections*: they read `TriageState` and return plain data. Neither
writes, and neither reaches for the CRM — a card must render through a CRM
outage (SPECIFICATION.md § degrade paths), so `patient_label` is derived from
the `crm_status` already on the case rather than fetched per card.

The allow-list in `CaseCard` is the point: raw payloads, redacted payloads and
`patient_history` never leave the server, and `tests/test_projection.py` in the
board service asserts exactly that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel

from app.states import ClinicalStatus, State
from app.verification import check_trace

# crm_status → what a human reads on a card. Not a name: identifier-class fields
# stay behind the CRM gate, and a card that showed one would make
# `verify_no_identifiers` a lie one screen over.
PATIENT_LABELS = {
    "found": "Registered",
    "not_found": "New patient",
    "db_error": "CRM down",
}


# Each pause's interrupt payload carries one marker key. A triaged case waiting
# in the queue counts as settled: intake's work on it is done.
_PAUSE_STATUS = {
    "gate": "awaiting_human_approval",
    "reassessment_pending": "awaiting_reassessment",
    "intake_fix_pending": "awaiting_intake_fix",
    "recovery_pending": "awaiting_recovery",
    "waiting_room": "settled",
}


def pause_status(pending: dict[str, Any] | None) -> str:
    """Name the pause a case is in, instead of calling every pause a gate."""
    for marker, status in _PAUSE_STATUS.items():
        if (pending or {}).get(marker):
            return status
    return "settled"


def case_view(state: dict[str, Any], pending: dict[str, Any] | None) -> dict[str, Any]:
    """What the browser needs: the outcome, the gate if any, and the trail.

    Also re-reads that trail with the after-run trace check on every call, so
    a nurse opening or acting on a case sees whether it broke a safety rule
    without anyone having to go looking for it.
    """
    trace = check_trace(state.get("audit_log", []))
    return {
        "case_id": state.get("case_id"),
        "status": pause_status(pending),
        "control_state": state.get("control_state"),
        "outcome": state.get("intake_outcome"),
        "missing_fields": state.get("missing_fields", []),
        "reason": state.get("intake_reason"),
        "acuity": state.get("acuity"),
        "acuity_source": state.get("acuity_source"),
        "acuity_gap": state.get("acuity_gap"),
        "safety_passed": state.get("safety_passed"),
        "approved": state.get("approved"),
        "release_reason": state.get("release_reason"),
        "degraded": state.get("degraded", []),
        "flags": state.get("flags", []),
        "gate": pending,
        "audit_log": state.get("audit_log", []),
        "trace_safety": trace.passed,
        "trace_violations": list(trace.violations),
    }


class CaseCard(BaseModel):
    """One card on the board. Holds only what the board renders."""

    case_id: str
    patient_id: Optional[str] = None
    patient_label: str = "Unknown"
    # What the case is actually about. Read from the *redacted* payload — the one
    # that already passed `verify_no_identifiers` — so the headline on a card is
    # clinical, never identifying.
    complaint: str = ""
    status: str
    acuity: Optional[int] = None
    bucket: Optional[str] = None
    acuity_source: Optional[str] = None
    nurse_proposed_acuity: Optional[int] = None
    system_proposed_acuity: Optional[int] = None
    arrival_time: Optional[str] = None
    waited_min: int = 0
    order_key: Optional[tuple[int, str]] = None
    flags: list[str] = []
    degraded: list[str] = []
    gate_pending: bool = False


def waited_minutes(arrival_time: str | None, now: datetime | None = None) -> int:
    """Whole minutes since arrival. Display only — the clock never re-sorts
    (SPECIFICATION.md § Queue ordering rule), so this value feeds no sort key.
    """
    if not arrival_time:
        return 0
    try:
        arrived = datetime.fromisoformat(arrival_time)
    except ValueError:
        return 0
    if arrived.tzinfo is None:
        arrived = arrived.replace(tzinfo=timezone.utc)
    delta = (now or datetime.now(timezone.utc)) - arrived
    return max(0, int(delta.total_seconds() // 60))


# Per status, which timestamp field is the natural "wait clock" basis. A
# status absent from this table falls back to arrival_time — the
# pre-existing behavior for human_review and reassessment_required.
_WAIT_CLOCK_FIELD: dict[str, str] = {
    ClinicalStatus.WAITING.value: "waiting_started_at",
    ClinicalStatus.TREATMENT_STARTED.value: "treatment_started_at",
    ClinicalStatus.PATIENT_RELEASED.value: "released_at",
}


def card_from_state(
    state: dict[str, Any], now: datetime | None = None, at_gate: bool = False
) -> CaseCard | None:
    """Project one persisted case onto a card, or None if it is not on the board.

    Two cases are not on the board: one that never reached the World plane (no
    `clinical_status` — it failed, was refused, or is still in intake), and one
    that is closed. Per the spec a closed case *leaves* the board; it stays
    reachable by direct link for its audit trail.

    `at_gate` says the run is suspended at `awaiting_human_approval` right now.
    That case has no `clinical_status` yet — the gate node writes `human_review`
    when it *returns*, and it has not returned — so without this it would be
    invisible on the board, which is precisely the case a charge nurse is needed
    for. Showing it in `human_review` is display, like the position badge: the
    board writes nothing, and the World-plane write still lands on resume.
    """
    status = state.get("clinical_status")
    status = getattr(status, "value", status)
    if at_gate and status in (None, ""):
        status = ClinicalStatus.HUMAN_REVIEW.value
    if not status or status == State.CASE_CLOSED.value:
        return None

    control_state = state.get("control_state")
    control_state = getattr(control_state, "value", control_state)
    order_key = state.get("order_key")

    wait_basis = state.get(_WAIT_CLOCK_FIELD.get(status, "arrival_time")) or state.get("arrival_time")

    return CaseCard(
        case_id=state.get("case_id") or "",
        patient_id=state.get("stable_patient_id"),
        patient_label=PATIENT_LABELS.get(state.get("crm_status") or "", "Unknown"),
        complaint=(state.get("redacted_payload") or {}).get("chief_complaint") or "",
        status=status,
        acuity=state.get("acuity"),
        bucket=getattr(state.get("acuity_bucket"), "value", state.get("acuity_bucket")),
        acuity_source=getattr(state.get("acuity_source"), "value", state.get("acuity_source")),
        nurse_proposed_acuity=state.get("nurse_proposed_acuity"),
        system_proposed_acuity=state.get("system_proposed_acuity"),
        arrival_time=state.get("arrival_time"),
        waited_min=waited_minutes(wait_basis, now),
        order_key=tuple(order_key) if order_key else None,
        flags=list(state.get("flags") or []),
        degraded=list(state.get("degraded") or []),
        gate_pending=at_gate or control_state == State.AWAITING_HUMAN_APPROVAL.value,
    )


# The six World-plane statuses, in spec order. `case_closed` is not one of them.
# Only `formal_validation` has no writer yet (STATUS.md item 4's full execution
# machine) and renders empty — the board shows the gap rather than hiding it.
BOARD_COLUMNS = [s.value for s in ClinicalStatus]
