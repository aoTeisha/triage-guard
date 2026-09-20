"""The OPA gate (`app.symbolic.opa` + `policy/monitor.rego`), evaluated by the
real `opa` binary: I5 no bypass, I9 release, and the monitor's own dispatch
and notify gates. Default deny; every deny names its reason; a missing or
broken engine is a deny too.
"""

from __future__ import annotations

import json
import os
import subprocess

from app.symbolic import opa, prolog


def test_dispatch_is_allowed_from_the_monitoring_pause():
    assert opa.evaluate({"action": "dispatch", "case": {"control_state": "monitoring"},
                         "timer": {"fire_state": "DUE"}}) == {"allow": True, "deny_reasons": []}


def test_dispatch_from_unknown_is_denied_with_the_reason():
    decision = opa.evaluate({"action": "dispatch", "case": {"control_state": "monitoring"},
                             "timer": {"fire_state": "UNKNOWN"}})
    assert decision == {"allow": False, "deny_reasons": ["blind_redispatch_from_unknown"]}


def test_dispatch_off_the_pause_is_denied():
    decision = opa.evaluate({"action": "dispatch", "case": {"control_state": "reassessment_required"},
                             "timer": {"fire_state": "DUE"}})
    assert decision["allow"] is False
    assert "case_not_at_monitoring_pause" in decision["deny_reasons"]


def test_notify_needs_an_open_pause_and_budget():
    assert opa.evaluate({"action": "notify", "pause_active": True, "notify_count": 0, "notify_budget": 5})["allow"]
    assert opa.evaluate({"action": "notify", "pause_active": False, "notify_count": 0, "notify_budget": 5}) == \
        {"allow": False, "deny_reasons": ["reminder_pause_resolved"]}
    assert opa.evaluate({"action": "notify", "pause_active": True, "notify_count": 5, "notify_budget": 5}) == \
        {"allow": False, "deny_reasons": ["notification_budget_exhausted"]}


def test_move_needs_safety_approval_and_role():
    """I5: no bypass into treatment."""
    ok = {"action": "move", "case": {"safety_passed": True, "approved": True}, "actor_role": "nurse"}
    assert opa.evaluate(ok)["allow"] is True
    assert opa.evaluate(ok | {"actor_role": "porter"})["deny_reasons"] == ['move refused: role "porter" not authorized']
    assert opa.evaluate({**ok, "case": {"safety_passed": False, "approved": True}})["deny_reasons"] == \
        ["move refused: safety not passed"]
    assert opa.evaluate({**ok, "case": {"safety_passed": True, "approved": False}})["deny_reasons"] == \
        ["move refused: not approved"]


def test_release_needs_a_valid_reason_and_a_charge_role():
    """I9: releasable from any pause, but only with a valid reason and signer."""
    assert opa.evaluate({"action": "release", "reason": "discharge", "actor_role": "charge_nurse"})["allow"]
    assert opa.evaluate({"action": "release", "reason": "discharge", "actor_role": "nurse"})["deny_reasons"] == \
        ['release refused: role "nurse" is not a charge role']
    assert opa.evaluate({"action": "release", "reason": "bogus", "actor_role": "charge_nurse"})["deny_reasons"] == \
        ['release refused: invalid reason "bogus"']


def test_unknown_action_is_denied_by_default():
    assert opa.evaluate({"action": "launch_missiles"}) == {"allow": False, "deny_reasons": ["unknown_action"]}


def test_charge_roles_match_prolog_charge_role():
    """I14: `charge_roles` here (release, I9) and `charge_role/1` in
    rules/monitor.pl (gate resolution, I14) independently restate the same
    role set — OPA can't call into Prolog to delegate. This is the drift
    check the sync comments in both files point at.
    """
    completed = subprocess.run(
        [os.environ.get("OPA_BIN", "opa"), "eval", "-d", str(opa.POLICY), "--format=raw",
         "data.triage.monitor.charge_roles"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    opa_roles = set(json.loads(completed.stdout))
    prolog_roles = {row["R"] for row in prolog._engine().query("charge_role(R)")}
    assert opa_roles == prolog_roles


def test_a_missing_engine_is_a_deny_not_an_exception(monkeypatch):
    monkeypatch.setenv("OPA_BIN", "/nonexistent/opa")
    decision = opa.evaluate({"action": "release", "reason": "discharge", "actor_role": "charge_nurse"})
    assert decision["allow"] is False
    assert decision["deny_reasons"][0].startswith("engine_unavailable:opa")
