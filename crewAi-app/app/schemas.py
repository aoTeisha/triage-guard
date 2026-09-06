"""Structured task outputs, mirroring the Data-plane variables in docs/SPECIFICATION.md.

Referenced from crew.jsonc as {"python": "schemas.<Name>"} — the loader resolves
these relative to crew.jsonc's own directory, not the package root.
"""

from typing import Literal

from pydantic import BaseModel, Field

IntakeOutcome = Literal[
    "DATA_PARSED",
    "MISSING_FIELDS_DETECTED",
    "SUBMISSION_FAILED",
    "INVALID_INPUT_DETECTED",
]


class ParseResult(BaseModel):
    """Outcome of validating one submitted triage webform."""

    outcome: IntakeOutcome = Field(description="Which of the four intake branches this submission took")
    parsed_fields: dict = Field(default_factory=dict, description="Extracted webform fields; empty unless outcome is DATA_PARSED")
    missing_fields: list[str] = Field(default_factory=list, description="Mandatory fields that were absent")
    reason: str | None = Field(default=None, description="Error reason; for INVALID_INPUT_DETECTED one of invalid_schema or injection")


class AcuityProposal(BaseModel):
    """The classifier's proposal. Not the final acuity — the Orchestrator settles that."""

    system_proposed_acuity: int = Field(ge=1, le=5, description="ESI level, 1 is most acute")
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence in the proposal")
    acuity_source: Literal["system", "rule_forced"] = Field(description="rule_forced when the red-flag pre-check set it")
    rationale: str = Field(description="Why this acuity, in one or two sentences")


class SafetyVerdict(BaseModel):
    """Deterministic pass/fail on the settled acuity. A fail routes to a charge nurse."""

    verdict: Literal["pass", "fail"] = Field(description="pass clears the case to queue; fail sends it to the human gate")
    reasons: list[str] = Field(default_factory=list, description="Which rules were evaluated and what failed")
