"""Safety Validation — deterministic verdict (docs/actors/safety_validator.jsonc).

Tech per the spec: Prolog / Datalog / Z3 / OPA. Deterministic by requirement, not
by convenience — a safety verdict that a stochastic model could influence would
make every downstream invariant unprovable.

SKELETON: returns the canned verdict from `mocks/safety_validator.json`. Edit that
file to exercise the fail branch (10·fail -> the human gate). Replace this function
body with real engine calls; the graph already routes pass / fail and the V·* rows
off its result, so no wiring changes.
"""

from __future__ import annotations

from typing import Any

from app.actors import load_mock
from app.schemas import SafetyVerdict


class ValidatorUnavailable(RuntimeError):
    """Raised when the symbolic engine cannot be reached (drives AF·safety)."""


def validate(case: dict[str, Any]) -> SafetyVerdict:
    """Propose pass/fail on the settled acuity.

    Takes the case as a plain mapping rather than the graph state: the real engines
    receive facts, not a framework object, and keeping that boundary now means the
    swap later is local to this file.
    """
    return SafetyVerdict.model_validate(load_mock("safety_validator"))
