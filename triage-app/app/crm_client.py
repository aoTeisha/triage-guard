"""The one client over the CRM stub's patient endpoints.

Maps the CRM's three-outcome fetch (found / not_found / db_error) to a small
result type the UI can render directly, without parsing HTTP status codes in
JS. Mirrors crm-stub's own FetchStatus split (crm/models.py) on the client
side.

See docs/SPECIFICATION.md — "New patient vs. DB down (both continue on
intake-only data, different logging)" — both not_found and db_error are
non-blocking outcomes; only the caller decides what continuing means.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Optional

import httpx

CRM_BASE_URL = os.environ.get("CRM_BASE_URL", "http://127.0.0.1:8000")

LookupStatus = Literal["found", "not_found", "db_error"]


@dataclass
class PatientLookupResult:
    """Outcome of looking up one patient by stable_patient_id.

    `record` is populated only when status == "found". For "not_found" and
    "db_error" it is None — these are not failures the UI should treat as
    fatal; both continue on intake-only data per SPECIFICATION.md.
    """

    status: LookupStatus
    record: Optional[dict] = None


def fetch_patient_by_national_id(national_id: str, *, timeout: float = 5.0) -> PatientLookupResult:
    """GET {CRM_BASE_URL}/patients/by-national-id/{id} — resolve the number the
    patient carries into their internal one.

    Same three outcomes as `fetch_patient`, and the same fail-open reading of
    them: `not_found` is an unregistered patient, not an error.
    """
    return _lookup(f"{CRM_BASE_URL}/patients/by-national-id/{national_id}", timeout)


def fetch_patient(stable_patient_id: str, *, timeout: float = 5.0) -> PatientLookupResult:
    """GET {CRM_BASE_URL}/patients/{id}, mapped to found / not_found / db_error.

    Any network-level failure (connection refused, timeout) is also reported
    as db_error — from the nurse's point of view an unreachable CRM and a
    CRM returning 503 look the same: continue without history, flag it.
    """
    return _lookup(f"{CRM_BASE_URL}/patients/{stable_patient_id}", timeout)


def _lookup(url: str, timeout: float) -> PatientLookupResult:
    """One mapping from HTTP to the three CRM outcomes, shared by both lookups."""
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError:
        return PatientLookupResult(status="db_error")

    if response.status_code == 200:
        return PatientLookupResult(status="found", record=response.json().get("record"))
    if response.status_code == 404:
        return PatientLookupResult(status="not_found")
    # 503, and any status outside the CRM contract: conservatively db_error
    # rather than crashing the intake flow on an unmapped code.
    return PatientLookupResult(status="db_error")


def visit_record(values: dict[str, Any]) -> dict[str, Any]:
    """What one visit leaves in the CRM (I17): when, how acute, and the
    complaint code as words. No identifiers — they never left the CRM — and no
    prose. Shared by the release step (the first attempt) and the sweeper's
    retry, so the two can never write different stories.
    """
    released = values.get("released_at") or values.get("arrival_time") or ""
    complaint = (values.get("redacted_payload") or {}).get("chief_complaint") or ""
    return {"date": str(released)[:10],
            "acuity": values.get("acuity"),
            "notes": str(complaint).replace("_", " ")}


def patch_patient(
    stable_patient_id: str, visit: dict, *, timeout: float = 5.0
) -> Literal["ok", "db_error"]:
    """PATCH {CRM_BASE_URL}/patients/{id} — this visit's write-back.

    Never raises. A write-back failure is flagged and deferred, never fatal:
    the triage decision is already made and must not be undone by a dead DB.
    """
    url = f"{CRM_BASE_URL}/patients/{stable_patient_id}"
    try:
        response = httpx.patch(url, json=visit, timeout=timeout)
    except httpx.HTTPError:
        return "db_error"
    return "ok" if response.status_code == 200 else "db_error"
