"""Builds the four mock intake payloads used to demo the Intake Parser's four
outcomes (SPECIFICATION.md — "Document intake & demo data").

The nurse always supplies national_id by typing it in — the lookup
result only enriches what's shown (name, DOB if found); it never decides the
submission's identity. A case is built the same way whether the lookup was
found, not_found, or db_error — SPECIFICATION.md's continue-on-intake-only-
data rule applies uniformly here.
"""

from __future__ import annotations

import uuid
from typing import Literal

from app.budgets import MAX_CORRECTION_ROUNDS
from app.deterministic import audit
from app.labels import Transition as T
from app.states import State

from .patient_lookup import PatientLookupResult

SubmissionType = Literal["clean", "missing", "failed", "gap",
                          "trace_violation", "safety_fail"]


def new_case_id() -> str:
    """A short unique id per submission so repeated demo runs don't collide
    in Langfuse (each run needs its own span, not an overwrite of the last).
    """
    return f"case-{uuid.uuid4().hex[:8]}"


def build_case(
    lookup_result: PatientLookupResult,
    national_id: str,
    submission_type: SubmissionType,
) -> dict:
    """Build one of the mock intake payloads.

    lookup_result is not required to be "found" — not_found and db_error
    still produce a valid payload; they just carry no prior-visit context.
    """
    case_id = new_case_id()

    # A trace-violation demo starts as a clean case; its planted records are
    # added afterwards, by `planted_records`. A safety_fail demo also starts
    # clean: it's board/board/intake.py that makes its safety verdict fail,
    # not this payload.
    if submission_type in ("clean", "trace_violation", "safety_fail"):
        return {
            "case_id": case_id,
            "channel": "website",
            "national_id": national_id,
            "nurse_proposed_acuity": 3,
            "chief_complaint": "chest_pain",
            "vitals": {"hr": 104, "bp": "148/92", "spo2": 95, "temp_c": 37.1},
            "free_text": "Patient reports pressure in the chest, worse on exertion.",
        }

    if submission_type == "missing":
        # Deliberately omits nurse_proposed_acuity and vitals — the system
        # must never infer acuity itself; a missing value is always routed
        # to MISSING_FIELDS_DETECTED, never guessed.
        return {
            "case_id": case_id,
            "channel": "website",
            "national_id": national_id,
            "chief_complaint": "chest_pain",
            "free_text": "Patient reports pressure in the chest, worse on exertion.",
        }

    if submission_type == "failed":
        # Nothing usable received — only routing metadata, no clinical
        # fields at all.
        return {
            "case_id": case_id,
            "channel": "website",
        }

    if submission_type == "gap":
        # Nurse says 5 (non-urgent); the classifier reads crushing chest pain and
        # proposes 2. Gap of 3 -> ACUITY_GAP_MAJOR -> the case pauses for a charge nurse. The
        # only submission type that exercises the human gate from the UI.
        return {
            "case_id": case_id,
            "channel": "website",
            "national_id": national_id,
            "nurse_proposed_acuity": 5,
            "chief_complaint": "chest_pain",
            "vitals": {"hr": 122, "bp": "162/98", "spo2": 94, "temp_c": 37.0},
            "free_text": "Sudden crushing chest pain while climbing stairs.",
        }

    raise ValueError(f"unknown submission_type: {submission_type!r}")


# ---- trace-violation demo -------------------------------------------------
# Records planted into a clean case's audit log after it has been queued, each
# the smallest sequence that breaks one rule of the after-run trace check
# (`app.verification.check_trace`) and no other. The guards would never write
# any of these; the demo exists to show the check catching them. Keys are the
# checker's rule names, snake_cased.

PLANTED = "planted_by_demo"

TRACE_VIOLATIONS: dict[str, tuple[T, ...]] = {
    # re-filed, then queued with no safety check in the new triage
    "no_bypass": (T.FRONT_DOOR_RERUN, T.CLEARED_TO_QUEUE),
    "single_treatment_start": (T.MOVE_CONFIRMED, T.MOVE_CONFIRMED),
    # a human answered the acuity question, so the only thing missing is the
    # correction of the safety failure itself
    "correct_then_revalidate": (T.FRONT_DOOR_RERUN, T.ACUITY_GAP_MAJOR, T.GATE_ACUITY_RESOLVED,
                                T.SAFETY_FAILED, T.SAFETY_PASSED, T.CLEARED_TO_QUEUE),
    "bounded_correction_loop": (T.FRONT_DOOR_RERUN, T.SAFETY_FAILED,
                                *(T.GATE_SAFETY_CORRECTED, T.SAFETY_FAILED) * (MAX_CORRECTION_ROUNDS + 1)),
    # one ordinary record, but filed under another case
    "audit_record": (T.TIMER_RUNNING,),
    "closed_case": (T.RELEASE, T.MOVE_CONFIRMED),
    # the model asked after the privacy check refused this triage's payload
    # (the planted refusal also names no layer: the same rule)
    "privacy_refusal": (T.FRONT_DOOR_RERUN, T.PRIVACY_REFUSED, T.RUN_CLASSIFIER),
}


def planted_records(case_id: str, rule: str) -> list[dict]:
    """The audit records that break `rule`, marked as planted so the trail
    shows plainly they are not the system's own.
    """
    owner = "case-someone-else" if rule == "audit_record" else case_id
    return [
        audit(owner, State.MONITORING, PLANTED, f"planted by the trace-violation demo: {rule}", t)
        for t in TRACE_VIOLATIONS[rule]
    ]
