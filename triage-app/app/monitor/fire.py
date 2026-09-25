"""Fire state machine: deciding what a claimed timer gets (`handle`), then
dispatch, reconcile, or a reminder.

The imperative code here only proposes. Which event a timer gets is chosen by
the b-threads in `app.monitor.bthreads`, cross-checked against
`app.symbolic.prolog`, and every side effect passes the OPA gate in
`app.symbolic.opa` immediately before it happens. A missing or failing engine
is a refusal, never a fallback.

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
from app.monitor import bthreads, timers
from app.observability import case_trace, record_outcome
from app.runner import config_for
from app.states import State
from app.symbolic import opa, prolog

# Who a gate reminder nudges, by rung: rung 0 is the nurse assigned to the
# case, rung 1 widens to any charge nurse. `gate.py` schedules both rungs of a
# visit back to back — sequence numbers `visit` and `visit + 1`, where `visit`
# is always even — so the sequence number's parity *is* the rung. That keeps
# the two reminders of one visit on distinct timer ids without a second column.
_GATE_RUNG_RECIPIENTS = {0: "assigned_nurse", 1: "any_charge_nurse"}

# Reminder kind -> node the case must still be paused at. Moved on = cancel.
_REMINDER_PAUSES = {
    "gate_reminder": State.AWAITING_HUMAN_APPROVAL.value,
    "reassessment_reminder": "awaiting_reassessment_submission",
    "senior_reminder": State.AWAITING_HUMAN_APPROVAL.value,
}


def fire_id(case_id: str, kind: str, schedule_seq: int, due_at: datetime | str) -> str:
    """Stable id for one firing. De-dupe key here and in the graph's audit log."""
    raw = f"{case_id}:{kind}:{schedule_seq}:{due_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _recipient_class(timer: dict[str, Any]) -> str:
    """Who this reminder goes to. A gate reminder widens by rung (see
    `_GATE_RUNG_RECIPIENTS`); a senior reminder always goes to a shift lead.
    """
    if timer["kind"] == "gate_reminder":
        return _GATE_RUNG_RECIPIENTS[timer["schedule_seq"] % len(_GATE_RUNG_RECIPIENTS)]
    if timer["kind"] == "senior_reminder":
        return "any_shift_lead"
    return "any_charge_nurse"


def _context(conn, timer: dict[str, Any], *, graph) -> dict[str, Any]:
    """The facts every symbolic layer reasons over for one claimed timer,
    built once so BPpy, Prolog and OPA all see the same snapshot. Raises if
    the graph is unreachable — `handle` turns that into the
    `store_unreachable` path rather than deciding on missing facts.
    """
    snapshot = graph.get_state(config_for(timer["case_id"]))
    pause = _REMINDER_PAUSES.get(timer["kind"])
    recipient = _recipient_class(timer)
    return {
        "timer_id": timer["timer_id"],
        "kind": timer["kind"],
        "fire_state": timer.get("fire_state") or "DUE",  # claim_due rows carry no fire_state
        "control_state": snapshot.values.get("control_state"),
        "case_exists": bool(snapshot.values),
        # `snapshot.next` is the only reliable "paused here" signal: the gate
        # pauses via `interrupt()` before it finishes, so while paused
        # `control_state` still shows the previous node.
        "pause_active": pause is not None and pause in snapshot.next,
        "notify_count": timers.notification_count_in_window(
            conn, recipient_class=recipient, window_minutes=NOTIFICATION_WINDOW_MINUTES),
        "notify_budget": NOTIFICATION_BUDGET_PER_WINDOW,
        "recipient_class": recipient,
    }


def handle(conn, timer: dict[str, Any], *, graph) -> str:
    """One claimed timer, start to finish: gather the facts, let the b-threads
    choose the action, insist Prolog reaches the same conclusion, record the
    chosen action, then execute it. Returns the resulting fire_state.
    """
    try:
        ctx = _context(conn, timer, graph=graph)
    except Exception as exc:  # noqa: BLE001 — graph/store unreachable: no facts to decide on, so don't
        fid = timer.get("fire_id") or fire_id(timer["case_id"], timer["kind"],
                                              timer["schedule_seq"], timer["due_at"])
        return _inconclusive(conn, timer, fid, timer.get("reconcile_attempts", 0) + 1,
                             recipient_class="technician", reason="store_unreachable", last_error=str(exc))

    selected, proposed = bthreads.select_action(ctx)
    expected, why = prolog.timer_action(ctx)
    if expected == "engine_unavailable":
        timers.set_state(conn, timer["timer_id"], "FAILED", last_error=f"engine_unavailable:prolog ({why})")
        return "FAILED"
    if selected.lower() != expected:
        timers.set_state(conn, timer["timer_id"], "FAILED",
                         last_error=f"layer_disagreement: bppy={selected} prolog={expected}")
        return "FAILED"
    timers.record_chosen_action(conn, timer["timer_id"],
                                selected if selected == proposed else f"{selected} (proposed {proposed}: {why})")

    if selected == "DISPATCH":
        return dispatch(conn, timer, graph=graph)
    if selected == "RECONCILE":
        return reconcile(conn, timer, graph=graph)
    if selected == "NOTIFY":
        return _send_reminder(conn, timer, ctx)
    if selected == "FAIL_BUDGET":
        timers.set_state(conn, timer["timer_id"], "FAILED", last_error="notification budget exhausted")
        return "FAILED"
    timers.set_state(conn, timer["timer_id"], "CANCELLED")
    return "CANCELLED"


