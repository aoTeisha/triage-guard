"""Tests for the sweeper's single pass (`run_once`): recording a heartbeat,
claiming due timers, dispatching them, and retrying or reconciling whatever
is outstanding from a previous tick. Every test passes `run_once` its own
explicit `graph` so it never touches the process-cached `app.runner.graph()`
or the real database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.budgets import TIMER_GAP_GRACE_MINUTES
from app.mock_cases import DEMO_CASES
from app.monitor import fire, sweeper, timers
from app.runner import config_for, hydrate


def test_run_once_writes_a_heartbeat(conn, graph):
    sweeper.run_once(conn, worker_id="w1", graph=graph)

    row = conn.execute("SELECT beat_at FROM sweeper_heartbeats WHERE worker_id='w1'").fetchone()
    assert row is not None


def test_run_once_claims_due_timers(conn, graph):
    timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")

    claimed = sweeper.run_once(conn, worker_id="w1", graph=graph)

    assert [t["case_id"] for t in claimed] == ["c1"]


def test_run_once_with_nothing_due_claims_nothing(conn, graph):
    claimed = sweeper.run_once(conn, worker_id="w1", graph=graph)

    assert claimed == []


def test_run_once_dispatches_a_claimed_timer(conn, graph, run):
    case = DEMO_CASES["clean"]
    state, pending, thread = run(case)
    assert pending == {"case_id": case["case_id"], "waiting_room": True}
    timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    row = conn.execute("SELECT fire_state FROM timers WHERE case_id=?", (thread,)).fetchone()
    assert row == ("DELIVERED",)
    result = hydrate(graph.get_state(config_for(thread)).values)
    assert result["reassessment_cycle"] == 1


def test_run_once_redispatches_a_failed_timer_on_a_later_tick(conn, graph):
    timer_id = timers.schedule(conn, case_id="no-such-case", kind="reassessment", cycle=0,
                                due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "FAILED", lease_until=None, worker_id=None)

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=?", (timer_id,)).fetchone()
    assert row == ("FAILED",)  # case still doesn't exist: refused again, not silently dropped


def test_run_once_flags_timer_gap_on_a_severely_overdue_reassessment(conn, graph, run):
    state, pending, thread = run(DEMO_CASES["clean"])
    band = state["acuity"] or 5
    grace = TIMER_GAP_GRACE_MINUTES.get(band, TIMER_GAP_GRACE_MINUTES[5])
    overdue_due_at = (datetime.now(timezone.utc) - timedelta(minutes=grace + 5)).isoformat()
    timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at=overdue_due_at)

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    result = hydrate(graph.get_state(config_for(thread)).values)
    assert "timer_gap" in result["flags"]


def test_run_once_does_not_flag_timer_gap_within_the_grace_window(conn, graph, run):
    state, pending, thread = run(DEMO_CASES["clean"])
    just_overdue = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at=just_overdue)

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    result = hydrate(graph.get_state(config_for(thread)).values)
    assert "timer_gap" not in result["flags"]


def test_run_once_reconciles_an_unknown_timer(conn, graph, run):
    case = DEMO_CASES["clean"]
    state, pending, thread = run(case)
    fid = fire.fire_id(thread, "reassessment", 0, "2000-01-01T00:00:00Z")
    timer_id = timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "UNKNOWN", fire_id=fid, lease_until=None, worker_id=None)
    # Nobody ever resumed the thread: reconciliation should prove it, not guess.

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=?", (timer_id,)).fetchone()
    assert row == ("FAILED",)
