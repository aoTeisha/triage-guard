"""The move endpoint's two additions: a move from the reassessment-required
column, and the out-of-order rule, where the board's server works out who is
skipped and the graph refuses an unexplained skip.

Real cases through the real graph, as in test_api.py.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.labels import Transition
from app.mock_cases import DEMO_CASES
from app.monitor import timers
from app.runner import run_to_completion

import board.api as api_module

client = TestClient(api_module.app)


def _case(acuity: int, tag: str = "case") -> str:
    case = dict(DEMO_CASES["clean"])
    case["case_id"] = f"{tag}-{acuity}-{uuid4().hex[:6]}"
    case["nurse_proposed_acuity"] = acuity
    run_to_completion(case, thread_id=case["case_id"])
    return case["case_id"]


@pytest.fixture
def line(checkpoint_db):
    """Two queued cases: #1 (ESI 1) ahead of #2 (ESI 4)."""
    return _case(1), _case(4)


def _move(case_id: str, **body):
    return client.post(f"/api/case/{case_id}/move-to-treatment", json={"actor_role": "nurse", **body})


def _card(case_id: str) -> dict:
    return next(c for c in client.get("/api/board").json()["cards"] if c["case_id"] == case_id)


def _trail(case_id: str) -> list[dict]:
    return client.get(f"/api/case/{case_id}").json()["view"]["audit_log"]


def _due(case_id: str) -> None:
    """Report a deterioration, which lands the case in reassessment_required."""
    resp = client.post(f"/api/case/{case_id}/deteriorated",
                       json={"signal": "spo2 dropped", "actor_role": "nurse"})
    assert resp.json() == {"status": "ok"}
    assert _card(case_id)["status"] == "reassessment_required"


# ---- who is ahead, computed on the server ---------------------------------------


def test_queue_place_reports_who_is_ahead(line):
    first, second = line
    assert client.get(f"/api/case/{first}/queue-place").json()["ahead"] == []
    place = client.get(f"/api/case/{second}/queue-place").json()
    assert place["position"] == 2
    assert place["ahead"] == [1]
    assert set(place["skip_reasons"]) == {"different_care_area", "patient_ahead_unavailable",
                                          "clinical_judgment"}


# ---- the out-of-order rule ------------------------------------------------------


def test_moving_the_first_in_line_needs_no_reason(line):
    first, _ = line
    assert _move(first).json() == {"status": "ok"}
    move = _trail(first)[-1]
    assert move["transition"] == Transition.MOVE_CONFIRMED.value
    assert move["queue_position"] == 1
    assert "skip_reason" not in move


def test_an_out_of_order_move_without_a_reason_is_refused(line):
    _, second = line

    resp = _move(second)

    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"
    assert "out of order" in resp.json()["detail"]
    assert _card(second)["status"] == "waiting"
    blk = _trail(second)[-1]
    assert blk["transition"] == Transition.BLK.value
    assert blk["denying_layer"] == "OPA (authorization)"


def test_the_client_cannot_claim_its_own_place(line):
    """Positions in the request body are ignored: the server recomputes them."""
    _, second = line
    resp = _move(second, skipped_positions=[], queue_position=1)
    assert resp.json()["status"] == "denied"


def test_an_out_of_order_move_with_a_reason_is_allowed_and_logged(line):
    _, second = line

    assert _move(second, skip_reason="patient_ahead_unavailable").json() == {"status": "ok"}

    assert _card(second)["status"] == "treatment_started"
    move = _trail(second)[-1]
    assert move["transition"] == Transition.MOVE_CONFIRMED.value
    assert move["queue_position"] == 2
    assert move["skipped_positions"] == [1]
    assert move["skip_reason"] == "patient_ahead_unavailable"


def test_free_text_is_refused_before_it_reaches_the_case(line):
    _, second = line
    before = len(_trail(second))

    resp = _move(second, skip_reason="Mrs Levi is in CT")

    assert resp.status_code == 422
    trail = _trail(second)
    assert len(trail) == before
    assert "Levi" not in str(trail)


def test_a_patient_in_treatment_no_longer_holds_a_place(line):
    first, second = line
    assert _move(first).json() == {"status": "ok"}

    assert client.get(f"/api/case/{second}/queue-place").json() == {
        "position": 1, "ahead": [],
        "skip_reasons": sorted(["clinical_judgment", "different_care_area",
                                "patient_ahead_unavailable"])}
    assert _move(second).json() == {"status": "ok"}


def test_a_patient_due_for_reassessment_still_holds_their_place(line):
    """reassessment_required is still in line: skipping them needs a reason."""
    first, second = line
    _due(first)
    assert _move(second).json()["status"] == "denied"
    assert _move(second, skip_reason="clinical_judgment").json() == {"status": "ok"}


# ---- moving from the reassessment-required column -------------------------------


def test_a_patient_due_for_reassessment_can_be_moved(line):
    first, _ = line
    _due(first)

    assert _move(first).json() == {"status": "ok"}

    assert _card(first)["status"] == "treatment_started"
    view = client.get(f"/api/case/{first}").json()["view"]
    assert view["control_state"] == "monitoring"
    reminders = [t for t in timers.all_rows(timers.connection())
                 if t["case_id"] == first and t["kind"] == "reassessment_reminder"]
    assert [t["fire_state"] for t in reminders] == ["CANCELLED"]
    # And the treatment buttons work from there, exactly as after a normal move.
    done = client.post(f"/api/case/{first}/treatment-complete", json={"actor_role": "nurse"})
    assert done.json() == {"status": "ok"}


def test_a_move_from_reassessment_is_refused_for_the_wrong_role(line):
    first, _ = line
    _due(first)

    resp = _move(first, actor_role="porter")

    assert resp.json()["status"] == "denied"
    assert "porter" in resp.json()["detail"]
    assert _card(first)["status"] == "reassessment_required"


def test_opa_down_a_shift_lead_moves_from_reassessment(line, monkeypatch):
    from app import outages

    first, _ = line
    _due(first)
    monkeypatch.setenv("DEMO_OUTAGES", "1")
    outages.set_down("opa", True)
    try:
        assert _move(first, actor_role="charge_nurse").json()["status"] == "denied"
        assert _move(first, actor_role="shift_lead").json() == {"status": "ok"}
    finally:
        outages.set_down("opa", False)
    assert _card(first)["status"] == "treatment_started"


def test_a_case_at_the_gate_still_cannot_be_moved(checkpoint_db):
    from app.runner import start_case

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = f"gated-{uuid4().hex[:6]}"
    case["nurse_proposed_acuity"] = 5
    start_case(case, thread_id=case["case_id"])

    resp = _move(case["case_id"])

    assert resp.status_code == 409
    assert resp.json()["detail"] == "case is not waiting in the queue or due for reassessment"
