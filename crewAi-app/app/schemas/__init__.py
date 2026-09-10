"""Structured task outputs, mirroring the Data-plane variables in docs/SPECIFICATION.md.

Written for the old crew.jsonc's output_pydantic wiring (now removed with the
move to a Flow); currently unused by app/flow/. Reused if a task ever needs a
Pydantic output schema again.
"""

from .acuity_proposal import AcuityProposal
from .parse_result import IntakeOutcome, ParseResult
from .safety_verdict import SafetyVerdict

__all__ = ["IntakeOutcome", "ParseResult", "AcuityProposal", "SafetyVerdict"]
