"""Triage Guard CRM stub — a local SQLite implementation of the CRM contract."""

from .models import (
    FetchResult,
    FetchStatus,
    PatchResult,
    PatchStatus,
    PatientRecord,
    PriorVisit,
)
from .repository import CRMRepository

__all__ = [
    "CRMRepository",
    "PatientRecord",
    "PriorVisit",
    "FetchResult",
    "FetchStatus",
    "PatchResult",
    "PatchStatus",
]
