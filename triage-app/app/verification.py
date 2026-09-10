"""Output Verification (docs/SPECIFICATION.md § On output verification).

Post-agent, deterministic, and on the symbolic side of the neuro-symbolic split.
Guards ask whether a step may run; this asks whether what it returned is usable.
No proposal reaches shared state without passing through here.

The spec splits failures two ways, and the split decides the route, so it is
modelled as a type rather than a bool:

  RECOVERABLE  schema mismatch, value out of range, null/wrong type. The step is
               healthy, its output is malformed. Discard and re-invoke on the
               agent's retry budget (V·retry), then that agent's AF row
               (V·exhausted).
  STRUCTURAL   an identifier reached a redacted payload, or a safety invariant
               broke. A retry cannot fix it. Discard and halt (V·halt).

One rule never bends: a malformed or unsafe output is never written to state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError

from app.deterministic import verify_no_identifiers


class Violation(str, Enum):
    RECOVERABLE = "recoverable"
    STRUCTURAL = "structural"


@dataclass(frozen=True)
class VerificationResult:
    """`VERIFICATION_PASSED` with the checked output, or `VERIFICATION_FAILED`
    with the violation list and its category.
    """

    passed: bool
    violations: tuple[str, ...] = ()
    category: Violation | None = None
    checked: Any = None

    @property
    def structural(self) -> bool:
        return self.category is Violation.STRUCTURAL


def _passed(checked: Any) -> VerificationResult:
    return VerificationResult(passed=True, checked=checked)


def verify_schema(agent: str, output: Any, model: type[BaseModel]) -> VerificationResult:
    """Schema + value-range check against the agent's declared output model.

    Pydantic does the work: field presence, types, and the `ge`/`le` bounds that
    `AcuityProposal` puts on the ESI level. A model that returns acuity 47 fails
    here instead of being written to state.
    """
    if isinstance(output, model):
        return _passed(output)
    if not isinstance(output, dict):
        return VerificationResult(
            passed=False,
            violations=(f"{agent}: expected dict or {model.__name__}, got {type(output).__name__}",),
            category=Violation.RECOVERABLE,
        )
    try:
        return _passed(model.model_validate(output))
    except ValidationError as exc:
        violations = tuple(
            f"{agent}: {'.'.join(str(p) for p in err['loc'])} — {err['msg']}"
            for err in exc.errors()
        )
        return VerificationResult(
            passed=False, violations=violations, category=Violation.RECOVERABLE
        )


def verify_redacted_payload(payload: dict[str, Any]) -> VerificationResult:
    """The `redacting_routing` check (V·halt·PII).

    Structural by definition: an identifier in the model-facing payload is a
    privacy-invariant breach, and re-running the same drop on the same input would
    leak it again. Never retried.
    """
    ok, why = verify_no_identifiers(payload)
    if not ok:
        return VerificationResult(
            passed=False, violations=(why,), category=Violation.STRUCTURAL
        )
    if not payload.get("case_id"):
        # The payload must be keyed by case_id — without it the proposal cannot be
        # attributed to a case, which breaks the audit chain. Recoverable: rebuild.
        return VerificationResult(
            passed=False,
            violations=("redacted payload is not keyed by case_id",),
            category=Violation.RECOVERABLE,
        )
    return _passed(payload)


def verify_patient_record(record: Any) -> VerificationResult:
    """CRM shape check. A record that is present but not a mapping is malformed
    (recoverable); an absent record is a legitimate new-patient result, not a
    failure, and is verified as passing.
    """
    if record is None:
        return _passed(None)
    if not isinstance(record, dict):
        return VerificationResult(
            passed=False,
            violations=(f"crm: expected record mapping, got {type(record).__name__}",),
            category=Violation.RECOVERABLE,
        )
    return _passed(record)
