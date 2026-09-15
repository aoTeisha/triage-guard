"""The fire state machine: attempting to deliver a timer's event into its
case (dispatch), confirming whether a delivery that lost its acknowledgment
actually landed (reconcile), sending notify-only reminders (notify), and
de-duplicating and escalating repeated failures.

`dispatch` and `reconcile` take an explicit `graph` argument so the sweeper,
in production, can pass the real, process-cached `app.runner.graph()`, while
tests pass in an isolated one instead. This module is the only code that
writes a timer's `fire_state` column — everything else only ever reads it.
"""

from __future__ import annotations

import hashlib
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

# Which recipient to remind at each reminder step: step 0 notifies just the
# nurse assigned to the case, step 1 widens the reminder to any charge nurse.
_GATE_RUNG_RECIPIENTS = {0: "assigned_nurse", 1: "any_charge_nurse"}


def fire_id(case_id: str, kind: str, cycle: int, due_at: str) -> str:
    """A deterministic id for one timer firing, stable across re-dispatches
    of the same cycle. Used as a de-dupe key both here (before dispatching
    again) and inside the graph itself (`awaiting_reassessment` checks its
    own audit log for this id) — a second line of defense in case the same
    fire ever gets dispatched twice.
    """
    raw = f"{case_id}:{kind}:{cycle}:{due_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def dispatch(conn, timer: dict[str, Any], *, graph) -> str:
    """Attempts to deliver a due timer's event into its case. Moves the
    timer's state from DUE to DISPATCHING, then to either DELIVERED or
    FAILED depending on what happens.

    If the case isn't currently paused in `monitoring` when this runs,
    either it already accepted this same fire on an earlier attempt (a
    reassessment timer only ever gets created because the case reached
    `monitoring` once), or the case doesn't exist at all — both cases return
    without attempting a resume, which is what makes calling this function
    twice for the same timer safe.

    There's no dispatch timeout here: `graph.invoke` is a local, synchronous,
    in-process call, so there's no network round-trip that could hang. A
    timer can still end up `UNKNOWN` though — if the *process itself* dies
    mid-call, `claim_retryable` later picks up the row once its lease
    expires, and `reconcile` (below) figures out what actually happened,
    treating that case the same as a lost acknowledgment.
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
        resume["timer_gap"] = True  # flags an honest record of an unwatched window

    try:
        graph.invoke(Command(resume=resume), config)
    except Exception as exc:  # noqa: BLE001 — any exception here is a real, actionable failure to record, not something to let crash the sweeper
        timers.set_state(conn, timer["timer_id"], "FAILED", last_error=str(exc))
        return "FAILED"

    timers.set_state(conn, timer["timer_id"], "DELIVERED", fire_id=fid)
    return "DELIVERED"


def reconcile(conn, timer: dict[str, Any], *, graph) -> str:
    """Figures out what actually happened to a timer stuck in UNKNOWN state
    (whose acknowledgment was lost) — resolving it to DELIVERED, FAILED,
    keeping it UNKNOWN for another attempt, or ESCALATED_TO_HUMAN if the
    retry budget for reconciling is spent.

    Checks the case's own audit log for this `fire_id` first, before drawing
    any other conclusion — that's the most reliable evidence of whether the
    fire actually landed.
    """
    case_id = timer["case_id"]
    fid = timer.get("fire_id") or fire_id(case_id, timer["kind"], timer["cycle"], timer["due_at"])
    config = config_for(case_id)
    attempts = timer.get("attempts", 0) + 1

    try:
        history = list(graph.get_state_history(config))
        current = graph.get_state(config)
    except Exception as exc:  # noqa: BLE001 — the store or graph is unreachable; treat as an infrastructure failure, not a clinical one
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
        # The case is still sitting at the same pause it was at before this
        # fire — proof the fire never applied, so it's safe to redispatch it
        # again with the same fire_id.
        timers.set_state(conn, timer["timer_id"], "FAILED", fire_id=fid)
        return "FAILED"

    # Neither proof that the fire landed nor proof that it didn't — the case
    # has moved on to something else, so it's ambiguous whether this fire
    # caused that or something unrelated did.
    return _inconclusive(conn, timer, fid, attempts, recipient_class="charge_nurse",
                          reason="reassessment_overdue")


def notify(conn, timer: dict[str, Any], *, graph) -> str:
    """Sends a gate reminder (nudging staff about an unanswered approval
    request). Notify-only: it doesn't change any case state and there's no
    acknowledgment to wait for, so it never passes through the
    `DISPATCHING`/`UNKNOWN` states `dispatch`/`reconcile` use.

    Returns `CANCELLED` if the gate has already been resolved by the time
    this runs, rather than sending a now-stale reminder; `FAILED` if the
    recipient has already hit their notification budget for this time
    window — that's always recorded as a `FAILED` state, never a silent drop.
    """
    case_id = timer["case_id"]
    snapshot = graph.get_state(config_for(case_id))

    # `monitoring` (used for reassessment) is split across two nodes so its
    # `control_state` field commits to storage before the actual pause
    # happens. `awaiting_human_approval` isn't split that way — it pauses
    # (via `interrupt()`) before it finishes running, so while paused,
    # `snapshot.values["control_state"]` still reflects whatever the
    # *previous* node set, not this one. Only `snapshot.next` (which node is
    # queued up to run next) reliably shows that the case is actually paused
    # here.
    if State.AWAITING_HUMAN_APPROVAL not in snapshot.next:
        timers.set_state(conn, timer["timer_id"], "CANCELLED")
        return "CANCELLED"

    recipient_class = _GATE_RUNG_RECIPIENTS.get(timer["cycle"], "any_charge_nurse")
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
    if attempts >= RECONCILE_BUDGET:
        timers.set_state(conn, timer["timer_id"], "ESCALATED_TO_HUMAN", fire_id=fid, attempts=attempts)
        timers.record_escalation(conn, case_id=timer["case_id"], fire_id=fid, channel="notification_strip",
                                  recipient_class=recipient_class, reason=reason)
        return "ESCALATED_TO_HUMAN"
    fields: dict[str, Any] = {"fire_id": fid, "attempts": attempts}
    if last_error:
        fields["last_error"] = last_error
    timers.set_state(conn, timer["timer_id"], "UNKNOWN", **fields)
    return "UNKNOWN"
