from typing import Literal

from pydantic import BaseModel, Field

from app.guards.fields import ACUITY_LEVELS


class AcuityProposal(BaseModel):
    """The classifier's proposal. Not the final acuity — the Orchestrator settles that."""

    system_proposed_acuity: int = Field(ge=min(ACUITY_LEVELS), le=max(ACUITY_LEVELS),
                                        description="ESI level, 1 is most acute")
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence in the proposal")
    acuity_source: Literal["system"] = Field(description="the classifier is the only source of a proposal")
    rationale: str = Field(description="Why this acuity, in one or two sentences")
