"""The human gates: a real pause, a real checkpoint, a real resume.

These are the tests that would have been impossible under the old Flow, where
`@human_feedback` would have handed the charge nurse's free text to gpt-4o-mini
for interpretation. Here the decision is a structured payload and no model is
involved.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from app.graph import build_graph
from app.labels import Transition
from app.runner import hydrate
from app.states import State
from tests.conftest import _drop_db, _throwaway_db, transitions

# nurse says 5, the classifier proposes 2 (mock fixture) -> gap 3 -> ACUITY_GAP_MAJOR.
GAP_CASE = {
    "case_id": "case-gap",
    "channel": "website",
    "national_id": "300000009",
    "nurse_proposed_acuity": 5,
    "chief_complaint": "chest_pain",
    "vitals": {"hr": 120, "bp": "160/100", "spo2": 94, "temp_c": 37.0},
    "free_text": "Sudden crushing chest pain.",
}

CHARGE = {"decision": "use_system_acuity", "resolver_role": "charge_nurse"}


def test_a_major_gap_pauses_the_run(run):
    """ACUITY_GAP_MAJOR: gap >= 2 is not resolved by the machine."""
    state, pending, _ = run(GAP_CASE)

    assert pending is not None
    assert pending["gate"] == "discrepancy"
    assert pending["required_role"] == "charge_nurse"
    assert pending["nurse_proposed_acuity"] == 5
    assert pending["system_proposed_acuity"] == 2
    assert Transition.ACUITY_GAP_MAJOR in transitions(state)


def test_a_paused_case_has_no_settled_acuity(run):
    """The machine must not pick a value while the question is with a human."""
    state, pending, _ = run(GAP_CASE)

    assert pending is not None
    assert state["acuity"] is None
    assert state["approved"] is False


def test_resuming_applies_the_humans_choice_and_marks_it_confirmed(graph, run):
    _, pending, thread = run(GAP_CASE)
    assert pending is not None

    resumed = hydrate(graph.invoke(Command(resume=CHARGE), {"configurable": {"thread_id": thread}}))

    assert resumed["acuity"] == 2
    assert resumed["acuity_source"] == "human_confirmed"
    assert Transition.GATE_ACUITY_RESOLVED in transitions(resumed)


def test_a_resolved_gate_re_runs_safety_before_the_queue(graph, run):
    """No approval bypass: the gate returns to safety_validating, not monitoring."""
    _, _, thread = run(GAP_CASE)
    resumed = hydrate(graph.invoke(Command(resume=CHARGE), {"configurable": {"thread_id": thread}}))

    trail = transitions(resumed)
    assert trail.index(Transition.GATE_ACUITY_RESOLVED) < trail.index(Transition.SAFETY_PASSED)
    assert resumed["control_state"] == State.MONITORING.value
    assert resumed["safety_passed"] is True


def test_the_pause_survives_a_rebuilt_graph():
    """The real test of durability: throw the graph object away between the pause
    and the resume, as a restarted process would.
    """
    dsn = _throwaway_db()
    cfg = {"configurable": {"thread_id": "case-gap"}}
    try:
        with PostgresSaver.from_conn_string(dsn) as saver_a:
            saver_a.setup()
            first = build_graph(checkpointer=saver_a)
            paused = first.invoke(
                {"case_id": GAP_CASE["case_id"], "raw_payload": dict(GAP_CASE),
                 "nurse_proposed_acuity": 5},
                cfg,
            )
            assert paused.get("__interrupt__")
        del first

        # A different graph object, a different connection — only the checkpoint links them.
        with PostgresSaver.from_conn_string(dsn) as saver_b:
            second = build_graph(checkpointer=saver_b)
            resumed = hydrate(second.invoke(Command(resume=CHARGE), cfg))
    finally:
        _drop_db(dsn)

    assert resumed["acuity"] == 2
    assert resumed["control_state"] == State.MONITORING.value
    # The trail spans both processes.
    assert (Transition.ACUITY_GAP_MAJOR in transitions(resumed)
            and Transition.GATE_ACUITY_RESOLVED in transitions(resumed))


def test_nurse_choice_is_honoured_when_that_is_what_the_charge_nurse_picks(graph, run):
    """The clinician is the final authority at the gate."""
    _, _, thread = run(GAP_CASE)
    resumed = hydrate(graph.invoke(
        Command(resume={"decision": "use_nurse_acuity", "resolver_role": "charge_nurse"}),
        {"configurable": {"thread_id": thread}},
    ))

    assert resumed["acuity"] == 5
    assert resumed["acuity_source"] == "human_confirmed"


# ---- I8: an exhausted correction loop goes to a senior, not END -------------


def _failing_safety(monkeypatch):
    from app.actors import safety
    from app.schemas import SafetyVerdict

    monkeypatch.setattr(safety, "validate",
                        lambda case: SafetyVerdict(verdict="fail", reasons=["unsafe"]))


def _answer(graph, thread, decision, role, corrections=None):
    cfg = {"configurable": {"thread_id": thread}}
    answer = {"decision": decision, "resolver_role": role}
    if corrections is not None:
        answer["corrections"] = corrections
    graph.invoke(Command(resume=answer), cfg)
    return graph.get_state(cfg)


def test_an_exhausted_correction_loop_waits_for_a_shift_lead(graph, run, monkeypatch):
    from app.mock_cases import DEMO_CASES

    _failing_safety(monkeypatch)
    _, pending, thread = run(DEMO_CASES["clean"])
    assert pending["gate"] == "safety_fail"

    for i in range(6):   # alternate 1 and 2 so every round is a real change (I7)
        snap = _answer(graph, thread, "corrected", "charge_nurse", {"acuity": 1 + i % 2})
        if snap.values.get("senior_required"):
            break

    assert snap.values["senior_required"] is True
    assert snap.next == (State.AWAITING_HUMAN_APPROVAL.value,)  # still open, not ended

    refused = _answer(graph, thread, "corrected", "charge_nurse", {"acuity": 4})
    assert Transition.BLK in transitions(hydrate(refused.values))
    assert refused.next == (State.AWAITING_HUMAN_APPROVAL.value,)

    monkeypatch.undo()  # the shift lead's correction now passes safety
    done = _answer(graph, thread, "corrected", "shift_lead", {"acuity": 4})
    assert done.next == ("awaiting_reassessment",)  # queued


def test_escalate_further_hands_the_case_to_a_shift_lead_at_once(graph, run, monkeypatch, conn):
    from app.mock_cases import DEMO_CASES

    _failing_safety(monkeypatch)
    _, _, thread = run(DEMO_CASES["clean"])

    snap = _answer(graph, thread, "escalate_further", "charge_nurse")

    assert snap.values["senior_required"] is True
    assert hydrate(snap.values)["correction_rounds"] == 0
    assert Transition.SENIOR_ESCALATION in transitions(hydrate(snap.values))  # review fix 5
    assert conn.execute(
        "SELECT COUNT(*) FROM timers WHERE case_id=%s AND kind='senior_reminder'",
        (DEMO_CASES["clean"]["case_id"],),
    ).fetchone()[0] == 1


def test_the_senior_reminder_goes_to_a_shift_lead(graph, run, monkeypatch, conn):
    """I15: a case waiting for a senior still escalates, to the right people."""
    from app.mock_cases import DEMO_CASES
    from app.monitor import fire

    _failing_safety(monkeypatch)
    _, _, thread = run(DEMO_CASES["clean"])
    _answer(graph, thread, "escalate_further", "charge_nurse")

    timer = {"timer_id": "s1", "case_id": thread, "kind": "senior_reminder",
             "schedule_seq": 0, "due_at": "2000-01-01T00:00:00Z"}
    assert fire.notify(conn, timer, graph=graph) == "DELIVERED"
    assert conn.execute(
        "SELECT recipient_class FROM notifications WHERE case_id=%s", (thread,)
    ).fetchone() == ("any_shift_lead",)


# ---- I7: a correction must change something ------------------------------------


@pytest.mark.parametrize("corrections", [None, {}, {"acuity": 3}, {"acuity": 9}, {"acuity": True}],
                         ids=["none", "empty", "same-acuity", "out-of-range", "not-an-int"])
def test_a_correction_that_changes_nothing_is_refused(graph, run, monkeypatch, corrections):
    """The clean case settles at ESI 3, so 3 is no change. A refusal keeps the
    case at the gate and uses up no correction round.
    """
    from app.mock_cases import DEMO_CASES

    _failing_safety(monkeypatch)
    _, _, thread = run(DEMO_CASES["clean"])

    snap = _answer(graph, thread, "corrected", "charge_nurse", corrections)

    result = hydrate(snap.values)
    assert snap.next == (State.AWAITING_HUMAN_APPROVAL.value,)
    assert transitions(result)[-1] == Transition.BLK
    assert result["correction_rounds"] == 0
    assert result["acuity"] == 3


def test_a_real_correction_is_applied_and_revalidated(graph, run, monkeypatch):
    from app.mock_cases import DEMO_CASES

    _failing_safety(monkeypatch)
    _, _, thread = run(DEMO_CASES["clean"])
    monkeypatch.undo()   # the corrected case passes safety

    snap = _answer(graph, thread, "corrected", "charge_nurse", {"acuity": 2})

    result = hydrate(snap.values)
    assert result["acuity"] == 2
    assert result["order_key"][0] == 2          # re-keyed by the real acuity change (I2)
    assert result["correction_rounds"] == 1
    assert Transition.GATE_SAFETY_CORRECTED in transitions(result)
    assert snap.next == ("awaiting_reassessment",)  # revalidated and queued
