"""The HTTP layer, against real cases run through the real graph.

No crm-stub is started anywhere in this suite. Every case therefore takes the
`db_error` degrade path, and the board still renders — which is rule 8
("a CRM outage must not blank the board") asserted rather than asserted-about.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.mock_cases import DEMO_CASES
from app.runner import run_to_completion

import board.api as api_module

client = TestClient(api_module.app)


@pytest.fixture
def seeded(checkpoint_db):
    """Two clean cases at different acuities, run end to end through the graph."""
    ids = []
    for acuity in (1, 4):
        case = dict(DEMO_CASES["clean"])
        case["case_id"] = f"case-{acuity}-{uuid4().hex[:6]}"
        case["nurse_proposed_acuity"] = acuity
        run_to_completion(case, thread_id=case["case_id"])
        ids.append(case["case_id"])
    return ids


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_board_is_empty_before_any_case(checkpoint_db):
    data = client.get("/api/board").json()
    assert data["cards"] == []
    assert data["counters"]["waiting"] == 0
    # The columns are rendered regardless — an empty board is still a board.
    assert "waiting" in data["columns"]


def test_board_lists_seeded_cases_with_positions(seeded):
    data = client.get("/api/board").json()
    by_id = {c["case_id"]: c for c in data["cards"]}

    assert set(seeded) <= set(by_id)
    assert [c["position"] for c in data["cards"]] == list(range(1, len(data["cards"]) + 1))
    assert data["counters"]["waiting"] == len(data["cards"])
    # The invariant, over whatever the mocked classifier settled on: no queued
    # card sits above an emergent one.
    buckets = [c["bucket"] for c in data["cards"]]
    assert buckets == sorted(buckets, key=lambda b: 0 if b == "emergent" else 1)


def test_board_renders_with_no_crm(seeded):
    """The CRM is down for the whole suite; the cards say so and still render."""
    cards = client.get("/api/board").json()["cards"]
    assert cards
    assert all(c["patient_label"] == "CRM down" for c in cards)
    assert all("crm" in c["degraded"] for c in cards)


def test_case_detail_returns_the_shared_view_and_the_trail(seeded):
    body = client.get(f"/api/case/{seeded[0]}").json()

    assert body["view"]["case_id"] == seeded[0]
    assert body["view"]["audit_log"], "the arrow trail is what the panel renders"
    assert all("arrow" in rec for rec in body["view"]["audit_log"])
    assert body["card"]["case_id"] == seeded[0]
    assert body["card"]["position"] >= 1, "the panel shows the queue position too"
    assert body["checkpoints"] > 0


def test_a_case_suspended_at_the_gate_is_on_the_board(checkpoint_db):
    """The gate node writes `human_review` when it returns — and a suspended run
    has not returned. Without reading the pending task, the one case a charge
    nurse is actually needed for would be missing from the board.
    """
    from app.runner import start_case

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = f"gated-{uuid4().hex[:6]}"
    case["nurse_proposed_acuity"] = 5   # gap >= 2 against the mock: goes to the gate
    _, pending = start_case(case, thread_id=case["case_id"])
    assert pending, "the fixture must actually suspend, or this proves nothing"

    data = client.get("/api/board").json()
    card = next(c for c in data["cards"] if c["case_id"] == case["case_id"])
    assert card["status"] == "human_review"
    assert card["gate_pending"] is True
    assert data["counters"]["human_review"] == 1

    detail = client.get(f"/api/case/{case['case_id']}").json()
    assert detail["view"]["status"] == "awaiting_human_approval"


def test_unknown_case_is_404(checkpoint_db):
    assert client.get("/api/case/nope").status_code == 404


def test_notifications_come_from_the_audit_arrows(seeded):
    notes = client.get("/api/board").json()["notifications"]
    assert notes
    assert all(n["arrow"] in api_module.NOTIFY_ARROWS for n in notes)


def test_a_notification_names_the_complaint_even_with_no_redacted_payload():
    """The cases that generate notifications — missing fields, unusable scan,
    refused input — fail before a redacted payload exists. Falling back to the
    raw one keeps the strip readable; only the complaint is read from it.
    """
    state = {
        "case_id": "c-1",
        "raw_payload": {"chief_complaint": "ankle pain", "stable_patient_id": "P-1"},
        "audit_log": [{"case_id": "c-1", "arrow": "16", "action": "notify_user",
                       "explanation": "request fields", "at": "2026-01-01T00:00:00+00:00"}],
    }
    note = api_module.notifications([state])[0]
    assert note["complaint"] == "ankle pain"
    assert "P-1" not in str(note)


def test_the_board_issues_no_writes(seeded):
    """M2 is not built: there is no move/release endpoint to call by accident."""
    assert client.post(f"/api/case/{seeded[0]}/move", json={}).status_code == 404
    assert client.post(f"/api/case/{seeded[0]}/release", json={}).status_code == 404
    assert not [r for r in api_module.app.routes if getattr(r, "methods", set()) & {"POST"}]
