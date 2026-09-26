"""Field-presence and field-usability guards (docs/SPECIFICATION.md § Guards).

Neither kind infers or guesses a value: an absent field is reported absent, and
an unusable one is reported unusable. Both go back to the nurse.
"""

from __future__ import annotations

from typing import Any

# The mandatory webform fields (SPECIFICATION.md § Context / State variables).
# nurse_proposed_acuity is mandatory and never inferred: absent means MISSING_FIELDS,
# never a guessed value.
REQUIRED_FIELDS = (
    "case_id",
    "channel",
    # The number the patient carries. The internal `stable_patient_id` is never
    # typed: the CRM returns it (SPECIFICATION.md § Identity).
    "national_id",
    "nurse_proposed_acuity",
    "chief_complaint",
    "vitals",
    "free_text",
)

# What a nurse may supply when completing an incomplete intake. Routing
# metadata is fixed by the original submission and never re-supplied.
NURSE_SUPPLIED_FIELDS = frozenset(REQUIRED_FIELDS) - {"case_id", "channel"}

# The subset that carries clinical content. case_id and channel are routing
# metadata — a payload holding only those two carried nothing usable, which is
# demo case 3 (SUBMISSION_FAILED), not a missing-fields round trip.
_CLINICAL_FIELDS = (
    "national_id",
    "nurse_proposed_acuity",
    "chief_complaint",
    "vitals",
    "free_text",
)


# The five ESI levels (ESI Handbook v5). The classifier's proposal is bounded by
# `AcuityProposal`; the nurse's number arrives as raw form data and is bounded here.
ACUITY_LEVELS = range(1, 6)


def is_esi_level(value: Any) -> bool:
    """`type(value) is int` rather than isinstance: bool subclasses int in Python,
    so `True in range(1, 6)` is true, and True is not an acuity.
    """
    return type(value) is int and value in ACUITY_LEVELS


# Either one identifies the patient: the number they carry, or the internal id
# the CRM already returned for them. A re-filed case has only the second, because
# `resolving_identity` drops the first once it has been traded in (I11).
IDENTITY_FIELDS = ("national_id", "stable_patient_id")


def missing_fields(payload: dict) -> list[str]:
    """Which mandatory fields are absent. Presence only — see `unusable_fields`
    for values that are present but cannot be run on.
    """
    absent = [f for f in REQUIRED_FIELDS if payload.get(f) is None]
    if any(payload.get(f) is not None for f in IDENTITY_FIELDS):
        absent = [f for f in absent if f != "national_id"]
    return absent


# The complaint is a code, not a sentence (I12: the model receives fixed-choice
# values or numbers). Kept in sync by hand with `chief_complaints` in
# app/symbolic/policy/privacy.rego; tests/symbolic/test_opa_privacy.py catches drift.
CHIEF_COMPLAINTS = (
    "chest_pain", "shortness_of_breath", "abdominal_pain", "head_injury", "fever",
    "laceration", "limb_injury", "dizziness", "vomiting", "back_pain", "other",
)


def unusable_fields(payload: dict) -> list[str]:
    """Present fields whose value the case cannot proceed on.

    Acuity, because `order_key` is built from it: an acuity of 0 or 7 would file
    the patient at a level that does not exist (I1, I4). The complaint, because
    the model may only see a code from the fixed set (I12) — free text typed here
    would otherwise be refused three nodes later, by the privacy policy.
    """
    unusable = []
    acuity = payload.get("nurse_proposed_acuity")
    if acuity is not None and not is_esi_level(acuity):
        unusable.append("nurse_proposed_acuity")
    complaint = payload.get("chief_complaint")
    if complaint is not None and complaint not in CHIEF_COMPLAINTS:
        unusable.append("chief_complaint")
    return unusable


def nothing_usable(payload: dict) -> bool:
    """True when the submission carried no clinical content at all — only
    routing metadata. Demo case 3.
    """
    return all(payload.get(f) is None for f in _CLINICAL_FIELDS)


def not_required_fields_complete(payload: dict) -> tuple[bool, str]:
    """¬`required_fields_complete` — the guard on MISSING_FIELDS."""
    gaps = missing_fields(payload)
    if not gaps:
        return False, "no required fields are missing"
    return True, f"required fields missing: {', '.join(gaps)}"
