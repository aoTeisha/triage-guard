"""Deterministic guards for the intake slice (docs/SPECIFICATION.md § Guards).

Every guard here returns (passed, explanation) — one shape, so state_machine's
transition() can call any of them the same way and put the explanation
straight into the audit record. None of them infer or guess a missing value:
presence checks only. No LLM, no network, no clock — these protect invariants,
and an invariant cannot depend on a probabilistic model.
"""

from __future__ import annotations

import re

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

# Injection patterns, each tagged with the category written to the audit log.
# This runs BEFORE any model sees the text — the spec's forbidden sequence is
# "injection reaches the model", so a probabilistic detector cannot be the one
# enforcing it.
_I = re.IGNORECASE | re.MULTILINE

_INJECTION_PATTERNS = (
    (re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)", _I), "override"),
    (re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)", _I), "override"),
    (re.compile(r"new\s+instructions\s*:", _I), "override"),
    (re.compile(r"you\s+are\s+now\b", _I), "role_injection"),
    (re.compile(r"^\s*(?:system|assistant)\s*:", _I), "role_injection"),
    (re.compile(r"```", _I), "format_break"),
    (re.compile(r"</?(?:system|prompt|instructions?)>", _I), "format_break"),
    (re.compile(r"\bset\s+(?:the\s+)?acuity\s+to\s*\d", _I), "acuity_targeting"),
    (re.compile(r"\bacuity\s*(?:=|:)\s*\d", _I), "acuity_targeting"),
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


def detect_injection(free_text: str) -> tuple[bool, str | None]:
    """Returns (is_injection, matched_category). The category feeds the audit
    log — explainability, not just a boolean.
    """
    if not free_text:
        return False, None
    for pattern, label in _INJECTION_PATTERNS:
        if pattern.search(free_text):
            return True, label
    return False, None


def required_fields_complete_and_valid(payload: dict) -> tuple[bool, str]:
    """`required_fields_complete` ∧ `input_is_valid` — the guard on arrow 4."""
    flagged, reason = not_input_is_valid(payload)
    if flagged:
        return False, reason
    gaps = missing_fields(payload)
    if gaps:
        return False, f"required fields missing: {', '.join(gaps)}"
    return True, "submission valid"


def not_required_fields_complete(payload: dict) -> tuple[bool, str]:
    """¬`required_fields_complete` — the guard on arrow 16."""
    gaps = missing_fields(payload)
    if not gaps:
        return False, "no required fields are missing"
    return True, f"required fields missing: {', '.join(gaps)}"


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
