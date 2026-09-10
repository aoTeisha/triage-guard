"""Structured payload schemas, mirroring the Data-plane variables in docs/SPECIFICATION.md.

These are load-bearing, not decorative. Each one is what `app/verification.py`
validates an actor's proposal against before the graph writes it into state — the
`output_verified` guard's schema half. `AcuityProposal` is additionally handed to
the LLM as its `response_format`, so the 1-5 acuity bound is enforced at the model
boundary rather than trusted.

`SafetyVerdict` is defined here and only here; `graph/state.py` imports it.
"""

from .acuity_proposal import AcuityProposal
from .parse_result import ParseResult
from .safety_verdict import SafetyVerdict

__all__ = ["ParseResult", "AcuityProposal", "SafetyVerdict"]
