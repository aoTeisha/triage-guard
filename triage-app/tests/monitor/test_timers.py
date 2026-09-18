"""Tests for the timer store: scheduling and claiming timers, fire-state
transitions, and escalation records.
"""

from __future__ import annotations

from app.monitor import timers


def test_schedule_inserts_a_scheduled_row(conn):
    timer_id = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2026-01-01T00:00:00Z")

    row = conn.execute("SELECT case_id, kind, cycle, due_at, fire_state FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row == ("c1", "reassessment", 0, "2026-01-01T00:00:00Z", "SCHEDULED")


def test_claim_due_claims_a_past_due_timer(conn):
    timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")

    claimed = timers.claim_due(conn, worker_id="w1", lease_seconds=30)

    assert len(claimed) == 1
    assert claimed[0]["case_id"] == "c1"
    row = conn.execute("SELECT fire_state, worker_id FROM timers WHERE case_id='c1'").fetchone()
    assert row == ("DUE", "w1")


def test_claim_due_ignores_future_timers(conn):
    timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2999-01-01T00:00:00Z")

    claimed = timers.claim_due(conn, worker_id="w1", lease_seconds=30)

    assert claimed == []


def test_claim_due_does_not_double_claim_an_active_lease(conn):
    timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.claim_due(conn, worker_id="w1", lease_seconds=9999)

    claimed_by_second_worker = timers.claim_due(conn, worker_id="w2", lease_seconds=30)

    assert claimed_by_second_worker == []


def test_claim_due_claims_a_scheduled_row_with_an_expired_stale_lease(conn):
    # A SCHEDULED row should never carry a lease in normal flow, but the claim
    # query's own lease check must still let a stale/expired one through.
    timer_id = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    conn.execute("UPDATE timers SET lease_until='2000-01-01T00:00:00Z', worker_id='ghost' WHERE timer_id=%s", (timer_id,))
    conn.commit()

    claimed = timers.claim_due(conn, worker_id="w2", lease_seconds=30)

    assert len(claimed) == 1
    row = conn.execute("SELECT worker_id FROM timers WHERE case_id='c1'").fetchone()
    assert row == ("w2",)


def test_heartbeat_upserts_the_workers_row(conn):
    timers.heartbeat(conn, worker_id="w1")
    first = conn.execute("SELECT beat_at FROM sweeper_heartbeats WHERE worker_id='w1'").fetchone()[0]

    timers.heartbeat(conn, worker_id="w1")
    second = conn.execute("SELECT beat_at FROM sweeper_heartbeats WHERE worker_id='w1'").fetchone()[0]

    assert first is not None and second is not None
    assert conn.execute("SELECT COUNT(*) FROM sweeper_heartbeats").fetchone()[0] == 1


def test_schedule_is_idempotent_on_the_same_case_kind_cycle(conn):
    # The graph's `monitoring` node calls `schedule` every time its pause
    # actually resumes, which can happen more than once for the same cycle
    # on a replay — so a second call must be a no-op, not a duplicate row or
    # a changed due_at.
    first = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2026-01-01T00:00:00Z")
    second = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2099-01-01T00:00:00Z")

    assert first == second
    rows = conn.execute("SELECT due_at FROM timers WHERE timer_id=%s", (first,)).fetchall()
    assert rows == [("2026-01-01T00:00:00Z",)]


def test_set_state_updates_fire_state_and_fields(conn):
    timer_id = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")

    timers.set_state(conn, timer_id, "DISPATCHING", fire_id="fid-1", attempts=1)

    row = conn.execute("SELECT fire_state, fire_id, attempts FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row == ("DISPATCHING", "fid-1", 1)


def test_claim_retryable_claims_an_unleased_failed_row(conn):
    timer_id = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "FAILED", lease_until=None, worker_id=None)

    claimed = timers.claim_retryable(conn, worker_id="w2", lease_seconds=30)

    assert [t["timer_id"] for t in claimed] == [timer_id]
    row = conn.execute("SELECT fire_state, worker_id FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row == ("FAILED", "w2")


def test_claim_retryable_claims_a_firing_row_with_an_expired_lease(conn):
    # A lease that expires while a row is still DISPATCHING means the worker
    # holding it likely crashed mid-dispatch — the same ambiguous situation
    # as a lost acknowledgment, so it's picked up by the same claim query and
    # routed to reconciliation, not blindly re-dispatched.
    timer_id = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "DISPATCHING", lease_until="2000-01-01T00:00:00Z", worker_id="dead")

    claimed = timers.claim_retryable(conn, worker_id="w2", lease_seconds=30)

    assert [t["timer_id"] for t in claimed] == [timer_id]
    row = conn.execute("SELECT fire_state, worker_id FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row == ("DISPATCHING", "w2")


def test_claim_retryable_ignores_a_row_under_active_lease(conn):
    timer_id = timers.schedule(conn, case_id="c1", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "UNKNOWN", lease_until="2999-01-01T00:00:00Z", worker_id="w1")

    claimed = timers.claim_retryable(conn, worker_id="w2", lease_seconds=30)

    assert claimed == []


def test_record_notification_persists_and_dedupes_by_case_and_reason(conn):
    # One notification per case+reason: a repeat call for the same pair
    # is a no-op, not a duplicate row.
    first = timers.record_notification(conn, case_id="c1", reason="gate_reminder_20a",
                                        channel="notification_strip", recipient_class="assigned_nurse")
    second = timers.record_notification(conn, case_id="c1", reason="gate_reminder_20a",
                                         channel="notification_strip", recipient_class="assigned_nurse")

    assert first is True
    assert second is False
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE case_id='c1'").fetchone()[0] == 1


def test_notification_count_in_window_counts_recent_sends(conn):
    timers.record_notification(conn, case_id="c1", reason="r1", channel="strip", recipient_class="charge_nurse")
    timers.record_notification(conn, case_id="c2", reason="r2", channel="strip", recipient_class="charge_nurse")
    timers.record_notification(conn, case_id="c3", reason="r3", channel="strip", recipient_class="technician")

    assert timers.notification_count_in_window(conn, recipient_class="charge_nurse", window_minutes=60) == 2
    assert timers.notification_count_in_window(conn, recipient_class="technician", window_minutes=60) == 1
    assert timers.notification_count_in_window(conn, recipient_class="charge_nurse", window_minutes=0) == 0


def test_heartbeat_status_degraded_when_no_worker_has_ever_beaten(conn):
    # The silent outage — nobody calls the monitor, so absence of a
    # heartbeat row at all (never started, or dead before its first tick) must
    # read as degraded, not as "nothing to report."
    status = timers.heartbeat_status(conn, stale_after_seconds=15)

    assert status == {"workers": [], "degraded": True}


def test_heartbeat_status_healthy_with_a_recent_beat(conn):
    timers.heartbeat(conn, worker_id="w1")

    status = timers.heartbeat_status(conn, stale_after_seconds=15)

    assert status["degraded"] is False
    assert status["workers"][0]["worker_id"] == "w1"
    assert status["workers"][0]["stale"] is False


def test_heartbeat_status_degraded_when_the_only_worker_is_stale(conn):
    timers.heartbeat(conn, worker_id="w1")
    conn.execute("UPDATE sweeper_heartbeats SET beat_at='2000-01-01T00:00:00Z' WHERE worker_id='w1'")
    conn.commit()

    status = timers.heartbeat_status(conn, stale_after_seconds=15)

    assert status["degraded"] is True
    assert status["workers"][0]["stale"] is True


def test_record_escalation_persists_a_row(conn):
    timers.record_escalation(conn, case_id="c1", fire_id="fid-1", channel="notification_strip",
                              recipient_class="charge_nurse", reason="budget_spent")

    row = conn.execute(
        "SELECT case_id, fire_id, channel, recipient_class, reason FROM escalations WHERE fire_id='fid-1'"
    ).fetchone()
    assert row == ("c1", "fid-1", "notification_strip", "charge_nurse", "budget_spent")
