"""intake-channel — standalone nurse-facing intake UI for Triage Guard.

Stands in for the real website intake form. Talks to the CRM stub over HTTP
for patient lookups and to the crewAI Intake Parser task for validation. Not
part of the crew itself — see README.md for the full boundary.
"""

from .mock_cases import SubmissionType, build_case
from .patient_lookup import PatientLookupResult, fetch_patient

__all__ = [
    "fetch_patient",
    "PatientLookupResult",
    "build_case",
    "SubmissionType",
]
