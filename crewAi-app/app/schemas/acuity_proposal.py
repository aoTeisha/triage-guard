from typing import Literal

from pydantic import BaseModel, Field


class AcuityProposal(BaseModel):
    """The classifier's proposal. Not the final acuity — the Orchestrator settles that."""

    system_proposed_acuity: int = Field(ge=1, le=5, description="ESI level, 1 is most acute")
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence in the proposal")
    acuity_source: Literal["system", "rule_forced"] = Field(description="rule_forced when the red-flag pre-check set it")
    rationale: str = Field(description="Why this acuity, in one or two sentences")
