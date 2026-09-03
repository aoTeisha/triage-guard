"""Tests for channel.patient_lookup — the 200/404/503 to found/not_found/db_error mapping.

Uses respx to mock the CRM stub over HTTP rather than requiring a live CRM
process, so this suite runs standalone.
"""

import httpx
import respx

from channel.patient_lookup import CRM_BASE_URL, fetch_patient


@respx.mock
def test_found_returns_record():
    respx.get(f"{CRM_BASE_URL}/patients/P-1001").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "found",
                "record": {
                    "stable_patient_id": "P-1001",
                    "name": "Alon Mizrahi",
                    "date_of_birth": "1958-03-12",
                },
            },
        )
    )

    result = fetch_patient("P-1001")

    assert result.status == "found"
    assert result.record["name"] == "Alon Mizrahi"


@respx.mock
def test_not_found_returns_no_record():
    respx.get(f"{CRM_BASE_URL}/patients/P-9999").mock(
        return_value=httpx.Response(404, json={"detail": "not_found"})
    )

    result = fetch_patient("P-9999")

    assert result.status == "not_found"
    assert result.record is None


@respx.mock
def test_db_error_returns_no_record():
    respx.get(f"{CRM_BASE_URL}/patients/P-1001").mock(
        return_value=httpx.Response(503, json={"detail": "db_error"})
    )

    result = fetch_patient("P-1001")

    assert result.status == "db_error"
    assert result.record is None


@respx.mock
def test_network_failure_maps_to_db_error():
    """An unreachable CRM looks the same to the nurse as a 503: continue
    without history, flag it. See SPECIFICATION.md's "New patient vs. DB
    down" note — both are non-blocking, but this is specifically the
    db_error branch, not not_found.
    """
    respx.get(f"{CRM_BASE_URL}/patients/P-1001").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    result = fetch_patient("P-1001")

    assert result.status == "db_error"
    assert result.record is None


@respx.mock
def test_unexpected_status_is_conservative_db_error():
    respx.get(f"{CRM_BASE_URL}/patients/P-1001").mock(
        return_value=httpx.Response(500, json={"detail": "unexpected"})
    )

    result = fetch_patient("P-1001")

    assert result.status == "db_error"
