"""The human gates: a real pause, a real checkpoint, a real resume.

These are the tests that would have been impossible under the old Flow, where
`@human_feedback` would have handed the charge nurse's free text to gpt-4o-mini
for interpretation. Here the decision is a structured payload and no model is
involved.
"""

from __future__ import annotations

import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.graph import build_graph
from app.runner import hydrate
from app.states import State
from tests.conftest import arrows

# nurse says 5, the red-flag pre-check forces 2 -> gap 3 -> arrow 9c.
GAP_CASE = {
    "case_id": "case-gap",
    "channel": "website",
    "stable_patient_id": "300000009",
    "nurse_proposed_acuity": 5,
    "chief_complaint": "chest pain radiating to the arm",
    "vitals": {"hr": 120, "bp": "160/100", "spo2": 94, "temp_c": 37.0},
    "free_text": "Sudden crushing chest pain.",
}

CHARGE = {"decision": "use_system_acuity", "resolver_role": "charge_nurse"}


def test_a_major_gap_pauses_the_run(run):
    """Arrow 9c: gap >= 2 is not resolved by the machine."""
    state, pending, _ = run(GAP_CASE)

    assert pending is not None
    assert pending["gate"] == "discrepancy"
    assert pending["required_role"] == "charge_nurse"
    assert pending["nurse_proposed_acuity"] == 5
    assert pending["system_proposed_acuity"] == 2
    assert "9c" in arrows(state)


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
    assert "1b.z·acuity" in arrows(resumed)


def test_a_resolved_gate_re_runs_safety_before_the_queue(graph, run):
    """No approval bypass: the gate returns to safety_validating, not monitoring."""
    _, _, thread = run(GAP_CASE)
    resumed = hydrate(graph.invoke(Command(resume=CHARGE), {"configurable": {"thread_id": thread}}))

    trail = arrows(resumed)
    assert trail.index("1b.z·acuity") < trail.index("10")
    assert resumed["control_state"] == State.MONITORING.value
    assert resumed["safety_passed"] is True


def test_the_pause_survives_a_rebuilt_graph(tmp_path):
    """The real test of durability: throw the graph object away between the pause
    and the resume, as a restarted process would.
    """
    db = str(tmp_path / "gate.db")
    cfg = {"configurable": {"thread_id": "case-gap"}}

    conn_a = sqlite3.connect(db, check_same_thread=False)
    first = build_graph(checkpointer=SqliteSaver(conn_a))
    paused = first.invoke(
        {"case_id": GAP_CASE["case_id"], "raw_payload": dict(GAP_CASE),
         "nurse_proposed_acuity": 5},
        cfg,
    )
    assert paused.get("__interrupt__")
    conn_a.close()
    del first

    # A different graph object, a different connection — only the checkpoint links them.
    conn_b = sqlite3.connect(db, check_same_thread=False)
    second = build_graph(checkpointer=SqliteSaver(conn_b))
    resumed = hydrate(second.invoke(Command(resume=CHARGE), cfg))
    conn_b.close()

    assert resumed["acuity"] == 2
    assert resumed["control_state"] == State.MONITORING.value
    # The trail spans both processes.
    assert "9c" in arrows(resumed) and "1b.z·acuity" in arrows(resumed)


def test_nurse_choice_is_honoured_when_that_is_what_the_charge_nurse_picks(graph, run):
    """The clinician is the final authority: a red flag is advisory, not a floor."""
    _, _, thread = run(GAP_CASE)
    resumed = hydrate(graph.invoke(
        Command(resume={"decision": "use_nurse_acuity", "resolver_role": "charge_nurse"}),
        {"configurable": {"thread_id": thread}},
    ))

    assert resumed["acuity"] == 5
    assert resumed["acuity_source"] == "human_confirmed"
    # The red-flag fact survives the override, for firing-rate tuning.
    assert resumed["red_flag_fired"] is True
