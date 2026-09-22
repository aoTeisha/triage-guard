"""The sweeper: `uv run sweeper`, a background process that polls the timer store.

Each tick: heartbeat, claim newly due timers, then leftovers from earlier ticks,
and hand every one to `fire.handle`, where the symbolic layers decide what it
gets. Then one Datalog pass over the whole store, checking for missed
deadlines and orphaned timers.
`graph=None` only in `main()`; tests always pass their own.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import psycopg

from app.budgets import TIMER_GAP_GRACE_MINUTES
from app.monitor import fire, timers
from app.runner import config_for
from app.runner import graph as real_graph
from app.symbolic import datalog

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 5
LOCK_SECONDS = 30


def _is_timer_gap(graph, timer: dict) -> bool:
    """Overdue past the acuity-scaled grace window? Reassessment timers only."""
    if timer["kind"] != "reassessment":
        return False
    overdue_minutes = (datetime.now(timezone.utc) - timer["due_at"]).total_seconds() / 60
    band = graph.get_state(config_for(timer["case_id"])).values.get("acuity") or 5
    return overdue_minutes > TIMER_GAP_GRACE_MINUTES.get(band, TIMER_GAP_GRACE_MINUTES[5])


def _handle(conn: psycopg.Connection, timer: dict, graph) -> None:
    """Every claimed timer — newly due or left over — goes through the same
    decision in `fire.handle`; the sweeper no longer decides anything itself.
    `timer_gap` is computed here because it needs the clock and the acuity,
    which are the sweeper's business, not the rules'.
    """
    if timer["kind"] == "reassessment":
        try:
            timer = {**timer, "timer_gap": _is_timer_gap(graph, timer)}
        except Exception:  # noqa: BLE001 — graph/store unreachable: let fire.handle's own
            # graph read surface this as the documented store_unreachable path,
            # instead of escaping here before fire.reconcile ever gets a turn.
            pass
    fire.handle(conn, timer, graph=graph)


def _escalate_once(conn: psycopg.Connection, *, case_id: str, reason: str) -> None:
    if timers.escalation_exists(conn, case_id=case_id, reason=reason):
        return
    timers.record_escalation(conn, case_id=case_id, fire_id=f"invariant:{reason}", channel="notification_strip",
                              recipient_class="technician", reason=reason)


def _check_invariants(conn: psycopg.Connection, graph) -> None:
    """The deadline pass, after the tick's own work so it sees this tick's
    outcomes: a waiting case nobody is watching, or a live timer for a case
    that is gone. Findings go to a technician, once per (case, reason). It
    never touches timer state — a timer the sweeper keeps refusing is
    evidence, not a mess to tidy away.
    """
    # ponytail: one graph.get_state per distinct live case per tick. Fine for
    # a ward's worth of cases; cache by checkpoint id if it ever isn't.
    try:
        timer_rows = timers.all_rows(conn)
        case_rows = []
        for case_id in sorted({row["case_id"] for row in timer_rows}):
            values = graph.get_state(config_for(case_id)).values
            case_rows.append({"case_id": case_id, "control_state": values.get("control_state"),
                              "clinical_status": values.get("clinical_status")})
        findings = datalog.tick_invariants(timer_rows, case_rows)
        for case_id in findings["unwatched"]:
            _escalate_once(conn, case_id=case_id, reason="unwatched_case")
        for case_id, _timer_id in findings["orphan"]:
            _escalate_once(conn, case_id=case_id, reason="orphan_timer")
    except Exception:  # noqa: BLE001 — store/DB error anywhere in this pass (timer read,
        # graph read, or escalation write): skip this tick's invariant check
        # and keep going, same as the per-timer path already does for
        # store_unreachable.
        logger.warning("sweeper: invariant pass failed this tick", exc_info=True)
        return


def run_once(conn: psycopg.Connection, *, worker_id: str, graph=None) -> list[dict]:
    """One tick. Returns the newly claimed (`DUE`) rows."""
    g = graph or real_graph()
    timers.heartbeat(conn, worker_id=worker_id)

    claimed = timers.claim_due(conn, worker_id=worker_id, lock_seconds=LOCK_SECONDS)
    for timer in claimed:
        _handle(conn, timer, g)

    for timer in timers.claim_retryable(conn, worker_id=worker_id, lock_seconds=LOCK_SECONDS):
        _handle(conn, timer, g)

    _check_invariants(conn, g)

    return claimed


def main() -> None:
    worker_id = os.environ.get("SWEEPER_WORKER_ID", f"sweeper-{os.getpid()}")
    # Reuse `timers.connection()`: it owns the connection settings (autocommit).
    # A second `psycopg.connect` here once drifted and left claims uncommitted.
    conn = timers.connection()
    while True:
        try:
            run_once(conn, worker_id=worker_id)
        except Exception:  # noqa: BLE001 — last line of defense: the sweeper never crashes,
            # even on a fault `run_once` itself failed to isolate.
            logger.exception("sweeper: run_once failed this tick")
        time.sleep(SWEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
