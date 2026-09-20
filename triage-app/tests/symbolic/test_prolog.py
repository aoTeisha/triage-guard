"""The Prolog layer (`app.symbolic.prolog` + `rules/monitor.pl`): I14
authorization and per-timer classification, each with an explanation.
Runs the real SWI-Prolog engine through pyswip — no mocks.
"""

from __future__ import annotations

from app.deterministic import actor_is_charge
from app.symbolic import prolog


def _ctx(**over):
    base = {"timer_id": "c1:reassessment:0", "kind": "reassessment", "fire_state": "DUE",
            "pause_active": True, "notify_count": 0, "notify_budget": 5}
    return base | over


def test_charge_roles_are_the_two_the_spec_names():
    assert prolog.charge_role("charge_nurse") == (True, "charge_nurse holds charge role")
    assert prolog.charge_role("shift_lead")[0] is True
    assert prolog.charge_role("nurse")[0] is False
    assert prolog.charge_role("")[0] is False


def test_a_refusal_explains_itself():
    ok, why = prolog.charge_role("nurse")
    assert not ok and why == "gate refused: role 'nurse' is not a charge role"


def test_a_case_handed_to_a_senior_needs_a_shift_lead():
    """I14 + I8: once `senior_required` is set, a charge nurse is no longer enough."""
    assert prolog.may_resolve_gate("shift_lead", senior_required=True)[0] is True
    ok, why = prolog.may_resolve_gate("charge_nurse", senior_required=True)
    assert not ok and why == "gate refused: role 'charge_nurse'; a shift lead must decide"
    assert prolog.may_resolve_gate("charge_nurse", senior_required=False)[0] is True


def test_roles_from_http_input_are_quoted_not_interpolated():
    # A quote in the role must neither crash the engine nor be parsed as Prolog.
    assert prolog.charge_role("o'brien")[0] is False
    assert prolog.charge_role("x). charge_role(y")[0] is False
    assert prolog.atom("o'brien") == "'o\\'brien'"


def test_actor_is_charge_is_the_prolog_engine():
    assert actor_is_charge("charge_nurse") == prolog.charge_role("charge_nurse")
    assert actor_is_charge("nurse") == prolog.charge_role("nurse")


def test_due_reassessment_dispatches():
    assert prolog.timer_action(_ctx()) == ("dispatch", "")


def test_failed_reassessment_redispatches():
    assert prolog.timer_action(_ctx(fire_state="FAILED"))[0] == "dispatch"


def test_unknown_reassessment_must_reconcile_never_dispatch():
    action, why = prolog.timer_action(_ctx(fire_state="UNKNOWN"))
    assert action == "reconcile"
    assert why == "dispatch: blind_redispatch_from_unknown"


def test_lease_expired_dispatching_counts_as_unknown():
    assert prolog.timer_action(_ctx(fire_state="DISPATCHING"))[0] == "reconcile"


def test_every_reminder_kind_notifies_while_its_pause_is_open():
    for kind in ("gate_reminder", "reassessment_reminder", "senior_reminder"):
        assert prolog.timer_action(_ctx(timer_id=f"c1:{kind}:0", kind=kind)) == ("notify", "")


def test_reminder_whose_pause_resolved_is_cancelled():
    action, why = prolog.timer_action(_ctx(timer_id="c1:gate_reminder:0", kind="gate_reminder", pause_active=False))
    assert action == "cancel"
    assert why == "notify: reminder_pause_resolved"


def test_reminder_over_budget_fails_loudly():
    action, why = prolog.timer_action(
        _ctx(timer_id="c1:senior_reminder:0", kind="senior_reminder", notify_count=5, notify_budget=5))
    assert action == "fail_budget"
    assert why == "notify: notification_budget_exhausted"


def test_unknown_timer_kind_is_cancelled_not_delivered_blind():
    assert prolog.timer_action(_ctx(timer_id="c1:safety_park:0", kind="safety_park"))[0] == "cancel"


def test_facts_do_not_leak_between_queries():
    prolog.timer_action(_ctx(fire_state="UNKNOWN"))
    assert prolog.timer_action(_ctx())[0] == "dispatch"


def test_an_engine_fault_is_a_deny_with_a_reason_not_a_crash(monkeypatch):
    """Fail closed (same principle `timer_action` already follows for the
    sweeper): a query-time engine fault must not escape `charge_role` or
    `may_resolve_gate` as a bare exception — it comes back as a refusal.
    """

    class _BoomEngine:
        def query(self, goal):
            raise RuntimeError("engine on fire")

    monkeypatch.setattr(prolog, "_engine", lambda: _BoomEngine())

    ok, why = prolog.charge_role("charge_nurse")
    assert ok is False
    assert "prolog engine unavailable" in why
    assert "engine on fire" in why

    ok, why = prolog.may_resolve_gate("shift_lead", senior_required=True)
    assert ok is False
    assert "prolog engine unavailable" in why
    assert "engine on fire" in why


def test_timer_action_survives_the_engine_itself_failing_to_start(monkeypatch):
    """Regression: `_engine()` used to be called outside timer_action's try
    block, so an engine-init fault (e.g. swipl failed to start) escaped
    unhandled all the way up through fire.handle -> sweeper -> sweeper.main,
    killing the sweeper. It must instead come back as the documented
    ("engine_unavailable", <error>) tuple, same as a query-time fault does.
    """

    def boom():
        raise RuntimeError("swipl process failed to start")

    monkeypatch.setattr(prolog, "_engine", boom)

    action, why = prolog.timer_action(_ctx())

    assert action == "engine_unavailable"
    assert "swipl process failed to start" in why
