"""The BLK row: an attempted action is refused and the case does not move.

This is the behaviour the CrewAI Flow had no primitive for. A `@router` there had
to return a branch and every branch ran; there was no way to express "nothing
happens". `state_machine.py` modelled it and was never called by anything.
"""

from __future__ import annotations

from langgraph.types import Command

from app.deterministic import actor_is_charge, move_authorized, release_authorized
from app.monitor import fire
from app.runner import hydrate
from app.states import State
from tests.conftest import arrows
from tests.test_gates import CHARGE, GAP_CASE

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


def test_release_needs_a_valid_reason_and_a_charge_role():
    assert release_authorized("discharge", "charge_nurse")[0] is True
    assert release_authorized("ama", "shift_lead")[0] is True
    assert release_authorized("discharge", "nurse")[0] is False       # role
    assert release_authorized("not_a_reason", "charge_nurse")[0] is False  # reason
    assert release_authorized("", "charge_nurse")[0] is False


def test_release_denial_explains_itself():
    ok, why = release_authorized("discharge", "nurse")
    assert not ok and "nurse" in why
    ok, why = release_authorized("bogus", "charge_nurse")
    assert not ok and "bogus" in why


# ---- BLK end to end -----------------------------------------------------------


def test_an_unauthorized_resolver_is_refused_and_the_gate_stays_open(graph, run):
    _, pending, thread = run(GAP_CASE)
    assert pending is not None
    config = {"configurable": {"thread_id": thread}}

    denied = hydrate(graph.invoke(Command(resume=JUNIOR), config))

    assert "BLK" in arrows(denied)
    assert denied["control_state"] == State.AWAITING_HUMAN_APPROVAL.value
    assert State.AWAITING_HUMAN_APPROVAL.value in graph.get_state(config).next, \
        "the run must still be paused at the gate, not ended"


def test_a_refused_gate_changes_no_case_state(graph, run):
    """The attempt dies; the case is exactly where it was."""
    paused, pending, thread = run(GAP_CASE)
    assert pending is not None

    denied = hydrate(
        graph.invoke(Command(resume=JUNIOR), {"configurable": {"thread_id": thread}})
    )

    # "The case does not move" means literally unchanged, so compare fields
    # against the paused snapshot rather than against assumed defaults.
    # `acuity_source` is already set here — the classifier proposed at arrow 8,
    # long before the gate — and a denial must not clear that either.
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


def test_a_refused_gate_can_still_be_resolved_afterwards(graph, run):
    """The whole bug: after a junior's attempt was refused, a charge nurse
    must still be able to answer the same gate.
    """
    _, _, thread = run(GAP_CASE)
    config = {"configurable": {"thread_id": thread}}
    graph.invoke(Command(resume=JUNIOR), config)

    resolved = hydrate(graph.invoke(Command(resume=CHARGE), config))

    assert resolved["acuity_source"] == "human_confirmed"
    assert State.AWAITING_HUMAN_APPROVAL.value not in graph.get_state(config).next


def test_a_refused_gate_keeps_its_reminders_deliverable(conn, graph, run):
    """A reminder is only sent while the gate is genuinely open — `fire.notify`
    cancels it otherwise. Ending the run on a refusal silently cancelled both
    reminders, so nobody was ever nudged about the stranded case.
    """
    _, _, thread = run(GAP_CASE)
    graph.invoke(Command(resume=JUNIOR), {"configurable": {"thread_id": thread}})
    timer = {"timer_id": "t-after-refusal", "case_id": thread, "kind": "gate_reminder",
             "schedule_seq": 0, "due_at": "2000-01-01T00:00:00Z"}

    assert fire.notify(conn, timer, graph=graph) == "DELIVERED"


def test_a_denial_names_the_layer_that_refused():
    """I18: a BLK row that says 'denied' without saying who denied it is not
    an explanation. Move and release are OPA's; the gate is Prolog's."""
    from app.deterministic import audit_denial
    from app.states import State

    row = audit_denial("c1", State.MONITORING, "move refused: not approved", layer="OPA (authorization)")
    assert row["denying_layer"] == "OPA (authorization)"
    assert audit_denial("c1", State.AWAITING_HUMAN_APPROVAL, "x")["denying_layer"] == "Prolog (authorization)"
    # The arrow stays BLK whatever the layer — `routers.release_route` routes
    # off that arrow, so a new kwarg must not change it.
    assert row["arrow"] == "BLK"
