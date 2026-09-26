"""Intake Parser — deterministic webform validator (docs/actors/intake_parser.jsonc).

Type: Validator (deterministic). Never an LLM: the four intake outcomes are the
front door's security boundary, and the spec's forbidden sequence is "injection
reaches the model", so a probabilistic detector cannot be the thing enforcing it.

Proposes a `ParseResult`; the graph decides where the case goes.
"""

from __future__ import annotations

from typing import Any

from app.events import IntakeOutcome
from app.guards import detect_injection, missing_fields, nothing_usable, unusable_fields
from app.guards.validity import not_input_is_valid
from app.schemas import ParseResult


def parse_intake(raw_payload: dict[str, Any]) -> ParseResult:
    """Classify one submission into exactly one of the four outcomes.

    Priority order is deliberate and matches `guards.classify_intake_payload`:
    injection > nothing-usable > gaps > clean. Injection outranks missing fields
    because a hostile submission must be rejected outright, not sent back for
    completion.
    """
    injected, category = detect_injection(str(raw_payload.get("free_text") or ""))
    if injected or not_input_is_valid(raw_payload)[0]:
        return ParseResult(
            outcome=IntakeOutcome.INVALID_INPUT_DETECTED,
            reason=f"injection:{category}" if injected else "invalid_schema",
        )

    if nothing_usable(raw_payload):
        return ParseResult(
            outcome=IntakeOutcome.SUBMISSION_FAILED,
            reason="submission carried no clinical content",
        )

    # Absent and unusable are one outcome: both need the nurse, and both are
    # fixable in place. An out-of-range acuity is not routed to INVALID_INPUT —
    # that ends the run, and the patient is still standing at the desk (I10).
    absent, unusable = missing_fields(raw_payload), unusable_fields(raw_payload)
    if absent or unusable:
        reason = "; ".join(
            part for part in (f"required fields missing: {', '.join(absent)}" if absent else "",
                              f"unusable values: {', '.join(unusable)}" if unusable else "")
            if part
        )
        return ParseResult(
            outcome=IntakeOutcome.MISSING_FIELDS_DETECTED,
            missing_fields=absent + unusable,
            reason=reason,
        )

    return ParseResult(
        outcome=IntakeOutcome.DATA_PARSED,
        parsed_fields=dict(raw_payload),
    )
