"""FastAPI wrapper over the CRM repository.

Exposes the same three contract operations as HTTP endpoints so the
Orchestrator can call the CRM over the network. The db-error simulation is
driven by the CRM_SIMULATE_DOWN env flag (read per request inside the
repository), and can also be toggled at runtime via /admin/simulate-down for
tests and demos.

Run:
    uv run crm                 # seeds the DB if missing, then serves
    uvicorn crm.api:app --reload
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .models import FetchStatus, PatchStatus
from .repository import CRMRepository

DB_PATH = os.environ.get("CRM_DB_PATH", "patients.db")

app = FastAPI(title="Triage Guard — CRM stub", version="1.0.0")
repo = CRMRepository(db_path=DB_PATH)


# -- request/response schemas --------------------------------------------

class VisitData(BaseModel):
    name: Optional[str] = None
    date_of_birth: Optional[str] = None
    known_conditions: Optional[list[str]] = None
    new_visit: Optional[dict] = None  # {date, acuity, notes}


# -- endpoints ------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness of the CRM itself (maps to is_available)."""
    return {"available": repo.is_available()}


@app.get("/patients/{stable_patient_id}")
def get_patient(stable_patient_id: str):
    """fetch_patient_data: found -> record, not_found -> 404, db_error -> 503.

    503 lets the caller distinguish an unreachable DB (fail-open degrade) from
    a genuine 404 (new patient, normal empty).
    """
    result = repo.fetch_patient_data(stable_patient_id)
    if result.status is FetchStatus.DB_ERROR:
        raise HTTPException(status_code=503, detail="db_error")
    if result.status is FetchStatus.NOT_FOUND:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "found", "record": asdict(result.record)}


@app.patch("/patients/{stable_patient_id}")
def patch_patient(stable_patient_id: str, visit: VisitData):
    """patch_patient_data write-back: ok -> 200, db_error -> 503."""
    result = repo.patch_patient_data(
        stable_patient_id, visit.model_dump(exclude_none=True)
    )
    if result.status is PatchStatus.DB_ERROR:
        raise HTTPException(status_code=503, detail="db_error")
    return {"status": "ok"}


@app.post("/admin/simulate-down")
def set_simulate_down(enabled: bool):
    """Toggle the db-error simulation at runtime (test/demo helper).

    Mirrors the CRM_SIMULATE_DOWN env flag; setting it here overrides the env
    for this process so the fail-open path can be exercised on demand.
    """
    repo._simulate_down = enabled  # explicit override for this process
    return {"simulate_down": enabled}


# -- entry point ----------------------------------------------------------

def run() -> None:
    """`uv run crm` — seed the DB on first run, then serve the API.

    Host/port come from CRM_HOST / CRM_PORT so the same entry point works
    locally and in the container.
    """
    import sqlite3

    import uvicorn

    from .repository import SCHEMA
    from .seed import seed

    # Seed when the table is empty, not when the file is missing: constructing
    # the repository above already created an empty DB file.
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 0
    finally:
        conn.close()
    if empty:
        print(f"{DB_PATH} is empty — seeding mock patients...", flush=True)
        print(f"seeded {seed(DB_PATH)} patients", flush=True)

    uvicorn.run(
        app,
        host=os.environ.get("CRM_HOST", "127.0.0.1"),
        port=int(os.environ.get("CRM_PORT", "8000")),
    )
