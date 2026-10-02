"""Tests for board.commands.answer_pause — the one function every staff action
goes through on its way into a paused case.

These test the mechanism. The *contracts* are tested by two other suites:
`tests/board/test_api.py`'s nine `status` assertions and
`tests/intake/test_case_lifecycle.py`. Neither of those changes, and that is
the point — this module rewires how a write happens without changing what any
endpoint replies.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

from app import runner
from app.mock_cases import DEMO_CASES
from app.runner import start_case
from app.states import State
from board import commands


def _waiting_case(case_id: str) -> dict:
    """Start a clean demo case and let it settle into the waiting-room pause."""
    case = dict(DEMO_CASES["clean"])
    case["case_id"] = case_id
    start_case(case, thread_id=case_id)
    return case


def _monitoring(detail: str = "case is not currently waiting in the queue"):
    return commands.in_control_state(State.MONITORING.value, detail)


def test_an_accepted_action_reports_accepted_and_can_produce_a_view(checkpoint_db):
    _waiting_case("case-accept-1")

    outcome = commands.answer_pause(
        "case-accept-1",
        {"event": "MOVE_REQUESTED", "actor_role": "charge_nurse"},
        _monitoring(),
        require_applied=True,
    )

    assert outcome.accepted is True
    assert outcome.refusal is None
    # The view is available on demand, for the callers that reply with one.
    view = outcome.view()
    assert view["case_id"] == "case-accept-1"
    assert "audit_log" in view


def test_a_refused_action_carries_the_guard_explanation_and_does_not_move_the_case(checkpoint_db):
    """Review Focus 1, at the mechanism level: a refusal is a normal outcome
    carrying the guard's own words, not an exception, and the case has not moved.

    That `refusal` string is what the board's endpoints put in
    `{"status": "denied", "detail": ...}` and what the page shows the nurse. The
    endpoint-level shape is asserted in `test_api.py`, which this task leaves
    untouched.
    """
    _waiting_case("case-refuse-1")

    outcome = commands.answer_pause(
        "case-refuse-1",
        {"event": "RELEASE_REQUESTED", "reason": "discharge", "actor_role": "nurse"},
        commands.open_pause(),
        require_applied=True,
    )

    assert outcome.accepted is False
    assert outcome.refusal                       # the guard's own words, not None
    assert outcome.result["control_state"] == State.MONITORING.value   # did not move


def test_two_concurrent_gate_answers_do_not_both_apply(checkpoint_db):
    """Review Focus 2. The board's release path has an I22 test for this; the
    human-approval gate has none, and this change moves the gate's validation
    from before the case lock to inside it.

    A double-clicked gate answer must resolve the gate once. Whichever request
    loses either sees `check` refuse it — the case is no longer at the gate — or
    is caught by the audit-log growth check. It must not silently report the
    winner's success as its own.
    """
    case = dict(DEMO_CASES["gap"]) if "gap" in DEMO_CASES else None
    if case is None:
        pytest.skip("no gap demo case in app.mock_cases; covered instead by "
                    "tests/intake/test_case_lifecycle.py once Task 5 lands")
    case["case_id"] = "case-gate-race"
    start_case(case, thread_id="case-gate-race")

    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def answer(key):
        barrier.wait()
        try:
            outcomes[key] = commands.answer_pause(
                "case-gate-race",
                {"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
                commands.paused_at(State.AWAITING_HUMAN_APPROVAL.value),
                require_applied=True,
            )
        except HTTPException as exc:
            outcomes[key] = exc

    first = threading.Thread(target=answer, args=("first",))
    second = threading.Thread(target=answer, args=("second",))
    first.start()
    second.start()
    first.join()
    second.join()

    applied = [o for o in outcomes.values() if isinstance(o, commands.Outcome)]
    refused = [o for o in outcomes.values() if isinstance(o, HTTPException)]
    assert len(applied) == 1, "exactly one gate answer may be applied"
    assert len(refused) == 1
    assert refused[0].status_code == 409


def test_a_resume_that_does_not_apply_is_a_409_when_the_caller_requires_it(checkpoint_db, monkeypatch):
    """LangGraph's `Command(resume=...)` on a thread with no pending task returns
    the current state rather than erroring, so a request whose resume never
    applied would otherwise read the *previous* audit log and report someone
    else's outcome as its own. The only reliable signal is that the log did not
    grow.
    """
    _waiting_case("case-noop-1")

    real_graph = runner.graph()

    class FrozenGraph:
        """Invokes nothing: returns the state exactly as it was."""

        def get_state(self, config):
            return real_graph.get_state(config)

        def invoke(self, command, config):
            return dict(real_graph.get_state(config).values)

    monkeypatch.setattr(runner, "graph", lambda: FrozenGraph())

    with pytest.raises(HTTPException) as caught:
        commands.answer_pause(
            "case-noop-1",
            {"event": "MOVE_REQUESTED", "actor_role": "charge_nurse"},
            _monitoring(),
            require_applied=True,
        )

    assert caught.value.status_code == 409
    assert "already left the pause" in caught.value.detail


def test_require_applied_false_does_not_raise_when_the_log_did_not_grow(checkpoint_db, monkeypatch):
    """The intake endpoints have never performed the growth check — they go
    through `runner.resume_case`, which does not look. `require_applied=False`
    preserves that exactly, rather than quietly extending a new refusal to four
    endpoints that never had it. This test is what pins the preservation.
    """
    _waiting_case("case-noop-2")

    real_graph = runner.graph()

    class FrozenGraph:
        def get_state(self, config):
            return real_graph.get_state(config)

        def invoke(self, command, config):
            return dict(real_graph.get_state(config).values)

    monkeypatch.setattr(runner, "graph", lambda: FrozenGraph())

    outcome = commands.answer_pause(
        "case-noop-2",
        {"event": "MOVE_REQUESTED", "actor_role": "charge_nurse"},
        _monitoring(),
        require_applied=False,
    )

    # No exception. Nothing applied, so nothing was refused either.
    assert outcome.accepted is True


def test_an_unknown_case_is_a_404(checkpoint_db):
    with pytest.raises(HTTPException) as caught:
        commands.answer_pause(
            "case-does-not-exist",
            {"event": "MOVE_REQUESTED", "actor_role": "nurse"},
            commands.open_pause(),
            require_applied=True,
        )

    assert caught.value.status_code == 404


def test_in_control_state_refuses_with_the_caller_s_own_wording(checkpoint_db):
    """Each pause gets to say what it is actually waiting for, because that
    string is what a nurse reads. These strings are part of the preserved
    contract: they are the `detail` of a 409 the page already renders.
    """
    _waiting_case("case-wrong-pause-1")

    with pytest.raises(HTTPException) as caught:
        commands.answer_pause(
            "case-wrong-pause-1",
            {"nurse_proposed_acuity": 3, "chief_complaint": "chest_pain", "vitals": {}},
            commands.in_control_state(
                State.REASSESSMENT_REQUIRED.value,
                "case is not awaiting a reassessment re-file",
            ),
            require_applied=False,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == "case is not awaiting a reassessment re-file"


def test_paused_at_reads_the_pending_task_not_the_control_state(checkpoint_db):
    """A bare `interrupt()` pause shows up only as a pending task on the
    checkpoint; `control_state` still holds whatever the previous node set. A
    case parked in the waiting room is not paused at the human-approval gate, and
    `paused_at` must say so.
    """
    _waiting_case("case-pending-1")

    with pytest.raises(HTTPException) as caught:
        commands.answer_pause(
            "case-pending-1",
            {"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
            commands.paused_at(State.AWAITING_HUMAN_APPROVAL.value),
            require_applied=False,
        )

    assert caught.value.status_code == 409
    assert "not paused at" in caught.value.detail
