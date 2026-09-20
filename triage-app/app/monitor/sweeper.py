"""The sweeper: `uv run sweeper`, a background process that polls the timer store.

Each tick: heartbeat, claim newly due timers and fire them, then claim leftovers
from earlier ticks (FAILED -> redispatch, UNKNOWN -> reconcile).
`graph=None` only in `main()`; tests always pass their own.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import psycopg

from app.budgets import TIMER_GAP_GRACE_MINUTES
from app.monitor import fire, timers
from app.runner import config_for
from app.runner import graph as real_graph

SWEEP_INTERVAL_SECONDS = 5
LEASE_SECONDS = 30


def _is_timer_gap(graph, timer: dict) -> bool:
    """Overdue past the acuity-scaled grace window? Reassessment timers only."""
    if timer["kind"] != "reassessment":
        return False
    overdue_minutes = (datetime.now(timezone.utc) - timer["due_at"]).total_seconds() / 60
    band = graph.get_state(config_for(timer["case_id"])).values.get("acuity") or 5
    return overdue_minutes > TIMER_GAP_GRACE_MINUTES.get(band, TIMER_GAP_GRACE_MINUTES[5])


def _dispatch(conn: psycopg.Connection, timer: dict, graph) -> None:
    fire.dispatch(conn, {**timer, "timer_gap": _is_timer_gap(graph, timer)}, graph=graph)


def _handle_due(conn: psycopg.Connection, timer: dict, graph) -> None:
    if timer["kind"] == "reassessment":
        _dispatch(conn, timer, graph)
    else:
        fire.notify(conn, timer, graph=graph)  # gate_reminder, safety_park: notify-only


def _handle_retry(conn: psycopg.Connection, timer: dict, graph) -> None:
    if timer["kind"] != "reassessment":
        fire.notify(conn, timer, graph=graph)  # no ack to reconcile — just retry
    elif timer["fire_state"] == "FAILED":
        _dispatch(conn, timer, graph)
    else:
        fire.reconcile(conn, timer, graph=graph)


def run_once(conn: psycopg.Connection, *, worker_id: str, graph=None) -> list[dict]:
    """One tick. Returns the newly claimed (`DUE`) rows."""
    g = graph or real_graph()
    timers.heartbeat(conn, worker_id=worker_id)

    claimed = timers.claim_due(conn, worker_id=worker_id, lease_seconds=LEASE_SECONDS)
    for timer in claimed:
        _handle_due(conn, timer, g)

    for timer in timers.claim_retryable(conn, worker_id=worker_id, lease_seconds=LEASE_SECONDS):
        _handle_retry(conn, timer, g)

    return claimed


def main() -> None:
    worker_id = os.environ.get("SWEEPER_WORKER_ID", f"sweeper-{os.getpid()}")
    # Reuse `timers.connection()`: it owns the connection settings (autocommit).
    # A second `psycopg.connect` here once drifted and left claims uncommitted.
    conn = timers.connection()
    while True:
        run_once(conn, worker_id=worker_id)
        time.sleep(SWEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
