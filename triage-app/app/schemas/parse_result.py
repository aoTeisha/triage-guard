from pydantic import BaseModel, Field

from app.events import IntakeOutcome


class ParseResult(BaseModel):
    """Outcome of validating one submitted triage webform."""

    outcome: IntakeOutcome = Field(description="Which of the four intake branches this submission took")
    parsed_fields: dict = Field(default_factory=dict, description="Extracted webform fields; empty unless outcome is DATA_PARSED")
    missing_fields: list[str] = Field(default_factory=list, description="Mandatory fields that were absent")
    reason: str | None = Field(default=None, description="Error reason; for INVALID_INPUT_DETECTED one of invalid_schema or injection")
