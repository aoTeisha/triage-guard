"""An authorization engine that cannot answer never blocks treatment or release:
a shift lead signs instead, before the action, and the facts the engine would
have checked are still checked. A real "no" from a working engine is kept.
"""

from __future__ import annotations

from langgraph.types import Command

from app import crm_client
from app.actors import acuity_classifier
from app.deterministic import SHIFT_LEAD_STANDS_IN, move_authorized, release_authorized
from app.labels import Transition
from app.mock_cases import DEMO_CASES
from app.monitor import fire, timers
from app.runner import config_for, hydrate
from app.schemas import AcuityProposal
from app.symbolic import prolog
from app.views import card_from_state, case_view
from tests.conftest import transitions


def _resume(graph, thread: str, **payload):
    return hydrate(graph.invoke(Command(resume=payload), config_for(thread)))


# ---- the checks themselves -------------------------------------------------------


def test_opa_down_lets_a_shift_lead_move_a_cleared_patient(outage_switch):
    outage_switch.set_down("opa", True)

    ok, why = move_authorized(True, True, "shift_lead")

    assert ok
    assert why.startswith(SHIFT_LEAD_STANDS_IN)


def test_opa_down_refuses_anyone_but_a_shift_lead(outage_switch):
    outage_switch.set_down("opa", True)

    ok, why = move_authorized(True, True, "charge_nurse")

    assert not ok
    assert "shift lead sign-off required" in why


def test_opa_down_still_needs_safety_and_approval_for_a_move(outage_switch):
    outage_switch.set_down("opa", True)

    assert not move_authorized(True, False, "shift_lead")[0]
    assert not move_authorized(False, True, "shift_lead")[0]


def test_opa_down_still_needs_a_valid_release_reason(outage_switch):
    outage_switch.set_down("opa", True)

    assert release_authorized("discharge", "shift_lead")[0]
    assert not release_authorized("bored", "shift_lead")[0]
    assert not release_authorized("discharge", "charge_nurse")[0]


def test_a_working_engine_keeps_its_answer_for_a_shift_lead(outage_switch):
    ok, why = release_authorized("bored", "shift_lead")

    assert not ok
    assert not why.startswith(SHIFT_LEAD_STANDS_IN)


def test_prolog_down_lets_only_a_shift_lead_answer_a_gate(outage_switch):
    outage_switch.set_down("prolog", True)

    ok, why = prolog.may_resolve_gate("shift_lead", senior_required=False)
    assert ok and why.startswith(SHIFT_LEAD_STANDS_IN)
    assert not prolog.may_resolve_gate("charge_nurse", senior_required=False)[0]


# ---- through the graph -------------------------------------------------------------


def test_opa_down_a_shift_lead_starts_treatment(graph, run, outage_switch):
    _, _, thread = run(DEMO_CASES["clean"])
    outage_switch.set_down("opa", True)

    refused = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="charge_nurse")
    assert refused["clinical_status"] != "treatment_started"

    moved = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="shift_lead")
    assert moved["clinical_status"] == "treatment_started"
    # A sign-off stand-in, not an intake outage: the case was checked for privacy.
    assert "opa_signoff" in moved["degraded"] and "opa" not in moved["degraded"]


def test_opa_down_a_shift_lead_releases(graph, run, outage_switch, monkeypatch):
    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")
    _, _, thread = run(DEMO_CASES["clean"])
    outage_switch.set_down("opa", True)

    released = _resume(graph, thread, event="RELEASE_REQUESTED", reason="discharge",
                       actor_role="shift_lead")

    assert released["control_state"] == "case_closed"
    assert "opa_signoff" in released["degraded"] and "opa" not in released["degraded"]


def test_prolog_down_a_shift_lead_answers_the_gate(graph, run, outage_switch, monkeypatch):
    # Nurse says 3, the classifier says 1: a gap of 2 pauses for a charge nurse.
    monkeypatch.setattr(acuity_classifier, "classify", lambda payload: AcuityProposal(
        system_proposed_acuity=1, confidence=0.95, acuity_source="system", rationale="stub"))
    _, pending, thread = run(DEMO_CASES["clean"])
    assert pending["gate"] == "discrepancy"
    outage_switch.set_down("prolog", True)

    _resume(graph, thread, decision="use_nurse_acuity", resolver_role="charge_nurse")
    snap = hydrate(graph.get_state(config_for(thread)).values)
    assert Transition.GATE_ACUITY_RESOLVED not in transitions(snap)

    answered = _resume(graph, thread, decision="use_nurse_acuity", resolver_role="shift_lead")
    assert Transition.GATE_ACUITY_RESOLVED in transitions(answered)
    assert "prolog_signoff" in answered["degraded"]
    # Prolog did not decide this row, the shift lead did.
    row = next(r for r in answered["audit_log"] if r.get("transition") == Transition.GATE_ACUITY_RESOLVED)
    assert row["engines"] == []
    assert SHIFT_LEAD_STANDS_IN in row["explanation"]


# ---- the sweeper's write-back ------------------------------------------------------


def test_opa_down_a_write_back_is_retried_not_cancelled(graph, run, conn, monkeypatch, outage_switch):
    _, _, thread = run(DEMO_CASES["clean"], thread=DEMO_CASES["clean"]["case_id"])
    timer_id = timers.schedule(conn, case_id=thread, kind="crm_writeback", schedule_seq=0,
                               due_at="2000-01-01T00:00:00Z")
    timer = next(t for t in timers.all_rows(conn) if t["timer_id"] == timer_id)
    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")
    outage_switch.set_down("opa", True)

    assert fire.handle(conn, timer, graph=graph) == "FAILED"


def test_a_case_view_lists_each_degraded_component_once():
    view = case_view({"case_id": "c1", "degraded": ["prolog", "prolog", "opa", "prolog"]}, None)
    card = card_from_state({"case_id": "c1", "clinical_status": "waiting",
                            "degraded": ["prolog", "prolog"]})
    assert view["degraded"] == ["prolog", "opa"]
    assert card.degraded == ["prolog"]


def test_release_reasons_match_the_policy():
    import re
    from pathlib import Path

    from app.deterministic import RELEASE_REASONS

    rego = (Path(__file__).parent.parent / "app/symbolic/policy/monitor.rego").read_text()
    in_rego = set(re.findall(r'"(\w+)"', re.search(r"release_reasons := \{(.*?)\}", rego).group(1)))
    assert in_rego == RELEASE_REASONS
