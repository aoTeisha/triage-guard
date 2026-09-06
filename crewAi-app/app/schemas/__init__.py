"""Structured task outputs, mirroring the Data-plane variables in docs/SPECIFICATION.md.

Referenced from crew.jsonc as {"python": "schemas.<Name>"} — the loader resolves
these relative to crew.jsonc's own directory, not the package root.
"""

from .acuity_proposal import AcuityProposal
from .parse_result import IntakeOutcome, ParseResult
from .safety_verdict import SafetyVerdict

__all__ = ["IntakeOutcome", "ParseResult", "AcuityProposal", "SafetyVerdict"]
