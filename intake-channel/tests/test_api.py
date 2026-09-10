"""Tests for channel.api — the HTTP layer over the real graph.

`/submit` used to return a canned dict from `MOCK_PARSE_RESULTS`. It now runs
the case through `app.graph`, so these tests exercise the control plane through
the same door a nurse uses, including the pause at the acuity gate.
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import channel.api as api_module
from channel.patient_lookup import CRM_BASE_URL

client = TestClient(api_module.app)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Mock actors, no tracing, and a throwaway checkpoint file per test."""
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from app import runner
    from app.graph import build_graph

    conn = sqlite3.connect(str(tmp_path / "ckpt.db"), check_same_thread=False)
    compiled = build_graph(checkpointer=SqliteSaver(conn))
    monkeypatch.setattr(runner, "graph", lambda: compiled)
    yield
    conn.close()


def _crm(status: int = 200, body: dict | None = None):
    return respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(
            status, json=body or {"status": "found", "record": {"stable_patient_id": "P-1005"}}
        )
    )


def _submit(kind: str, patient: str = "P-1005"):
    return client.post(
        "/submit", json={"stable_patient_id": patient, "submission_type": kind}
    ).json()


# ---- lookup (unchanged behaviour) -------------------------------------------


@respx.mock
def test_lookup_found_returns_200_with_status_found():
    _crm(200, {"status": "found", "record": {"stable_patient_id": "P-1005", "name": "David Friedman"}})

    r = client.get("/lookup/P-1005")

    assert r.status_code == 200
    assert r.json()["status"] == "found"
    assert r.json()["record"]["name"] == "David Friedman"


@respx.mock
def test_lookup_not_found_still_returns_200():
    """not_found is a normal outcome, not an HTTP error — the caller must not have
    to branch on status codes to render it.
    """
    respx.get(f"{CRM_BASE_URL}/patients/P-9999").mock(
        return_value=httpx.Response(404, json={"detail": "not_found"})
    )

    r = client.get("/lookup/P-9999")

    assert r.status_code == 200
    assert r.json()["status"] == "not_found"
    assert r.json()["record"] is None


@respx.mock
def test_lookup_db_error_still_returns_200():
    _crm(503, {"detail": "db_error"})

    r = client.get("/lookup/P-1005")

    assert r.json()["status"] == "db_error"


# ---- submit now drives the graph --------------------------------------------


@respx.mock
def test_a_clean_submission_runs_the_whole_pipeline():
    _crm()

    body = _submit("clean")

    assert body["outcome"] == "DATA_PARSED"
    assert body["control_state"] == "monitoring"
    assert body["status"] == "settled"
    assert body["safety_passed"] is True
    assert body["acuity"] is not None


@respx.mock
def test_a_submission_returns_the_real_audit_trail():
    """The UI shows the spec's arrows, not a canned response."""
    _crm()

    body = _submit("clean")
    arrows = [r["arrow"] for r in body["audit_log"] if r["arrow"]]

    assert arrows[0] == "1a"
    assert arrows[-1] == "11·pass"


@respx.mock
def test_missing_fields_stops_and_names_the_gaps():
    _crm()

    body = _submit("missing")

    assert body["outcome"] == "MISSING_FIELDS_DETECTED"
    assert body["control_state"] == "missing_fields_requested"
    assert "nurse_proposed_acuity" in body["missing_fields"]


@respx.mock
def test_an_unusable_submission_stops():
    _crm()

    body = _submit("failed")

    assert body["outcome"] == "SUBMISSION_FAILED"
    assert body["control_state"] == "submission_failed"


@respx.mock
def test_injection_is_rejected_and_never_classified():
    _crm()

    body = _submit("injection")

    assert body["outcome"] == "INVALID_INPUT_DETECTED"
    assert body["reason"].startswith("injection")
    assert body["acuity"] is None


@respx.mock
def test_submission_survives_a_crm_outage():
    """Fail-open: a CRM outage degrades the case, it does not block intake."""
    _crm(503, {"detail": "db_error"})

    body = _submit("clean")

    assert body["outcome"] == "DATA_PARSED"
    assert "crm" in body["degraded"]


@respx.mock
def test_case_ids_are_unique_across_submissions():
    _crm()

    assert _submit("clean")["case_id"] != _submit("clean")["case_id"]


# ---- the human gate, through the UI's endpoints ------------------------------


@respx.mock
def test_a_major_acuity_gap_pauses_and_surfaces_the_gate():
    _crm()

    body = _submit("gap")

    assert body["status"] == "awaiting_human_approval"
    assert body["gate"]["gate"] == "discrepancy"
    assert body["gate"]["required_role"] == "charge_nurse"
    assert body["acuity"] is None


@respx.mock
def test_a_charge_nurse_can_resolve_the_gate_and_the_case_completes():
    _crm()
    paused = _submit("gap")

    resumed = client.post(
        f"/resume/{paused['case_id']}",
        json={"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
    ).json()

    assert resumed["status"] == "settled"
    assert resumed["control_state"] == "monitoring"
    assert resumed["acuity_source"] == "human_confirmed"


@respx.mock
def test_an_unauthorized_resolver_is_refused_through_the_api():
    """BLK reaches the browser as a refusal, not as a silent success."""
    _crm()
    paused = _submit("gap")

    denied = client.post(
        f"/resume/{paused['case_id']}",
        json={"decision": "use_system_acuity", "resolver_role": "nurse"},
    ).json()

    assert denied["control_state"] == "action_denied"
    assert denied["acuity"] is None
    assert any(r["arrow"] == "BLK" for r in denied["audit_log"])


@respx.mock
def test_a_case_can_be_read_back_after_the_request_that_created_it():
    """State is checkpointed, so a later GET sees the same case."""
    _crm()
    submitted = _submit("clean")

    fetched = client.get(f"/case/{submitted['case_id']}").json()

    assert fetched["case_id"] == submitted["case_id"]
    assert fetched["control_state"] == "monitoring"


def test_reading_an_unknown_case_is_a_404():
    assert client.get("/case/case-does-not-exist").status_code == 404
