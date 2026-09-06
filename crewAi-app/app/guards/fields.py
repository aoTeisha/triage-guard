"""Field-presence guards (docs/SPECIFICATION.md § Guards).

Presence checks only — none of these infer or guess a missing value.
"""

from __future__ import annotations

# The mandatory webform fields (SPECIFICATION.md § Context / State variables).
# nurse_proposed_acuity is mandatory and never inferred: absent means arrow 16,
# never a guessed value.
REQUIRED_FIELDS = (
    "case_id",
    "channel",
    "stable_patient_id",
    "nurse_proposed_acuity",
    "chief_complaint",
    "vitals",
    "free_text",
)

# The subset that carries clinical content. case_id and channel are routing
# metadata — a payload holding only those two carried nothing usable, which is
# demo case 3 (SUBMISSION_FAILED), not a missing-fields round trip.
_CLINICAL_FIELDS = (
    "stable_patient_id",
    "nurse_proposed_acuity",
    "chief_complaint",
    "vitals",
    "free_text",
)


def missing_fields(payload: dict) -> list[str]:
    """Which mandatory fields are absent. Presence, not meaning — a present
    but clinically nonsensical value is not this function's problem.
    """
    return [f for f in REQUIRED_FIELDS if payload.get(f) is None]


def nothing_usable(payload: dict) -> bool:
    """True when the submission carried no clinical content at all — only
    routing metadata. Demo case 3.
    """
    return all(payload.get(f) is None for f in _CLINICAL_FIELDS)


def not_required_fields_complete(payload: dict) -> tuple[bool, str]:
    """¬`required_fields_complete` — the guard on arrow 16."""
    gaps = missing_fields(payload)
    if not gaps:
        return False, "no required fields are missing"
    return True, f"required fields missing: {', '.join(gaps)}"