def dispatch(conn, timer: dict[str, Any], *, graph) -> str:
    """Deliver a due timer into its case. DUE -> DISPATCHING -> DELIVERED | FAILED.

    Safe to call twice: a case not paused in `monitoring` already took this
    fire (or does not exist), so no resume is attempted.
    """
    case_id = timer["case_id"]
    fid = fire_id(case_id, timer["kind"], timer["schedule_seq"], timer["due_at"])
    config = config_for(case_id)
    timers.set_state(conn, timer["timer_id"], "DISPATCHING", fire_id=fid)

    current = graph.get_state(config).values
    if current.get("control_state") != State.MONITORING.value:
        if not current:
            timers.set_state(conn, timer["timer_id"], "FAILED", last_error="case not found")
            return "FAILED"
        timers.set_state(conn, timer["timer_id"], "DELIVERED")
        return "DELIVERED"

    # The last line before the side effect: the check above reconciles what
    # already happened, this decides whether the resume may happen now.
    gate = opa.evaluate({"action": "dispatch",
                         "case": {"control_state": current.get("control_state")},
                         "timer": {"fire_state": timer.get("fire_state") or "DUE"}})
    if not gate["allow"]:
        timers.set_state(conn, timer["timer_id"], "FAILED",
                         last_error="opa denied dispatch: " + "; ".join(gate["deny_reasons"]))
        return "FAILED"

    resume = {"event": "REASSESSMENT_TIMEOUT", "fire_id": fid}
    if timer.get("timer_gap"):
        resume["timer_gap"] = True  # records that nobody was watching for a while

    # No timeout: `graph.invoke` is in-process. If the process dies mid-call the
    # row's lock runs out, `claim_retryable` picks it up, and `reconcile` sorts it out.
    try:
        with case_trace(case_id, "timer-fire", current) as span:
            result = graph.invoke(Command(resume=resume), config)
            record_outcome(span, current, result)
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
    fid = timer.get("fire_id") or fire_id(case_id, timer["kind"], timer["schedule_seq"], timer["due_at"])
    config = config_for(case_id)
    attempts = timer.get("reconcile_attempts", 0) + 1

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


def _send_reminder(conn, timer: dict[str, Any], ctx: dict[str, Any]) -> str:
    """The NOTIFY executor: OPA gate, then the notification record.
    CANCELLED / over-budget were already chosen upstream by the b-threads;
    this gate is the last line, immediately before the send.
    """
    gate = opa.evaluate({"action": "notify", "pause_active": ctx["pause_active"],
                         "notify_count": ctx["notify_count"], "notify_budget": ctx["notify_budget"]})
    if not gate["allow"]:
        timers.set_state(conn, timer["timer_id"], "FAILED",
                         last_error="opa denied notify: " + "; ".join(gate["deny_reasons"]))
        return "FAILED"

    reason = f"{timer['kind']}_{timer['schedule_seq']}"
    timers.record_notification(conn, case_id=timer["case_id"], reason=reason, channel="notification_strip",
                                recipient_class=ctx["recipient_class"])
    timers.set_state(conn, timer["timer_id"], "DELIVERED")
    return "DELIVERED"


def notify(conn, timer: dict[str, Any], *, graph) -> str:
    """Send a gate, re-filing or senior reminder. Notify-only: no case
    state change, no ack, so the b-threads never select DISPATCHING/UNKNOWN
    for one. Kept as a named entry point because callers and tests reach for
    it by name; choosing the action is `handle`'s job.
    """
    return handle(conn, timer, graph=graph)


def _inconclusive(conn, timer: dict[str, Any], fid: str, attempts: int, *,
                   recipient_class: str, reason: str, last_error: str | None = None) -> str:
    """Could not tell if the fire landed. Stay UNKNOWN, or escalate once the budget is spent."""
    if attempts >= RECONCILE_BUDGET:
        timers.set_state(conn, timer["timer_id"], "ESCALATED_TO_HUMAN", fire_id=fid,
                         reconcile_attempts=attempts)
        timers.record_escalation(conn, case_id=timer["case_id"], fire_id=fid, channel="notification_strip",
                                  recipient_class=recipient_class, reason=reason)
        return "ESCALATED_TO_HUMAN"
    fields: dict[str, Any] = {"fire_id": fid, "reconcile_attempts": attempts}
    if last_error:  # only overwrite last_error when this attempt produced one
        fields["last_error"] = last_error
    timers.set_state(conn, timer["timer_id"], "UNKNOWN", **fields)
    return "UNKNOWN"
