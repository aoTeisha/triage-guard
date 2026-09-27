"""Thin client over the CRM stub's GET /patients/{id}.

Maps the CRM's three-outcome fetch (found / not_found / db_error) to a small
result type the UI can render directly, without parsing HTTP status codes in
JS. Mirrors crm-stub's own FetchStatus split (crm/models.py) on the client
side.

Kept separate from `app/crm_client.py` on purpose. That client is the one the
graph's own nodes use, and the board's tests pin it at an unreachable address to
prove the board keeps rendering through a CRM outage. This one serves the intake
form, where a lookup failing is a thing the nurse is shown and continues past
rather than a thing the page survives. Both `not_found` and `db_error` are
non-blocking outcomes; only the caller decides what continuing means.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import quote

import httpx

CRM_BASE_URL = os.environ.get("CRM_BASE_URL", "http://127.0.0.1:8000")

LookupStatus = Literal["found", "not_found", "db_error"]


@dataclass
class PatientLookupResult:
    """Outcome of looking up one patient by the id they carry.

    `record` is populated only when status == "found". For "not_found" and
    "db_error" it is None — these are not failures the UI should treat as
    fatal; both continue on intake-only data per SPECIFICATION.md.
    """

    status: LookupStatus
    record: Optional[dict] = None


def fetch_patient(national_id: str, *, timeout: float = 5.0) -> PatientLookupResult:
    """GET {CRM_BASE_URL}/patients/by-national-id/{id}, mapped to found /
    not_found / db_error.

    The desk only ever has the number the patient carries; the internal
    `stable_patient_id` comes back inside the record.

    Any network-level failure (connection refused, timeout) is also reported
    as db_error — from the nurse's point of view an unreachable CRM and a
    CRM returning 503 look the same: continue without history, flag it.

    `national_id` is percent-encoded before it goes into the path (`safe=""`,
    so even `/` is escaped). Without that, a value containing a slash or a
    `..` segment gets collapsed by ordinary URL normalization and can climb
    out of `/patients/by-national-id/` to a different CRM route entirely.
    """
    url = f"{CRM_BASE_URL}/patients/by-national-id/{quote(national_id, safe='')}"
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError:
        return PatientLookupResult(status="db_error")

    if response.status_code == 200:
        body = response.json()
        return PatientLookupResult(status="found", record=body.get("record"))
    if response.status_code == 404:
        return PatientLookupResult(status="not_found")
    if response.status_code == 503:
        return PatientLookupResult(status="db_error")

    # Unexpected status from the CRM contract — treat conservatively as
    # db_error rather than crashing the intake flow on an unmapped code.
    return PatientLookupResult(status="db_error")
