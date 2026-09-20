"""Tests for the fire state machine in `app.monitor.fire`: dispatching a due
timer, handling a lost acknowledgment, de-duplicating on fire_id, and the
escalation budget. Reassessment timers only (gate reminders are covered in
`test_sweeper.py`).

Every test uses its own real graph and checkpointer (`conftest.graph`), never
the process-cached `app.runner.graph()` — `fire.dispatch`/`fire.reconcile`
take an explicit `graph` argument specifically so tests can stay isolated
from that shared singleton and from each other.
"""

from __future__ import annotations

from langgraph.types import Command

from app.mock_cases import DEMO_CASES
from app.monitor import fire, timers
from app.runner import config_for, hydrate
from app.states import State
from tests.test_gates import GAP_CASE, CHARGE


def _reach_monitoring(conn, graph, run, case=None):
    """Drive a case to the waiting-room pause and schedule its timer row."""
    case = case or DEMO_CASES["clean"]
    state, pending, thread = run(case)
    assert pending == {"case_id": case["case_id"], "waiting_room": True}
    due_at = "2000-01-01T00:00:00Z"  # already due
    timer_id = timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at=due_at)
    return {"timer_id": timer_id, "case_id": thread, "kind": "reassessment", "cycle": 0, "due_at": due_at}


def test_fire_id_is_deterministic():
    a = fire.fire_id("c1", "reassessment", 0, "2026-01-01T00:00:00Z")
    b = fire.fire_id("c1", "reassessment", 0, "2026-01-01T00:00:00Z")
    c = fire.fire_id("c1", "reassessment", 1, "2026-01-01T00:00:00Z")

    assert a == b
    assert a != c


def test_a_fire_id_less_event_is_never_treated_as_a_duplicate(conn, graph, run):
    """A nurse-initiated DETERIORATION_DETECTED event carries no `fire_id` at
    all. The de-dupe check in `awaiting_reassessment` must not treat it (or a
    second one just like it) as a repeat of some other fire_id-less audit row
    already on the case — which comparing `None == None` would incorrectly
    do if the check didn't first require a real fire_id to be present.
    """
    timer = _reach_monitoring(conn, graph, run)
    config = config_for(timer["case_id"])

    graph.invoke(Command(resume={"event": "DETERIORATION_DETECTED", "signal": "spo2 88"}), config)

    result = hydrate(graph.get_state(config).values)
    assert any(
        rec.get("explanation", "").startswith("reassessment timer fired: DETERIORATION_DETECTED")
        for rec in result["audit_log"]
    )
    assert result["reassessment_cycle"] == 1


def test_dispatch_delivers_a_pending_fire(conn, graph, run):
    timer = _reach_monitoring(conn, graph, run)

    outcome = fire.dispatch(conn, timer, graph=graph)

    assert outcome == "DELIVERED"
    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row == ("DELIVERED",)
    # The fire landed and the case moved off `monitoring` — but it now rests
    # at the reassessment re-filing pause, waiting for a nurse, not back at a
    # fresh monitoring cycle. tests/test_reassessment.py covers that pause.
    result = hydrate(graph.get_state(config_for(timer["case_id"])).values)
    assert result["control_state"] == State.REASSESSMENT_REQUIRED.value
    assert result["reassessment_cycle"] == 1


def test_dispatch_is_idempotent_on_a_case_already_past_monitoring(conn, graph, run):
    # Delivering the same fire_id twice should move the case forward only
    # once. That's a real risk here because a case with no issues loops all
    # the way back around to a fresh `monitoring` pause for its *next* cycle,
    # within the same resume call — so a naive re-dispatch could land on that
    # new pause and advance the case a second time instead of correctly
    # doing nothing.
    timer = _reach_monitoring(conn, graph, run)
    fire.dispatch(conn, timer, graph=graph)  # first delivery moves the case on

    outcome = fire.dispatch(conn, dict(timer), graph=graph)  # a redundant second attempt

    assert outcome == "DELIVERED"
    result = hydrate(graph.get_state(config_for(timer["case_id"])).values)
    assert result["reassessment_cycle"] == 1
    fid = fire.fire_id(timer["case_id"], "reassessment", 0, timer["due_at"])
    assert sum(1 for rec in result["audit_log"] if rec.get("fire_id") == fid) == 1


