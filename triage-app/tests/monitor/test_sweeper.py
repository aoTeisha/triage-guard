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

    row = conn.execute("SELECT fire_state FROM timers WHERE case_id=%s", (thread,)).fetchone()
    assert row == ("DELIVERED",)
    result = hydrate(graph.get_state(config_for(thread)).values)
    assert result["reassessment_cycle"] == 1


def test_run_once_redispatches_a_failed_timer_on_a_later_tick(conn, graph):
    timer_id = timers.schedule(conn, case_id="no-such-case", kind="reassessment", cycle=0,
                                due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "FAILED", lease_until=None, worker_id=None)

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
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

    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row == ("FAILED",)


def test_run_once_never_redispatches_an_unknown_timer_blind(conn, graph, run):
    """A retryable UNKNOWN row must go to reconcile, not dispatch — and the
    decision column has to show the b-threads made that call (I18)."""
    case = DEMO_CASES["clean"]
    state, pending, thread = run(case)
    fid = fire.fire_id(thread, "reassessment", 0, "2000-01-01T00:00:00Z")
    timer_id = timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "UNKNOWN", fire_id=fid, lease_until=None, worker_id=None)

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    row = conn.execute("SELECT fire_state, decision FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row[0] == "FAILED"
    assert row[1].startswith("RECONCILE (proposed DISPATCH")
    assert hydrate(graph.get_state(config_for(thread)).values)["reassessment_cycle"] == 0


def test_an_orphan_timer_is_escalated_to_a_technician_once(conn, graph):
    timer_id = timers.schedule(conn, case_id="no-such-case", kind="reassessment", cycle=0,
                                due_at="2000-01-01T00:00:00Z")

    sweeper.run_once(conn, worker_id="w1", graph=graph)
    sweeper.run_once(conn, worker_id="w1", graph=graph)

    # The timer itself is left alone (still FAILED, still retryable): this
    # layer reports, it never mutates.
    assert conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer_id,)).fetchone() == ("FAILED",)
    rows = conn.execute("SELECT recipient_class, reason FROM escalations WHERE case_id='no-such-case'").fetchall()
    assert rows == [("technician", "orphan_timer")]


def test_run_once_survives_a_broken_invariant_pass(conn, graph, monkeypatch):
    """A DB/store error inside the Datalog invariant pass (timers.all_rows,
    tick_invariants, or the escalation writes) must not propagate out of
    run_once — and the rest of that tick's work must already have
    completed, unaffected."""
    timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")

    def boom(*a, **k):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr(timers, "all_rows", boom)

    claimed = sweeper.run_once(conn, worker_id="w1", graph=graph)

    assert [t["case_id"] for t in claimed] == ["c1"]  # the claim loop already ran and returned normally


def test_handle_degrades_to_technician_escalation_when_graph_is_unreachable_for_an_unknown_reassessment(conn):
    """Regression: `_is_timer_gap` used to call `graph.get_state` unguarded
    for every reassessment row before `_handle` decided dispatch vs
    reconcile, so a store-unreachable fault escaped there before
    `fire.reconcile`'s own store-unreachable handler ever got a turn. An
    UNKNOWN reassessment timer must still degrade to the technician
    escalation, not raise.
    """
    timer_id = timers.schedule(conn, case_id="c9", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "UNKNOWN", fire_id="fid-c9",
                      attempts=fire.RECONCILE_BUDGET - 1, lease_until=None, worker_id=None)
    timer = {"timer_id": timer_id, "case_id": "c9", "kind": "reassessment", "cycle": 0,
             "due_at": "2000-01-01T00:00:00Z", "fire_state": "UNKNOWN",
             "fire_id": "fid-c9", "attempts": fire.RECONCILE_BUDGET - 1}

    class UnreachableGraph:
        def get_state(self, config):
            raise ConnectionError("graph unreachable")

        def get_state_history(self, config):
            raise ConnectionError("graph unreachable")

    sweeper._handle(conn, timer, UnreachableGraph())

    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row == ("ESCALATED_TO_HUMAN",)
    esc = conn.execute(
        "SELECT recipient_class, reason FROM escalations WHERE fire_id='fid-c9'"
    ).fetchone()
    assert esc == ("technician", "store_unreachable")


def test_a_waiting_case_nobody_is_watching_is_escalated(conn, graph, run):
    """I16: the queue's deadline disappeared, so somebody has to hear about it."""
    case = DEMO_CASES["clean"]
    state, pending, thread = run(case)
    timer_id = timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at="2999-01-01T00:00:00Z")
    sweeper.run_once(conn, worker_id="w1", graph=graph)
    assert conn.execute("SELECT COUNT(*) FROM escalations WHERE case_id=%s", (thread,)).fetchone() == (0,)

    timers.set_state(conn, timer_id, "CANCELLED")  # the watcher was pulled out from under a waiting patient
    sweeper.run_once(conn, worker_id="w1", graph=graph)

    rows = conn.execute("SELECT recipient_class, reason FROM escalations WHERE case_id=%s", (thread,)).fetchall()
    assert rows == [("technician", "unwatched_case")]
