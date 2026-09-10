"""The BLK row: an attempted action is refused and the case does not move.

This is the behaviour the CrewAI Flow had no primitive for. A `@router` there had
to return a branch and every branch ran; there was no way to express "nothing
happens". `state_machine.py` modelled it and was never called by anything.
"""

from __future__ import annotations

from langgraph.types import Command

from app.deterministic import actor_is_charge, move_authorized
from app.runner import hydrate
from app.states import State
from tests.conftest import arrows
from tests.test_gates import GAP_CASE

JUNIOR = {"decision": "use_system_acuity", "resolver_role": "nurse"}


# ---- the authorization predicates themselves --------------------------------


def test_only_charge_roles_may_resolve_a_gate():
    assert actor_is_charge("charge_nurse")[0] is True
    assert actor_is_charge("shift_lead")[0] is True
    assert actor_is_charge("nurse")[0] is False
    assert actor_is_charge("")[0] is False


def test_a_denial_explains_itself():
    """`explain_denial` needs a reason, not just a boolean."""
    ok, why = actor_is_charge("nurse")
    assert not ok and "nurse" in why


def test_a_treatment_move_needs_safety_and_approval_and_a_role():
    """No-approval-bypass, the invariant `move_authorized` exists to enforce."""
    assert move_authorized(True, True, "charge_nurse")[0] is True
    assert move_authorized(False, True, "charge_nurse")[0] is False
    assert move_authorized(True, False, "charge_nurse")[0] is False
    assert move_authorized(True, True, "porter")[0] is False


def test_move_refusal_names_which_condition_failed():
    assert "safety" in move_authorized(False, True, "nurse")[1]
    assert "approved" in move_authorized(True, False, "nurse")[1]


# ---- BLK end to end -----------------------------------------------------------


def test_an_unauthorized_resolver_is_refused_at_the_gate(graph, run):
    _, pending, thread = run(GAP_CASE)
    assert pending is not None

    denied = hydrate(
        graph.invoke(Command(resume=JUNIOR), {"configurable": {"thread_id": thread}})
    )

    assert denied["control_state"] == State.ACTION_DENIED.value
    assert "BLK" in arrows(denied)


def test_a_refused_gate_changes_no_case_state(graph, run):
    """The attempt dies; the case is exactly where it was."""
    paused, pending, thread = run(GAP_CASE)
    assert pending is not None

    denied = hydrate(
        graph.invoke(Command(resume=JUNIOR), {"configurable": {"thread_id": thread}})
    )

    # "The case does not move" means literally unchanged, so compare fields
    # against the paused snapshot rather than against assumed defaults.
    # `acuity_source` is already `rule_forced` here — the classifier set it at
    # arrow 8, long before the gate — and a denial must not clear that either.
    for field in ("acuity", "acuity_source", "acuity_bucket", "order_key",
                  "approved", "safety_passed", "acuity_gap"):
        assert denied[field] == paused[field], f"{field} changed on a denied action"


def test_a_denial_records_the_denying_layer(graph, run):
    """An audit row that says 'denied' without saying who denied it is not an
    explanation.
    """
    _, _, thread = run(GAP_CASE)
    denied = hydrate(
        graph.invoke(Command(resume=JUNIOR), {"configurable": {"thread_id": thread}})
    )

    blk = [r for r in denied["audit_log"] if r["arrow"] == "BLK"]
    assert blk
    assert any(r.get("denying_layer") for r in blk)


def test_a_denied_case_never_reaches_the_queue(graph, run):
    _, _, thread = run(GAP_CASE)
    denied = hydrate(
        graph.invoke(Command(resume=JUNIOR), {"configurable": {"thread_id": thread}})
    )

    assert denied["control_state"] != State.MONITORING.value
    assert "11·pass" not in arrows(denied)
