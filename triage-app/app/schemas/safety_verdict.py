from typing import Literal

from pydantic import BaseModel, Field


class SafetyVerdict(BaseModel):
    """Deterministic pass/fail on the settled acuity. A fail routes to a charge nurse."""

    verdict: Literal["pass", "fail"] = Field(description="pass clears the case to queue; fail sends it to the human gate")
    reasons: list[str] = Field(default_factory=list, description="Which rules were evaluated and what failed")
