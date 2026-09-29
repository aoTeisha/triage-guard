"""The outage switch: a component flagged down in Postgres stays down for every
caller until it is switched back, and nothing reads the flags unless the demo
switch is enabled.
"""

from __future__ import annotations

import pytest

from app import outages
from app.actors import acuity_classifier
from app.monitor import sweeper, timers
from app.symbolic import datalog, opa, prolog


def test_nothing_is_down_while_the_switch_is_disabled(monkeypatch):
    monkeypatch.delenv("DEMO_OUTAGES", raising=False)
    assert not outages.enabled()
    assert all(not outages.is_down(c) for c in outages.COMPONENTS)
    outages.check("opa")          # never raises


def test_a_flag_stays_down_until_switched_back(outage_switch):
    outage_switch.set_down("opa", True)
    assert outage_switch.is_down("opa")
    assert outage_switch.down_list() == ["opa"]
    with pytest.raises(RuntimeError, match="simulated outage: opa"):
        outage_switch.check("opa")

    outage_switch.set_down("opa", False)
    assert not outage_switch.is_down("opa")
    assert outage_switch.down_list() == []


# ---- each component's hook -----------------------------------------------------


def test_the_llm_hook(outage_switch):
    outage_switch.set_down("llm", True)
    with pytest.raises(RuntimeError, match="simulated outage: llm"):
        acuity_classifier.classify({"case_id": "c1"})


def test_the_opa_hook_is_an_engine_unavailable_answer(outage_switch):
    outage_switch.set_down("opa", True)
    gate = opa.evaluate({"action": "release", "reason": "discharge", "actor_role": "charge_nurse"})
    assert gate["allow"] is False
    assert gate["deny_reasons"][0].startswith("engine_unavailable:opa")


def test_the_prolog_hook(outage_switch):
    outage_switch.set_down("prolog", True)
    assert prolog.charge_roles() == frozenset()
    outage_switch.set_down("prolog", False)
    assert "charge_nurse" in prolog.charge_roles()


def test_the_datalog_hook(outage_switch):
    outage_switch.set_down("datalog", True)
    result = datalog.acuity_provenance([], {}, {"charge_nurse"})
    assert result["engine_error"] and "simulated outage: datalog" in result["engine_error"]


def test_the_monitor_hook_skips_the_tick_and_the_heartbeat(outage_switch, conn):
    conn.execute("DELETE FROM sweeper_heartbeats")
    outage_switch.set_down("monitor", True)

    claimed = sweeper.run_once(conn, worker_id="w-test", graph=object())

    assert claimed == []
    assert timers.heartbeat_status(conn, stale_after_seconds=60)["degraded"] is True
