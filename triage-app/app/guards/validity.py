"""Combined input-validity guards (docs/SPECIFICATION.md § Guards)."""

from __future__ import annotations

from .fields import missing_fields


def not_input_is_valid(payload: dict) -> tuple[bool, str]:
    """¬`input_is_valid` = ¬schema_matches — the guard on INVALID_INPUT.

    The one structural requirement is a string case id: without it the case
    cannot be recorded, so it is rejected rather than sent back for completion.
    """
    if not isinstance(payload.get("case_id"), str):
        return True, "invalid_schema"
    return False, "input is valid"


def required_fields_complete_and_valid(payload: dict) -> tuple[bool, str]:
    """`required_fields_complete` ∧ `input_is_valid` — the guard on SUBMISSION_VALID."""
    flagged, reason = not_input_is_valid(payload)
    if flagged:
        return False, reason
    gaps = missing_fields(payload)
    if gaps:
        return False, f"required fields missing: {', '.join(gaps)}"
    return True, "submission valid"
