"""Intake Parser — deterministic webform validator (docs/actors/intake_parser.jsonc).

Type: Validator (deterministic). Never an LLM: the four intake outcomes are the
front door's security boundary, and the spec's forbidden sequence is "injection
reaches the model", so a probabilistic detector cannot be the thing enforcing it.

Proposes a `ParseResult`; the graph decides where the case goes.
"""

from __future__ import annotations

from typing import Any

from app.events import IntakeOutcome
from app.guards import detect_injection, missing_fields, nothing_usable
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

    gaps = missing_fields(raw_payload)
    if gaps:
        return ParseResult(
            outcome=IntakeOutcome.MISSING_FIELDS_DETECTED,
            missing_fields=gaps,
            reason=f"required fields missing: {', '.join(gaps)}",
        )

    return ParseResult(
        outcome=IntakeOutcome.DATA_PARSED,
        parsed_fields=dict(raw_payload),
    )
