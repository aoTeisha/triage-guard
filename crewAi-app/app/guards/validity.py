"""Combined input-validity guards (docs/SPECIFICATION.md § Guards)."""

from __future__ import annotations

from .fields import missing_fields
from .injection import detect_injection


def not_input_is_valid(payload: dict) -> tuple[bool, str]:
    """¬`input_is_valid` = ¬schema_matches ∨ injection_detected(free_text) —
    the guard on arrow 18.

    Injection is checked first and its reason wins outright: a payload that is
    both malformed and hostile is reported as "injection", never
    "invalid_schema". Mislabelling it would route a security event into the
    ordinary schema-error bucket.
    """
    detected, label = detect_injection(payload.get("free_text") or "")
    if detected:
        return True, f"injection ({label})"
    if not isinstance(payload.get("case_id"), str):
        return True, "invalid_schema"
    return False, "input is valid"


def required_fields_complete_and_valid(payload: dict) -> tuple[bool, str]:
    """`required_fields_complete` ∧ `input_is_valid` — the guard on arrow 4."""
    flagged, reason = not_input_is_valid(payload)
    if flagged:
        return False, reason
    gaps = missing_fields(payload)
    if gaps:
        return False, f"required fields missing: {', '.join(gaps)}"
    return True, "submission valid"
