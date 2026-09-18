"""The HTTP layer, against real cases run through the real graph.

No crm-stub is started anywhere in this suite, so every case takes the
"CRM unreachable" degrade path. The board must still render correctly when
that happens — this suite proves that by actually exercising it, rather than
just asserting about it in isolation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.mock_cases import DEMO_CASES
from app.monitor import timers
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
    """The gate node writes `human_review` status when it *returns* — but a
    suspended run is paused mid-node and never returns. Without separately
    checking for that pending pause, the one case that actually needs a
    charge nurse right now would be missing from the board.
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
    """The cases that generate notifications (missing fields, unusable scan,
    refused input) all fail *before* a redacted payload gets built. Falling
    back to the raw payload keeps the notification readable; only the
    complaint text is ever read from it, never a patient identifier.
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


def test_heartbeat_endpoint_reports_degraded_with_no_worker(checkpoint_db):
    data = client.get("/api/heartbeat").json()
    assert data["degraded"] is True
    assert data["workers"] == []


def test_heartbeat_endpoint_reports_healthy_after_a_beat(checkpoint_db):
    timers.heartbeat(timers.connection(), worker_id="w1")

    data = client.get("/api/heartbeat").json()

    assert data["degraded"] is False
    assert data["workers"][0]["worker_id"] == "w1"


def test_board_counters_show_monitor_degraded(checkpoint_db):
    assert client.get("/api/board").json()["counters"]["monitor_degraded"] is True

    timers.heartbeat(timers.connection(), worker_id="w1")

    assert client.get("/api/board").json()["counters"]["monitor_degraded"] is False


def test_the_board_issues_writes_only_through_named_exceptions(seeded):
    """Move/release now exist, but only as named, single-purpose re-entry
    points — not a general write API. `/move` (no hyphen) still isn't a
    real path, guarding against an accidental typo'd route silently working.
    """
    assert client.post(f"/api/case/{seeded[0]}/move", json={}).status_code == 404
    post_paths = {
        r.path for r in api_module.app.routes if getattr(r, "methods", set()) & {"POST"}
    }
    assert post_paths == {
        "/api/case/{case_id}/deteriorated",
        "/api/case/{case_id}/move-to-treatment",
        "/api/case/{case_id}/release",
    }


def test_move_to_treatment_endpoint_updates_the_card(seeded):
    resp = client.post(f"/api/case/{seeded[0]}/move-to-treatment",
                        json={"actor_role": "nurse"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    card = next(c for c in client.get("/api/board").json()["cards"]
                if c["case_id"] == seeded[0])
    assert card["status"] == "treatment_started"


def test_release_endpoint_marks_the_card_released(seeded):
    """The card is NOT removed from `/api/board`'s `cards` — it moves to the
    `patient_released` status, and the frontend's drawer section (already
    built, board.js `DRAWER_COLUMN`) is what visually separates it from the
    main columns.
    """
    resp = client.post(f"/api/case/{seeded[0]}/release",
                        json={"reason": "discharge", "actor_role": "charge_nurse"})
    assert resp.status_code == 200

    card = next(c for c in client.get("/api/board").json()["cards"]
                if c["case_id"] == seeded[0])
    assert card["status"] == "patient_released"

    detail = client.get(f"/api/case/{seeded[0]}").json()
    assert detail["view"]["release_reason"] == "discharge"


def test_release_endpoint_defaults_actor_role_to_nurse_not_charge_nurse(seeded):
    """An omitted actor_role must degrade safely — get denied by
    release_authorized — not silently grant release authority.
    """
    resp = client.post(f"/api/case/{seeded[0]}/release", json={"reason": "discharge"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"


def test_racing_release_requests_each_report_their_own_outcome(seeded):
    """Two near-simultaneous /release calls for the same case — a plain
    nurse (must be denied) and a charge nurse (must succeed) — must each
    report their OWN outcome, not whichever audit row landed last in the
    checkpoint under real thread-pool concurrency.
    """
    import threading

    barrier = threading.Barrier(2)
    results = {}

    def call(role, key):
        barrier.wait()
        resp = client.post(f"/api/case/{seeded[0]}/release",
                            json={"reason": "discharge", "actor_role": role})
        results[key] = resp.json()

    t1 = threading.Thread(target=call, args=("nurse", "nurse"))
    t2 = threading.Thread(target=call, args=("charge_nurse", "charge"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results["nurse"]["status"] == "denied"
    assert results["charge"]["status"] == "ok"


def test_release_endpoint_refuses_a_plain_nurse(seeded):
    resp = client.post(f"/api/case/{seeded[0]}/release",
                        json={"reason": "discharge", "actor_role": "nurse"})
    assert resp.status_code == 200   # accepted, but denied inside the graph — see docstring
    assert resp.json()["status"] == "denied"
    assert "nurse" in resp.json()["detail"]

    card = next(c for c in client.get("/api/board").json()["cards"]
                if c["case_id"] == seeded[0])
    assert card["status"] == "waiting", "a denied release must not change clinical status"

    # And the case must still be retriable — not permanently ended by the denial.
    retry = client.post(f"/api/case/{seeded[0]}/release",
                         json={"reason": "discharge", "actor_role": "charge_nurse"})
    assert retry.json() == {"status": "ok"}


def test_move_to_treatment_404s_for_an_unknown_case(checkpoint_db):
    resp = client.post("/api/case/does-not-exist/move-to-treatment", json={"actor_role": "nurse"})
    assert resp.status_code == 404


def test_move_to_treatment_refuses_a_case_at_the_human_gate(checkpoint_db):
    from app.runner import start_case

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = f"gated-{uuid4().hex[:6]}"
    case["nurse_proposed_acuity"] = 5
    _, pending = start_case(case, thread_id=case["case_id"])
    assert pending and pending.get("gate")

    resp = client.post(f"/api/case/{case['case_id']}/move-to-treatment",
                        json={"actor_role": "nurse"})
    assert resp.status_code == 409


def test_deteriorated_endpoint_re_enters_the_graph_while_waiting(checkpoint_db):
    from app.runner import start_case

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = f"deteriorating-{uuid4().hex[:6]}"
    values, pending = start_case(case, thread_id=case["case_id"])
    assert pending == {"case_id": case["case_id"], "waiting_room": True}

    resp = client.post(
        f"/api/case/{case['case_id']}/deteriorated",
        json={"signal": "spo2 dropped to 88", "actor_role": "nurse"},
    )

    assert resp.status_code == 200
    detail = client.get(f"/api/case/{case['case_id']}").json()
    # A reported deterioration lands the case at the reassessment re-filing
    # pause, waiting for a nurse's fresh observations — it does not replay the
    # stale intake payload. triage-app's tests/test_reassessment.py covers the
    # pause; intake-channel's POST /reassess/{case_id} is what answers it.
    assert detail["view"]["control_state"] == "reassessment_required"
    assert any(
        rec.get("explanation", "").startswith("reassessment timer fired: DETERIORATION_DETECTED")
        for rec in detail["view"]["audit_log"]
    )


def test_deteriorated_endpoint_refuses_a_case_sitting_at_the_human_gate(checkpoint_db):
    from app.runner import start_case

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = f"gated-{uuid4().hex[:6]}"
    case["nurse_proposed_acuity"] = 5   # forces the discrepancy gate, not the queue
    _, pending = start_case(case, thread_id=case["case_id"])
    assert pending and pending.get("gate")   # paused at the human gate, not monitoring

    resp = client.post(
        f"/api/case/{case['case_id']}/deteriorated",
        json={"signal": "x", "actor_role": "nurse"},
    )

    assert resp.status_code == 409


def test_deteriorated_endpoint_404s_for_an_unknown_case(checkpoint_db):
    resp = client.post(
        "/api/case/does-not-exist/deteriorated",
        json={"signal": "x", "actor_role": "nurse"},
    )
    assert resp.status_code == 404


def test_a_refused_resolution_leaves_the_gate_open_on_the_board(checkpoint_db):
    """Picking the unauthorized role on the resolve form must not strand the
    case: the panel keeps offering the form and the card keeps its
    awaiting-approval marker.
    """
    from app.runner import resume_case, start_case

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = f"refused-{uuid4().hex[:6]}"
    case["nurse_proposed_acuity"] = 5
    _, pending = start_case(case, thread_id=case["case_id"])
    assert pending

    resume_case(case["case_id"], {"decision": "use_system_acuity", "resolver_role": "nurse"})

    detail = client.get(f"/api/case/{case['case_id']}").json()
    assert detail["view"]["status"] == "awaiting_human_approval"
    assert any(r["arrow"] == "BLK" for r in detail["view"]["audit_log"])
    card = next(c for c in client.get("/api/board").json()["cards"] if c["case_id"] == case["case_id"])
    assert card["gate_pending"] is True
