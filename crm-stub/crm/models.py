"""Data models for the CRM stub.

These mirror the contract in SPECIFICATION.md ("Local CRM stub (SQLite)"):
a patient record, and the three-outcome result of a fetch (found / not_found /
db_error). Identifier-class fields (name, date_of_birth) live here on the CRM
side; the caller is responsible for dropping them before building any
model-facing payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class PriorVisit:
    """One past visit, as stored in the patient's history."""

    date: str          # ISO date, e.g. "2025-11-02"
    acuity: int        # ESI-style 1..5 recorded at that visit
    notes: str = ""


@dataclass
class PatientRecord:
    """A patient record as returned to the Orchestrator.

    `name` and `date_of_birth` are identifier-class: they stay on the CRM /
    Orchestrator side under actor_authorized and must be dropped before the
    model-facing payload is built.
    """

    stable_patient_id: str
    name: str
    date_of_birth: str                     # ISO date
    known_conditions: list[str] = field(default_factory=list)
    prior_visits: list[PriorVisit] = field(default_factory=list)
    last_updated: Optional[str] = None      # ISO timestamp, set on write-back


class FetchStatus(str, Enum):
    """Outcome of a fetch, mapping onto the spec's db guards.

    FOUND and NOT_FOUND both mean db_reachable is true (a new patient with no
    record is a normal empty, not a failure). DB_ERROR means the DB is
    unreachable and triggers the CRM fail-open degrade path.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    DB_ERROR = "db_error"


@dataclass
class FetchResult:
    """Result of fetch_patient_data: a status plus the record when FOUND."""

    status: FetchStatus
    record: Optional[PatientRecord] = None

    @property
    def db_reachable(self) -> bool:
        """True unless the DB itself was unreachable (maps to spec guard)."""
        return self.status is not FetchStatus.DB_ERROR


class PatchStatus(str, Enum):
    """Outcome of a write-back."""

    OK = "ok"
    DB_ERROR = "db_error"


@dataclass
class PatchResult:
    status: PatchStatus

    @property
    def ok(self) -> bool:
        return self.status is PatchStatus.OK
