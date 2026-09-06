"""The client is the only thing that turns CRM HTTP codes into the spec's
three outcomes. 503 and a dead socket must land on the same branch — the
fail-open degrade path reads one value, not two.
"""

import httpx
import respx

from app.crm_client import CRM_BASE_URL, fetch_patient, patch_patient


@respx.mock
def test_200_is_found_and_carries_the_record():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(200, json={"status": "found", "record": {"name": "David Friedman"}})
    )
    result = fetch_patient("P-1005")
    assert result.status == "found"
    assert result.record == {"name": "David Friedman"}


@respx.mock
def test_404_is_not_found_with_no_record():
    respx.get(f"{CRM_BASE_URL}/patients/P-9999").mock(
        return_value=httpx.Response(404, json={"detail": "not_found"})
    )
    result = fetch_patient("P-9999")
    assert result.status == "not_found"
    assert result.record is None


@respx.mock
def test_503_and_an_unreachable_crm_are_the_same_outcome():
    respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(503, json={"detail": "db_error"})
    )
    assert fetch_patient("P-1005").status == "db_error"

    respx.get(f"{CRM_BASE_URL}/patients/P-1006").mock(side_effect=httpx.ConnectError("refused"))
    assert fetch_patient("P-1006").status == "db_error"


@respx.mock
def test_patch_returns_ok_on_200_and_db_error_on_503():
    respx.patch(f"{CRM_BASE_URL}/patients/P-1005").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert patch_patient("P-1005", {"new_visit": {"date": "2026-09-06", "acuity": 2, "notes": "x"}}) == "ok"

    respx.patch(f"{CRM_BASE_URL}/patients/P-1006").mock(return_value=httpx.Response(503, json={"detail": "db_error"}))
    assert patch_patient("P-1006", {"new_visit": {}}) == "db_error"
