"""Deterministic guards for the intake slice (docs/SPECIFICATION.md § Guards).

Every guard here returns (passed, explanation) — one shape, so state_machine's
transition() can call any of them the same way and put the explanation
straight into the audit record. None of them infer or guess a missing value:
presence checks only. No LLM, no network, no clock — these protect invariants,
and an invariant cannot depend on a probabilistic model.
"""

from .fields import REQUIRED_FIELDS, missing_fields, nothing_usable, not_required_fields_complete
from .injection import detect_injection
from .validity import not_input_is_valid, required_fields_complete_and_valid

__all__ = [
    "REQUIRED_FIELDS",
    "missing_fields",
    "nothing_usable",
    "not_required_fields_complete",
    "detect_injection",
    "not_input_is_valid",
    "required_fields_complete_and_valid",
]
