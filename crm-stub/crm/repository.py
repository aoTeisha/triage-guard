"""Postgres-backed CRM repository.

Implements the whole CRM contract from SPECIFICATION.md:
fetch_patient_data / patch_patient_data / is_available, with a
three-outcome fetch (found / not_found / db_error).

The `db-error` outcome is driven by the CRM_SIMULATE_DOWN env flag (or the
`simulate_down` constructor arg), so the fail-open degrade path can be tested
end-to-end rather than only described.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg
import psycopg.rows

from .models import (
    FetchResult,
    FetchStatus,
    PatchResult,
    PatchStatus,
    PatientRecord,
    PriorVisit,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    stable_patient_id TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    date_of_birth     TEXT NOT NULL,
    known_conditions  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    prior_visits      TEXT NOT NULL DEFAULT '[]',   -- JSON array
    last_updated      TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class DBUnavailable(Exception):
    """Raised internally when the DB is simulated down; never leaks to callers."""


class CRMRepository:
    """Postgres implementation of the CRM contract.

    Parameters
    ----------
    db_path:
        Postgres DSN (kept the name `db_path` for the smallest diff against
        callers — it hasn't been a filesystem path since the Postgres swap).
    simulate_down:
        Force every operation to report db_error. If None, falls back to the
        CRM_SIMULATE_DOWN environment flag, re-read on each call so it can be
        toggled at runtime in tests.
    """

    def __init__(self, db_path: str = "postgresql://triage:triage@localhost:5434/crm",
                 simulate_down: Optional[bool] = None):
        self.db_path = db_path
        self._simulate_down = simulate_down
        self._init_schema()

    # -- internal helpers -------------------------------------------------

    def _down(self) -> bool:
        if self._simulate_down is not None:
            return self._simulate_down
        return _env_flag("CRM_SIMULATE_DOWN")

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.db_path, row_factory=psycopg.rows.dict_row)

    def _init_schema(self) -> None:
        # Schema creation itself is not gated by simulate_down; the switch only
        # models a runtime outage of reads/writes, not a missing database.
        conn = self._connect()
        try:
            conn.execute(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: dict) -> PatientRecord:
        return PatientRecord(
            stable_patient_id=row["stable_patient_id"],
            name=row["name"],
            date_of_birth=row["date_of_birth"],
            known_conditions=json.loads(row["known_conditions"]),
            prior_visits=[PriorVisit(**v) for v in json.loads(row["prior_visits"])],
            last_updated=row["last_updated"],
        )

    # -- contract operations ---------------------------------------------

    def fetch_patient_data(self, stable_patient_id: str) -> FetchResult:
        """Look up a patient by stable id.

        Returns FOUND(record) / NOT_FOUND / DB_ERROR. NOT_FOUND is a normal
        empty (new patient), not a failure; only DB_ERROR trips fail-open.
        """
        if self._down():
            return FetchResult(FetchStatus.DB_ERROR)
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM patients WHERE stable_patient_id = %s",
                    (stable_patient_id,),
                )
                row = cur.fetchone()
            finally:
                conn.close()
        except psycopg.Error:
            return FetchResult(FetchStatus.DB_ERROR)

        if row is None:
            return FetchResult(FetchStatus.NOT_FOUND)
        return FetchResult(FetchStatus.FOUND, self._row_to_record(row))

    def patch_patient_data(self, stable_patient_id: str, visit_data: dict) -> PatchResult:
        """Write this visit back into the patient's history.

        `visit_data` may carry:
          - "new_visit": {date, acuity, notes}  -> appended to prior_visits
          - "known_conditions": [...]           -> replaces the conditions list
        Upserts the row; sets last_updated. Returns OK / DB_ERROR.
        """
        if self._down():
            return PatchResult(PatchStatus.DB_ERROR)
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT * FROM patients WHERE stable_patient_id = %s",
                    (stable_patient_id,),
                )
                row = cur.fetchone()

                if row is None:
                    conditions = visit_data.get("known_conditions", [])
                    visits = []
                    if visit_data.get("new_visit"):
                        visits.append(visit_data["new_visit"])
                    conn.execute(
                        """INSERT INTO patients
                           (stable_patient_id, name, date_of_birth,
                            known_conditions, prior_visits, last_updated)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (
                            stable_patient_id,
                            visit_data.get("name", "UNKNOWN"),
                            visit_data.get("date_of_birth", "UNKNOWN"),
                            json.dumps(conditions),
                            json.dumps(visits),
                            _now_iso(),
                        ),
                    )
                else:
                    conditions = json.loads(row["known_conditions"])
                    visits = json.loads(row["prior_visits"])
                    if "known_conditions" in visit_data:
                        conditions = visit_data["known_conditions"]
                    if visit_data.get("new_visit"):
                        visits.append(visit_data["new_visit"])
                    conn.execute(
                        """UPDATE patients
                           SET known_conditions = %s, prior_visits = %s, last_updated = %s
                           WHERE stable_patient_id = %s""",
                        (
                            json.dumps(conditions),
                            json.dumps(visits),
                            _now_iso(),
                            stable_patient_id,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
        except psycopg.Error:
            return PatchResult(PatchStatus.DB_ERROR)

        return PatchResult(PatchStatus.OK)

    def is_available(self) -> bool:
        """Health check used for degrade decisions. False when down/unreachable."""
        if self._down():
            return False
        try:
            conn = self._connect()
            try:
                conn.execute("SELECT 1")
            finally:
                conn.close()
        except psycopg.Error:
            return False
        return True
