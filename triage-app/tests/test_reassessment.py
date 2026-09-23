"""The reassessment re-filing pause: a fired timer (or a reported
deterioration) must not silently replay the case's stale `raw_payload` — it
has to wait for a nurse to submit fresh observations before re-entering
intake (docs/SPECIFICATION.md, `reassessment_required` row, REASSESSMENT_DUE -> FRONT_DOOR_RERUN).
"""

from __future__ import annotations

from langgraph.types import Command

from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.states import State

REFILE = {
    "nurse_proposed_acuity": 1,
    "chief_complaint": "worsening chest pain",
    "vitals": {"hr": 140, "bp": "90/60", "spo2": 88, "temp_c": 38.2},
}


def _fire_the_timer(graph, thread: str):
    """Deliver a reassessment timeout the way `fire.dispatch` does."""
    graph.invoke(
        Command(resume={"event": "REASSESSMENT_TIMEOUT", "fire_id": "f1"}),
        config_for(thread),
    )


def test_reassessment_timeout_pauses_for_a_nurse_refile_instead_of_replaying(
    graph, run
):
    case = DEMO_CASES["clean"]
    _, pending, thread = run(case)
    assert pending == {"case_id": case["case_id"], "waiting_room": True}

    _fire_the_timer(graph, thread)

    snapshot = graph.get_state(config_for(thread))
    result = hydrate(snapshot.values)
    assert result["control_state"] == State.REASSESSMENT_REQUIRED.value
    assert result["clinical_status"] == "reassessment_required"
    assert snapshot.next, "case must still be paused, not fallen through to parsing"
    # The stale payload must NOT have been re-parsed: acuity is untouched.
    assert result["nurse_proposed_acuity"] == 3


def test_nurse_refile_with_changed_acuity_moves_the_case_and_updates_its_order_key(
    graph, run
):
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _fire_the_timer(graph, thread)

    result = hydrate(graph.invoke(Command(resume=REFILE), config_for(thread)))

    # gap = |1 - 2| (the mock classifier always proposes 2) = 1, which
    # resolve_acuity's ACUITY_GAP_MINOR band settles to the nurse's number. So acuity really
    # did change because the nurse re-filed, not because the clock ticked.
    assert result["acuity"] == 1
    assert result["order_key"][0] == 1  # acuity is the first part of the key
    assert any(
        rec.get("explanation") == "nurse re-filed with fresh observations"
        for rec in result["audit_log"]
    )


def test_nurse_refile_preserves_routing_fields(graph, run):
    """Only the clinical fields a nurse can change get overwritten. The rest of
    the payload is carried over — see the merge constraint in plan.md, this is
    what keeps a re-file parsing cleanly instead of ending the run.
    """
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _fire_the_timer(graph, thread)

    graph.invoke(Command(resume=REFILE), config_for(thread))

    result = hydrate(graph.get_state(config_for(thread)).values)
    assert result["raw_payload"]["stable_patient_id"] == case["stable_patient_id"]
    assert result["raw_payload"]["channel"] == case["channel"]
    assert result["raw_payload"]["free_text"] == case["free_text"]


def test_entering_the_refile_pause_schedules_a_reminder(conn, graph, run):
    """The pause must not be the one place a waiting patient has no timer."""
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)

    _fire_the_timer(graph, thread)

    row = conn.execute(
        "SELECT kind, fire_state FROM timers WHERE case_id=%s AND kind='reassessment_reminder'",
        (case["case_id"],),
    ).fetchone()
    assert row == ("reassessment_reminder", "SCHEDULED")


def test_the_reminder_notifies_a_charge_nurse_while_the_case_still_waits(
    conn, graph, run
):
    from app.monitor import fire

    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _fire_the_timer(graph, thread)
    # schedule_seq 0 -> _GATE_RUNG_RECIPIENTS[0] is "assigned_nurse", so only the
    # kind-aware branch in fire.notify (not a gate-only fallback) can produce
    # "any_charge_nurse" here; schedule_seq 1 would not discriminate the bug.
    timer = {
        "timer_id": "r1",
        "case_id": thread,
        "kind": "reassessment_reminder",
        "schedule_seq": 0,
        "due_at": "2000-01-01T00:00:00Z",
    }

    outcome = fire.notify(conn, timer, graph=graph)

    assert outcome == "DELIVERED"
    assert conn.execute(
        "SELECT recipient_class FROM notifications WHERE case_id=%s", (thread,)
    ).fetchone() == ("any_charge_nurse",)


def test_the_reminder_is_cancelled_once_the_nurse_has_refiled(conn, graph, run):
    from app.monitor import fire

    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _fire_the_timer(graph, thread)
    graph.invoke(Command(resume=REFILE), config_for(thread))
    timer = {
        "timer_id": "r2",
        "case_id": thread,
        "kind": "reassessment_reminder",
        "schedule_seq": 1,
        "due_at": "2000-01-01T00:00:00Z",
    }

    outcome = fire.notify(conn, timer, graph=graph)

    assert outcome == "CANCELLED"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE case_id=%s", (thread,)
        ).fetchone()[0]
        == 0
    )


def test_a_queued_case_with_no_acuity_is_reassessed_now_not_last(monkeypatch):
    """I16: a missing acuity fails safe (0 minutes), not as ESI 5 (120)."""
    from app.graph import TriageState
    from app.graph.nodes import terminal

    asked = []
    real_due_in = terminal.timers.due_in
    monkeypatch.setattr(terminal.timers, "due_in",
                        lambda minutes: asked.append(minutes) or real_due_in(minutes))

    terminal.monitoring(TriageState(case_id="c-no-acuity", acuity=None))

    assert asked == [0]


def test_a_refile_starts_a_new_triage_with_no_old_approval(graph, run):
    """I5: approval counts only for the current triage. The first triage
    approved the case; a re-file that lands at the gate must not inherit it.
    """
    case = DEMO_CASES["clean"]
    _, _, thread = run(case)
    _fire_the_timer(graph, thread)

    refile_to_gate = {**REFILE, "nurse_proposed_acuity": 4}  # gap 2 vs the mock's 2
    graph.invoke(Command(resume=refile_to_gate), config_for(thread))

    snapshot = graph.get_state(config_for(thread))
    result = hydrate(snapshot.values)
    assert snapshot.next == (State.AWAITING_HUMAN_APPROVAL.value,)  # paused at the gate
    assert result["approved"] is False
    assert result["safety_passed"] is False
    assert result["acuity"] is None  # the old triage's acuity must not skip the gate
    assert result["order_key"][0] == 4  # queues by the nurse's new acuity (review fix 2)


def test_a_second_gate_visit_gets_its_own_reminders(conn, graph, run):
    """Review fix 3 (I15): reminder ids are unique per gate visit, so a case
    that reaches the gate again after a re-file is reminded again.
    """
    from tests.test_gates import CHARGE, GAP_CASE

    _, _, thread = run(GAP_CASE)
    graph.invoke(Command(resume=CHARGE), config_for(thread))   # resolved -> queued
    _fire_the_timer(graph, thread)
    graph.invoke(Command(resume={**REFILE, "nurse_proposed_acuity": 4}), config_for(thread))

    count = conn.execute(
        "SELECT COUNT(*) FROM timers WHERE case_id=%s AND kind='gate_reminder'",
        (GAP_CASE["case_id"],),
    ).fetchone()[0]
    assert count == 4   # two rungs per visit, two visits