def test_dispatch_refuses_a_case_that_does_not_exist(conn, graph):
    timer_id = timers.schedule(conn, case_id="no-such-case", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timer = {"timer_id": timer_id, "case_id": "no-such-case", "kind": "reassessment",
             "cycle": 0, "due_at": "2000-01-01T00:00:00Z"}

    outcome = fire.dispatch(conn, timer, graph=graph)

    assert outcome == "FAILED"
    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row == ("FAILED",)


def test_reconcile_finds_a_landed_fire_in_the_audit_log(conn, graph, run):
    timer = _reach_monitoring(conn, graph, run)
    fid = fire.fire_id(timer["case_id"], "reassessment", 0, timer["due_at"])
    timers.set_state(conn, timer["timer_id"], "UNKNOWN", fire_id=fid)
    # The fire actually landed; only the ack was lost.
    graph.invoke(Command(resume={"event": "REASSESSMENT_TIMEOUT", "fire_id": fid}), config_for(timer["case_id"]))

    outcome = fire.reconcile(conn, {**timer, "fire_id": fid, "attempts": 0}, graph=graph)

    assert outcome == "DELIVERED"


def test_reconcile_fails_a_fire_that_never_applied(conn, graph, run):
    timer = _reach_monitoring(conn, graph, run)
    fid = fire.fire_id(timer["case_id"], "reassessment", 0, timer["due_at"])
    timers.set_state(conn, timer["timer_id"], "UNKNOWN", fire_id=fid)
    # Nobody ever resumed the thread — the case is still sitting at the pause.

    outcome = fire.reconcile(conn, {**timer, "fire_id": fid, "attempts": 0}, graph=graph)

    assert outcome == "FAILED"
    row = conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row == ("FAILED",)


def test_reconcile_escalates_when_the_store_is_unreachable_through_the_whole_budget(conn, graph, run, monkeypatch):
    timer = _reach_monitoring(conn, graph, run)
    fid = fire.fire_id(timer["case_id"], "reassessment", 0, timer["due_at"])

    def broken_history(*a, **k):
        raise RuntimeError("store unreachable")

    monkeypatch.setattr(graph, "get_state_history", broken_history)

    outcome = fire.reconcile(conn, {**timer, "fire_id": fid, "attempts": fire.RECONCILE_BUDGET - 1}, graph=graph)

    assert outcome == "ESCALATED_TO_HUMAN"
    row = conn.execute("SELECT recipient_class, reason FROM escalations WHERE fire_id=%s", (fid,)).fetchone()
    assert row == ("technician", "store_unreachable")


def test_notify_delivers_a_reminder_while_the_gate_is_still_open(conn, graph, run):
    state, pending, thread = run(GAP_CASE)
    assert pending is not None and pending.get("gate") is not None
    timer = {"timer_id": "t1", "case_id": thread, "kind": "gate_reminder", "cycle": 0,
             "due_at": "2000-01-01T00:00:00Z"}

    outcome = fire.notify(conn, timer, graph=graph)

    assert outcome == "DELIVERED"
    assert conn.execute(
        "SELECT recipient_class FROM notifications WHERE case_id=%s", (thread,)
    ).fetchone() == ("assigned_nurse",)


def test_notify_widens_to_any_charge_nurse_on_the_second_rung(conn, graph, run):
    state, pending, thread = run(GAP_CASE)
    timer = {"timer_id": "t2", "case_id": thread, "kind": "gate_reminder", "cycle": 1,
             "due_at": "2000-01-01T00:00:00Z"}

    fire.notify(conn, timer, graph=graph)

    assert conn.execute(
        "SELECT recipient_class FROM notifications WHERE case_id=%s", (thread,)
    ).fetchone() == ("any_charge_nurse",)


def test_notify_cancels_a_reminder_for_a_gate_thats_already_resolved(conn, graph, run):
    state, pending, thread = run(GAP_CASE)
    graph.invoke(Command(resume=CHARGE), config_for(thread))
    timer = {"timer_id": "t3", "case_id": thread, "kind": "gate_reminder", "cycle": 0,
             "due_at": "2000-01-01T00:00:00Z"}

    outcome = fire.notify(conn, timer, graph=graph)

    assert outcome == "CANCELLED"
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE case_id=%s", (thread,)).fetchone()[0] == 0


def test_notify_stops_sending_once_the_recipients_budget_is_spent(conn, graph, run, monkeypatch):
    monkeypatch.setattr(fire, "NOTIFICATION_BUDGET_PER_WINDOW", 1)
    state, pending, thread = run(GAP_CASE)
    timers.record_notification(conn, case_id="someone-else", reason="x", channel="notification_strip",
                                recipient_class="assigned_nurse")
    timer = {"timer_id": "t4", "case_id": thread, "kind": "gate_reminder", "cycle": 0,
             "due_at": "2000-01-01T00:00:00Z"}

    outcome = fire.notify(conn, timer, graph=graph)

    assert outcome == "FAILED"
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE case_id=%s", (thread,)).fetchone()[0] == 0


def test_dispatch_flags_timer_gap_on_a_severely_overdue_fire(conn, graph, run):
    # A case whose timer was overdue by more than the grace window
    # carries `timer_gap` — an honest record that a window existed where nobody
    # was watching, not silently resumed as if nothing happened.
    timer = _reach_monitoring(conn, graph, run)

    fire.dispatch(conn, {**timer, "timer_gap": True}, graph=graph)

    result = hydrate(graph.get_state(config_for(timer["case_id"])).values)
    assert "timer_gap" in result["flags"]


def test_dispatch_without_timer_gap_does_not_flag_it(conn, graph, run):
    timer = _reach_monitoring(conn, graph, run)

    fire.dispatch(conn, timer, graph=graph)

    result = hydrate(graph.get_state(config_for(timer["case_id"])).values)
    assert "timer_gap" not in result["flags"]


def test_reconcile_escalation_holds_even_when_the_graph_is_totally_unreachable(conn):
    """Wait-liveness through total control-plane failure — the escalation must be
    raised even though this reconcile call never touches a working graph at all.
    """
    timer_id = timers.schedule(conn, case_id="c9", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timer = {"timer_id": timer_id, "case_id": "c9", "kind": "reassessment", "cycle": 0,
             "due_at": "2000-01-01T00:00:00Z", "fire_id": "fid-c9", "attempts": fire.RECONCILE_BUDGET - 1}

    class UnreachableGraph:
        def get_state_history(self, config):
            raise ConnectionError("graph unreachable")

        def get_state(self, config):
            raise ConnectionError("graph unreachable")

    outcome = fire.reconcile(conn, timer, graph=UnreachableGraph())

    assert outcome == "ESCALATED_TO_HUMAN"
    assert conn.execute("SELECT COUNT(*) FROM escalations WHERE fire_id='fid-c9'").fetchone()[0] == 1


# ---- the symbolic layers on the fire path --------------------------------------
from app.symbolic import prolog


def _gate_timer(conn, graph, run, cycle=0):
    """A case parked at the human gate, plus a gate reminder row for it."""
    state, pending, thread = run(GAP_CASE)
    assert pending is not None and pending.get("gate") is not None
    timer_id = timers.schedule(conn, case_id=thread, kind="gate_reminder", cycle=cycle,
                                due_at="2000-01-01T00:00:00Z")
    return {"timer_id": timer_id, "case_id": thread, "kind": "gate_reminder", "cycle": cycle,
            "due_at": "2000-01-01T00:00:00Z"}


def test_handle_records_the_decision_and_dispatches(conn, graph, run):
    timer = _reach_monitoring(conn, graph, run)

    assert fire.handle(conn, timer, graph=graph) == "DELIVERED"

    row = conn.execute("SELECT decision FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row == ("DISPATCH",)


def test_handle_reconciles_an_unknown_timer_instead_of_redispatching(conn, graph, run):
    """I16 has teeth only if a lost ack is proved, not guessed."""
    timer = _reach_monitoring(conn, graph, run)
    fid = fire.fire_id(timer["case_id"], "reassessment", 0, timer["due_at"])
    timers.set_state(conn, timer["timer_id"], "UNKNOWN", fire_id=fid)

    outcome = fire.handle(conn, {**timer, "fire_state": "UNKNOWN", "fire_id": fid, "attempts": 0}, graph=graph)

    # reconcile proves the fire never applied: FAILED, retryable — and the case was NOT resumed
    assert outcome == "FAILED"
    assert hydrate(graph.get_state(config_for(timer["case_id"])).values)["reassessment_cycle"] == 0
    decision = conn.execute("SELECT decision FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()[0]
    assert decision == "RECONCILE (proposed DISPATCH: dispatch: blind_redispatch_from_unknown)"


def test_handle_refuses_when_prolog_and_bppy_disagree(conn, graph, run, monkeypatch):
    """Two layers derive the action from the same facts. If they ever differ,
    nothing executes — that's the point of enforcing a rule twice."""
    timer = _reach_monitoring(conn, graph, run)
    monkeypatch.setattr(prolog, "timer_action", lambda ctx: ("cancel", ""))

    assert fire.handle(conn, timer, graph=graph) == "FAILED"

    row = conn.execute("SELECT fire_state, last_error FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row[0] == "FAILED" and row[1] == "layer_disagreement: bppy=DISPATCH prolog=cancel"
    assert hydrate(graph.get_state(config_for(timer["case_id"])).values)["reassessment_cycle"] == 0


def test_handle_refuses_when_prolog_is_unavailable(conn, graph, run, monkeypatch):
    timer = _reach_monitoring(conn, graph, run)
    monkeypatch.setattr(prolog, "timer_action", lambda ctx: ("engine_unavailable", "prolog: boom"))

    assert fire.handle(conn, timer, graph=graph) == "FAILED"

    row = conn.execute("SELECT last_error FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row == ("engine_unavailable:prolog (prolog: boom)",)


def test_dispatch_is_denied_when_opa_is_unavailable(conn, graph, run, monkeypatch):
    """Fail-closed: no engine, no resume. The case stays exactly where it was."""
    timer = _reach_monitoring(conn, graph, run)
    monkeypatch.setenv("OPA_BIN", "/nonexistent/opa")

    assert fire.dispatch(conn, timer, graph=graph) == "FAILED"

    row = conn.execute("SELECT fire_state, last_error FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row[0] == "FAILED" and row[1].startswith("opa denied dispatch: engine_unavailable:opa")
    result = hydrate(graph.get_state(config_for(timer["case_id"])).values)
    assert result["control_state"] == State.MONITORING.value
    assert result["reassessment_cycle"] == 0


def test_notify_is_denied_when_opa_is_unavailable(conn, graph, run, monkeypatch):
    timer = _gate_timer(conn, graph, run)
    monkeypatch.setenv("OPA_BIN", "/nonexistent/opa")

    assert fire.notify(conn, timer, graph=graph) == "FAILED"

    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone() == (0,)


def test_handle_treats_an_unreachable_graph_as_store_unreachable(conn):
    class UnreachableGraph:
        def get_state(self, config):
            raise ConnectionError("graph unreachable")

    timers.schedule(conn, case_id="c9", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timer = {"timer_id": "c9:reassessment:0", "case_id": "c9", "kind": "reassessment", "cycle": 0,
             "due_at": "2000-01-01T00:00:00Z"}

    assert fire.handle(conn, timer, graph=UnreachableGraph()) == "UNKNOWN"

    row = conn.execute("SELECT fire_state, attempts, last_error FROM timers WHERE timer_id=%s",
                       (timer["timer_id"],)).fetchone()
    assert row == ("UNKNOWN", 1, "graph unreachable")
