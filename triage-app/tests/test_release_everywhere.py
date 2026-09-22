"""I9: a release is possible from any pause before the case is closed.

Each pause is reached the way a real case reaches it, then answered with a
release. A charge role with a valid reason closes the case; anyone else is
refused and the case stays paused exactly where it was.
"""

from __future__ import annotations

import pytest
from langgraph.types import Command

from app.actors import normalizer
from app.mock_cases import DEMO_CASES
from app.runner import hydrate
from app.states import State
from tests.conftest import arrows
from tests.test_gates import GAP_CASE

RELEASE = {"event": "RELEASE_REQUESTED", "reason": "ama", "actor_role": "charge_nurse"}
NOT_CHARGE = {**RELEASE, "actor_role": "nurse"}


def _cfg(thread):
    return {"configurable": {"thread_id": thread}}


def _at_gate(graph, run, monkeypatch):
    _, _, thread = run(GAP_CASE)
    return thread, State.AWAITING_HUMAN_APPROVAL.value


def _at_intake_fix(graph, run, monkeypatch):
    _, _, thread = run(DEMO_CASES["missing"])
    return thread, "awaiting_intake_fix"


def _at_recovery(graph, run, monkeypatch):
    monkeypatch.setattr(normalizer, "drop_identifiers", lambda fields: dict(fields))
    _, _, thread = run(DEMO_CASES["clean"])
    return thread, "awaiting_recovery"


def _at_refile(graph, run, monkeypatch):
    _, _, thread = run(DEMO_CASES["clean"])
    graph.invoke(Command(resume={"event": "REASSESSMENT_TIMEOUT", "fire_id": "f1"}), _cfg(thread))
    return thread, "awaiting_reassessment_submission"


PAUSES = [_at_gate, _at_intake_fix, _at_recovery, _at_refile]


@pytest.mark.parametrize("reach", PAUSES, ids=lambda f: f.__name__)
def test_a_charge_role_can_release_from_this_pause(graph, run, monkeypatch, reach):
    thread, pause = reach(graph, run, monkeypatch)
    assert graph.get_state(_cfg(thread)).next == (pause,)

    graph.invoke(Command(resume=RELEASE), _cfg(thread))

    snap = graph.get_state(_cfg(thread))
    result = hydrate(snap.values)
    assert snap.next == ()                                   # the run ended: released
    assert result["control_state"] == State.CASE_CLOSED.value
    assert result["release_reason"] == "ama"


@pytest.mark.parametrize("reach", PAUSES, ids=lambda f: f.__name__)
def test_a_refused_release_leaves_the_case_paused_where_it_was(graph, run, monkeypatch, reach):
    thread, pause = reach(graph, run, monkeypatch)

    graph.invoke(Command(resume=NOT_CHARGE), _cfg(thread))

    snap = graph.get_state(_cfg(thread))
    assert snap.next == (pause,)
    assert arrows(hydrate(snap.values))[-1] == "BLK"


def test_a_reminder_left_over_after_release_is_cancelled(graph, run, monkeypatch, conn):
    """A released case is no longer at the gate, so its reminder is dropped,
    not sent (the sweeper checks the pause before notifying).
    """
    from app.monitor import fire

    thread, _ = _at_gate(graph, run, monkeypatch)
    graph.invoke(Command(resume=RELEASE), _cfg(thread))

    timer = {"timer_id": "g0", "case_id": thread, "kind": "gate_reminder",
             "schedule_seq": 0, "due_at": "2000-01-01T00:00:00Z"}
    assert fire.notify(conn, timer, graph=graph) == "CANCELLED"
