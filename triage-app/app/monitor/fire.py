"""Fire state machine: dispatch, reconcile, and notify for claimed timers.

`dispatch`/`reconcile`/`notify` take `graph` so the sweeper passes the real
cached graph and tests pass an isolated one. Only this module writes `fire_state`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from langgraph.types import Command

from app.budgets import (
    NOTIFICATION_BUDGET_PER_WINDOW,
    NOTIFICATION_WINDOW_MINUTES,
    RECONCILE_BUDGET,
)
from app.monitor import timers
from app.runner import config_for
from app.states import State

# Reminder kind -> node the case must still be paused at. Moved on = cancel.
_REMINDER_PAUSES = {
    "gate_reminder": State.AWAITING_HUMAN_APPROVAL.value,
    "reassessment_reminder": "awaiting_reassessment_submission",
    "senior_reminder": State.AWAITING_HUMAN_APPROVAL.value,
}


def fire_id(case_id: str, kind: str, cycle: int, due_at: datetime | str) -> str:
    """Stable id for one firing. De-dupe key here and in the graph's audit log."""
    raw = f"{case_id}:{kind}:{cycle}:{due_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def dispatch(conn, timer: dict[str, Any], *, graph) -> str:
    """Deliver a due timer into its case. DUE -> DISPATCHING -> DELIVERED | FAILED.

    Safe to call twice: a case not paused in `monitoring` already took this
    fire (or does not exist), so no resume is attempted.
    """
    case_id = timer["case_id"]
    fid = fire_id(case_id, timer["kind"], timer["cycle"], timer["due_at"])
    config = config_for(case_id)
    timers.set_state(conn, timer["timer_id"], "DISPATCHING", fire_id=fid)

    current = graph.get_state(config).values
    if current.get("control_state") != State.MONITORING.value:
        if not current:
            timers.set_state(conn, timer["timer_id"], "FAILED", last_error="case not found")
            return "FAILED"
        timers.set_state(conn, timer["timer_id"], "DELIVERED")
        return "DELIVERED"

    resume = {"event": "REASSESSMENT_TIMEOUT", "fire_id": fid}
    if timer.get("timer_gap"):
        resume["timer_gap"] = True  # records that nobody was watching for a while

    # No timeout: `graph.invoke` is in-process. If the process dies mid-call the
    # lease expires, `claim_retryable` picks the row up, and `reconcile` sorts it out.
    try:
        graph.invoke(Command(resume=resume), config)
    except Exception as exc:  # noqa: BLE001 — record the failure, never crash the sweeper
        timers.set_state(conn, timer["timer_id"], "FAILED", last_error=str(exc))
        return "FAILED"

    timers.set_state(conn, timer["timer_id"], "DELIVERED")
    return "DELIVERED"


def reconcile(conn, timer: dict[str, Any], *, graph) -> str:
    """Work out what happened to an UNKNOWN timer (its ack was lost).

    Audit log has this `fire_id` -> DELIVERED. Case still in `monitoring` ->
    FAILED (safe to redispatch). Otherwise UNKNOWN again, or
    ESCALATED_TO_HUMAN once `RECONCILE_BUDGET` is spent.
    """
    case_id = timer["case_id"]
    fid = timer.get("fire_id") or fire_id(case_id, timer["kind"], timer["cycle"], timer["due_at"])
    config = config_for(case_id)
    attempts = timer.get("attempts", 0) + 1

    try:
        history = list(graph.get_state_history(config))
        current = graph.get_state(config)
    except Exception as exc:  # noqa: BLE001 — store unreachable: infra problem, page a technician
        return _inconclusive(conn, timer, fid, attempts, recipient_class="technician",
                              reason="store_unreachable", last_error=str(exc))

    landed = any(
        rec.get("fire_id") == fid
        for snapshot in history
        for rec in snapshot.values.get("audit_log", [])
    )
    if landed:
        timers.set_state(conn, timer["timer_id"], "DELIVERED", fire_id=fid)
        return "DELIVERED"

    if current.values.get("control_state") == State.MONITORING.value:
        # Still at the same pause: the fire never applied, safe to redispatch.
        timers.set_state(conn, timer["timer_id"], "FAILED", fire_id=fid)
        return "FAILED"

    # Case moved on, but no audit proof this fire did it. Ambiguous.
    return _inconclusive(conn, timer, fid, attempts, recipient_class="charge_nurse",
                          reason="reassessment_overdue")


def notify(conn, timer: dict[str, Any], *, graph) -> str:
    """Send a gate or re-filing reminder. Notify-only: no case state change, no ack.

    CANCELLED if the pause it was about is already over (or unknown kind).
    FAILED if the recipient's notification budget for this window is spent.
    """
    case_id = timer["case_id"]
    snapshot = graph.get_state(config_for(case_id))

    # `awaiting_human_approval` pauses via `interrupt()` before it finishes, so
    # `values["control_state"]` still shows the previous node. `snapshot.next`
    # is the only reliable "paused here" signal.
    pause = _REMINDER_PAUSES.get(timer["kind"])
    if pause is None or pause not in snapshot.next:
        timers.set_state(conn, timer["timer_id"], "CANCELLED")
        return "CANCELLED"

    # Gate step 0 nudges the assigned nurse; every later step widens to any charge nurse.
    recipient_class = (
        _GATE_RUNG_RECIPIENTS[timer["cycle"] % len(_GATE_RUNG_RECIPIENTS)]
        if timer["kind"] == "gate_reminder"
        else "any_shift_lead" if timer["kind"] == "senior_reminder"
        else "any_charge_nurse"
    )
    if timers.notification_count_in_window(
        conn, recipient_class=recipient_class, window_minutes=NOTIFICATION_WINDOW_MINUTES
    ) >= NOTIFICATION_BUDGET_PER_WINDOW:
        timers.set_state(conn, timer["timer_id"], "FAILED", last_error="notification budget exhausted")
        return "FAILED"

    reason = f"{timer['kind']}_{timer['cycle']}"
    timers.record_notification(conn, case_id=case_id, reason=reason, channel="notification_strip",
                                recipient_class=recipient_class)
    timers.set_state(conn, timer["timer_id"], "DELIVERED")
    return "DELIVERED"


def _inconclusive(conn, timer: dict[str, Any], fid: str, attempts: int, *,
                   recipient_class: str, reason: str, last_error: str | None = None) -> str:
    """Could not tell if the fire landed. Stay UNKNOWN, or escalate once the budget is spent."""
    if attempts >= RECONCILE_BUDGET:
        timers.set_state(conn, timer["timer_id"], "ESCALATED_TO_HUMAN", fire_id=fid, attempts=attempts)
        timers.record_escalation(conn, case_id=timer["case_id"], fire_id=fid, channel="notification_strip",
                                  recipient_class=recipient_class, reason=reason)
        return "ESCALATED_TO_HUMAN"
    fields: dict[str, Any] = {"fire_id": fid, "attempts": attempts}
    if last_error:  # only overwrite last_error when this attempt produced one
        fields["last_error"] = last_error
    timers.set_state(conn, timer["timer_id"], "UNKNOWN", **fields)
    return "UNKNOWN"
