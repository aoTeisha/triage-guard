"""Field-presence and field-usability guards (docs/SPECIFICATION.md § Guards).

Neither kind infers or guesses a value: an absent field is reported absent, and
an unusable one is reported unusable. Both go back to the nurse.
"""

from __future__ import annotations

import math
import re
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
)
# `free_text` is not required: the model never reads it, and the real-case
# form is structured only.

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


# The vital signs the model may see, and the shape of each: numbers, and blood
# pressure as "120/80". Kept in sync by hand with `vital_fields` and `bp_pattern`
# in app/symbolic/policy/privacy.rego; tests/symbolic/test_opa_privacy.py catches drift.
VITAL_FIELDS = ("hr", "rr", "bp", "spo2", "temp_c")
BP_PATTERN = r"^[0-9]{2,3}/[0-9]{2,3}$"

# Could this be a real reading at all? Inclusive (low, high) per sign. These are
# believability limits, not normal ranges: they catch a phone number typed into
# heart rate or 98.6 typed into Celsius, and must never refuse a critically ill
# patient's real value, because a refused vital waits for the nurse to fix it and
# a true reading has no fix. Every ESI v5 danger-zone limit (app/esi.py) and every
# vital in the handbook's examples sits well inside them; rr reaches 120 for
# newborns in distress, temp_c 20 for accidental hypothermia, the diastolic 10 for
# shock. Working values pending clinical sign-off, the same status as the
# thresholds in app/budgets.py. Not mirrored in privacy.rego: the policy checks
# the payload's privacy shape, not its clinical range.
VITAL_BOUNDS: dict[str, tuple[float, float]] = {
    "hr": (20, 300),
    "rr": (2, 120),
    "spo2": (30, 100),
    "temp_c": (20, 46),
    "bp_systolic": (40, 300),
    "bp_diastolic": (10, 200),
}


def _is_number(value: Any) -> bool:
    # bool is not a reading, though Python counts it an int.
    return type(value) in (int, float) and math.isfinite(value)


def _within(value: float, sign: str) -> bool:
    low, high = VITAL_BOUNDS[sign]
    return low <= value <= high


def vitals_usable(vitals: Any) -> bool:
    """The vitals have the shape the privacy policy allows — only the named signs,
    each a number or blood pressure as "120/80" — and each is a believable reading
    (`VITAL_BOUNDS`, systolic above diastolic). `None` is an absent reading, as it
    is to the policy. `fullmatch`, not `match`: Python's `$` also matches before a
    trailing newline, and RE2's does not.
    """
    if not isinstance(vitals, dict):
        return False
    for key, value in vitals.items():
        if key not in VITAL_FIELDS:
            return False
        if value is None:
            continue
        if key == "bp":
            if not (isinstance(value, str) and re.fullmatch(BP_PATTERN, value)):
                return False
            systolic, diastolic = (int(part) for part in value.split("/"))
            if not (_within(systolic, "bp_systolic") and _within(diastolic, "bp_diastolic")
                    and systolic > diastolic):
                return False
        elif not (_is_number(value) and _within(value, key)):
            return False
    return True


def unusable_fields(payload: dict) -> list[str]:
    """Present fields whose value the case cannot proceed on.

    Acuity, because `order_key` is built from it: an acuity of 0 or 7 would file
    the patient at a level that does not exist (I1, I4). The complaint and the
    vitals, because the model may only see a code from the fixed set and numbers
    (I12) — anything else typed there would otherwise be refused three nodes
    later, by the privacy policy, and halt the case.
    """
    unusable = []
    acuity = payload.get("nurse_proposed_acuity")
    if acuity is not None and not is_esi_level(acuity):
        unusable.append("nurse_proposed_acuity")
    complaint = payload.get("chief_complaint")
    if complaint is not None and complaint not in CHIEF_COMPLAINTS:
        unusable.append("chief_complaint")
    vitals = payload.get("vitals")
    if vitals is not None and not vitals_usable(vitals):
        unusable.append("vitals")
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
