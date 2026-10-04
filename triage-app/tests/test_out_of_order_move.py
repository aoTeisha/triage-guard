"""Out-of-order moves: moving a patient while someone ahead of them is still in
line is allowed, but only with a reason from a fixed list, and the skip is
recorded on the move (docs/SPECIFICATION.md, `move_authorized`).

The positions skipped are worked out by the board's server over the global
queue; here they arrive in the resume payload exactly as the board sends them.
OPA decides; with OPA down a shift lead signs, and the reason is still needed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from langgraph.types import Command

from app.deterministic import SHIFT_LEAD_STANDS_IN, SKIP_REASONS, move_authorized
from app.labels import Transition
from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.symbolic import opa
from app.verification import check_trace
from tests.conftest import transitions

REGO = Path(__file__).parent.parent / "app/symbolic/policy/monitor.rego"


def _resume(graph, thread: str, **payload):
    return hydrate(graph.invoke(Command(resume=payload), config_for(thread)))


def test_skip_reasons_match_the_policy():
    """The OPA rule and the Python stand-in must offer the same reasons."""
    rego = REGO.read_text()
    in_rego = set(re.findall(r'"(\w+)"', re.search(r"move_skip_reasons := \{(.*?)\}", rego).group(1)))
    assert in_rego == SKIP_REASONS
    assert SKIP_REASONS == {"different_care_area", "patient_ahead_unavailable", "clinical_judgment"}


# ---- the rule itself, OPA up and down ----------------------------------------


def test_an_in_order_move_needs_no_reason():
    assert move_authorized(True, True, "nurse")[0]
    assert move_authorized(True, True, "nurse", skipped=[])[0]


def test_an_out_of_order_move_without_a_reason_is_refused():
    ok, why = move_authorized(True, True, "nurse", skipped=[1, 2])
    assert not ok
    assert "out of order" in why and "2 patient(s)" in why


@pytest.mark.parametrize("reason", sorted(SKIP_REASONS))
def test_an_out_of_order_move_with_a_listed_reason_is_allowed(reason):
    assert move_authorized(True, True, "nurse", skipped=[1], skip_reason=reason)[0]


def test_a_reason_outside_the_list_is_refused_and_not_echoed():
    ok, why = move_authorized(True, True, "nurse", skipped=[1], skip_reason="Mr Cohen is in X-ray")
    assert not ok
    assert "Cohen" not in why


def test_a_reason_does_not_stand_in_for_safety_or_role():
    assert not move_authorized(False, True, "nurse", skipped=[1], skip_reason="clinical_judgment")[0]
    assert not move_authorized(True, True, "porter", skipped=[1], skip_reason="clinical_judgment")[0]


def test_opa_down_the_shift_lead_still_needs_a_reason(outage_switch):
    outage_switch.set_down("opa", True)

    ok, why = move_authorized(True, True, "shift_lead", skipped=[1])
    assert not ok
    assert "out of order" in why

    ok, why = move_authorized(True, True, "shift_lead", skipped=[1], skip_reason="clinical_judgment")
    assert ok and why.startswith(SHIFT_LEAD_STANDS_IN)
    assert move_authorized(True, True, "shift_lead")[0]
    assert not move_authorized(True, True, "charge_nurse", skipped=[1],
                               skip_reason="clinical_judgment")[0]


# OPA up vs the stand-in's own facts, over every combination that matters. The
# stand-in also needs a shift lead, so the comparison is made for one.
_CASES = [
    (skipped, reason, safety, approved)
    for skipped in ([], [1], [1, 2, 3])
    for reason in (None, "clinical_judgment", "different_care_area",
                   "patient_ahead_unavailable", "bored")
    for safety in (True, False)
    for approved in (True, False)
]


@pytest.mark.parametrize("skipped,reason,safety,approved", _CASES)
def test_opa_and_the_python_stand_in_agree(skipped, reason, safety, approved, outage_switch):
    up, _ = move_authorized(safety, approved, "shift_lead", skipped=skipped, skip_reason=reason)
    outage_switch.set_down("opa", True)
    down, _ = move_authorized(safety, approved, "shift_lead", skipped=skipped, skip_reason=reason)
    assert up == down


def test_opa_itself_is_what_refuses_the_skip():
    """Straight to the engine, no Python in between."""
    base = {"action": "move", "case": {"safety_passed": True, "approved": True},
            "actor_role": "nurse"}
    assert opa.evaluate(base)["allow"]
    assert opa.evaluate(base | {"queue": {"skipped": [], "skip_reason": None}})["allow"]
    refused = opa.evaluate(base | {"queue": {"skipped": [1], "skip_reason": None}})
    assert not refused["allow"] and any("out of order" in r for r in refused["deny_reasons"])
    assert opa.evaluate(base | {"queue": {"skipped": [1], "skip_reason": "clinical_judgment"}})["allow"]


# ---- through the graph ---------------------------------------------------------


def test_an_out_of_order_move_without_a_reason_is_a_blk(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse",
                     queue_position=3, skipped_positions=[1, 2])

    assert result["clinical_status"] == "waiting"
    last = result["audit_log"][-1]
    assert last["transition"] == Transition.BLK
    assert last["denying_layer"] == "OPA (authorization)"
    assert "out of order" in last["explanation"]
    assert graph.get_state(config_for(thread)).next, "a refused move leaves the case waiting"


def test_an_out_of_order_move_with_a_reason_is_allowed_and_logged(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse",
                     queue_position=3, skipped_positions=[1, 2], skip_reason="different_care_area")

    assert result["clinical_status"] == "treatment_started"
    move = result["audit_log"][-1]
    assert move["transition"] == Transition.MOVE_CONFIRMED
    assert move["queue_position"] == 3
    assert move["skipped_positions"] == [1, 2]
    assert move["skip_reason"] == "different_care_area"
    assert "ahead of #1, #2 (different_care_area)" in move["explanation"]
    assert check_trace(result["audit_log"]).passed


def test_an_in_order_move_records_no_skip(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse",
                     queue_position=1, skipped_positions=[])

    move = result["audit_log"][-1]
    assert move["transition"] == Transition.MOVE_CONFIRMED
    assert move["queue_position"] == 1
    assert "skipped_positions" not in move and "skip_reason" not in move


def test_a_reason_on_an_in_order_move_is_not_logged(graph, run):
    """Nothing was skipped, so there is nothing for a reason to explain."""
    _, _, thread = run(DEMO_CASES["clean"])

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse",
                     queue_position=1, skipped_positions=[], skip_reason="clinical_judgment")

    assert "skip_reason" not in result["audit_log"][-1]


def test_junk_positions_are_dropped_not_logged(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])

    result = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse",
                     skipped_positions=["Mr Cohen", True, 2, 2], skip_reason="clinical_judgment")

    assert result["audit_log"][-1]["skipped_positions"] == [2]


def test_out_of_order_from_the_refile_pause_needs_a_reason_too(graph, run):
    _, _, thread = run(DEMO_CASES["clean"])
    _resume(graph, thread, event="REASSESSMENT_TIMEOUT", fire_id="f1")

    refused = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse",
                      skipped_positions=[1])
    assert refused["clinical_status"] == "reassessment_required"
    assert transitions(refused)[-1] == Transition.BLK

    moved = _resume(graph, thread, event="MOVE_REQUESTED", actor_role="nurse",
                    skipped_positions=[1], skip_reason="patient_ahead_unavailable")
    assert moved["clinical_status"] == "treatment_started"
    assert moved["audit_log"][-1]["skip_reason"] == "patient_ahead_unavailable"
