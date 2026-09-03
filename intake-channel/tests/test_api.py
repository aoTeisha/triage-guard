"""Tests for channel.api — the HTTP layer wiring lookup and submit together."""

import httpx
import respx
from fastapi.testclient import TestClient

import channel.api as api_module
from channel.patient_lookup import CRM_BASE_URL

client = TestClient(api_module.app)


@respx.mock
def test_lookup_found_returns_200_with_status_found():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(
            200,
            json={"status": "found", "record": {"stable_patient_id": "P-1005", "name": "David Friedman"}},
        )
    )

    r = client.get("/lookup/P-1005")

    assert r.status_code == 200
    assert r.json()["status"] == "found"
    assert r.json()["record"]["name"] == "David Friedman"


@respx.mock
def test_lookup_not_found_still_returns_200():
    """not_found is a normal outcome, not an HTTP error — the caller must
    not have to branch on status codes to render it."""
    respx.get(f"{CRM_BASE_URL}/patients/P-9999").mock(
        return_value=httpx.Response(404, json={"detail": "not_found"})
    )

    r = client.get("/lookup/P-9999")

    assert r.status_code == 200
    assert r.json()["status"] == "not_found"
    assert r.json()["record"] is None


@respx.mock
def test_lookup_db_error_still_returns_200():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(503, json={"detail": "db_error"})
    )

    r = client.get("/lookup/P-1005")

    assert r.status_code == 200
    assert r.json()["status"] == "db_error"


@respx.mock
def test_submit_clean_returns_data_parsed_with_real_payload():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(200, json={"status": "found", "record": {}})
    )

    r = client.post("/submit", json={"stable_patient_id": "P-1005", "submission_type": "clean"})

    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "DATA_PARSED"
    assert body["parsed_fields"]["stable_patient_id"] == "P-1005"
    assert body["case_id"]


@respx.mock
def test_submit_missing_returns_missing_fields():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(200, json={"status": "found", "record": {}})
    )

    r = client.post("/submit", json={"stable_patient_id": "P-1005", "submission_type": "missing"})

    body = r.json()
    assert body["outcome"] == "MISSING_FIELDS_DETECTED"
    assert "nurse_proposed_acuity" in body["missing_fields"]


@respx.mock
def test_submit_failed_returns_submission_failed():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(200, json={"status": "found", "record": {}})
    )

    r = client.post("/submit", json={"stable_patient_id": "P-1005", "submission_type": "failed"})

    assert r.json()["outcome"] == "SUBMISSION_FAILED"


@respx.mock
def test_submit_injection_returns_invalid_input_with_injection_reason():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(200, json={"status": "found", "record": {}})
    )

    r = client.post("/submit", json={"stable_patient_id": "P-1005", "submission_type": "injection"})

    body = r.json()
    assert body["outcome"] == "INVALID_INPUT_DETECTED"
    assert body["reason"] == "injection"


@respx.mock
def test_submit_works_even_when_crm_lookup_is_db_error():
    """Submission must not be blocked by a CRM outage — only a missing
    stable_patient_id blocks (enforced client-side; the API still accepts
    a submission built around a db_error lookup)."""
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(503, json={"detail": "db_error"})
    )

    r = client.post("/submit", json={"stable_patient_id": "P-1005", "submission_type": "clean"})

    assert r.status_code == 200
    assert r.json()["outcome"] == "DATA_PARSED"


@respx.mock
def test_submit_case_id_unique_across_calls():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(200, json={"status": "found", "record": {}})
    )

    r1 = client.post("/submit", json={"stable_patient_id": "P-1005", "submission_type": "clean"})
    r2 = client.post("/submit", json={"stable_patient_id": "P-1005", "submission_type": "clean"})

    assert r1.json()["case_id"] != r2.json()["case_id"]
