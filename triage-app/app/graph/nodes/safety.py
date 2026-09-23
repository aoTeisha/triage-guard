"""safety_validating and verdict_proposed — the deterministic safety verdict and
the confidence gate over it (SAFETY_PASSED, SAFETY_FAILED, ESCALATION_NEEDED,
CLEARED_TO_QUEUE, AF_SAFETY).
"""

from __future__ import annotations

from typing import Any

from app.actors import human_bridge, safety
from app.budgets import CONFIDENCE_THRESHOLD, confidence_ok
from app.deterministic import audit
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Transition
from app.schemas import SafetyVerdict
from app.states import State
from app.verification import verify_schema


def safety_validating(state: TriageState) -> dict[str, Any]:
    """Deterministic verdict on the settled acuity."""
    verdict = safety.validate(
        {"case_id": state.case_id, "acuity": state.acuity,
         "acuity_source": state.acuity_source, "payload": state.redacted_payload}
    )
    check = verify_schema("safety_validation", verdict, SafetyVerdict)

    if not check.passed:
        return {
            "control_state": State.SAFETY_VALIDATING.value,
            "retry_count": _bump(state, "safety_validation"),
            "audit_log": [audit(state.case_id, State.SAFETY_VALIDATING,
                                "discard_output", "; ".join(check.violations),
                                Transition.V_RETRY_SAFETY)],
        }

    checked: SafetyVerdict = check.checked
    passed = checked.verdict == "pass"
    return {
        "control_state": State.SAFETY_VALIDATING.value,
        "safety_verdict": checked.model_dump(),
        "safety_passed": passed,
        "escalation_reason": None if passed else human_bridge.SAFETY_FAIL,
        "audit_log": [audit(state.case_id, State.SAFETY_VALIDATING,
                            "emit_event_log" if passed else "invoke_human_escalation",
                            "safety passed" if passed else "safety failed, human decides",
                            Transition.SAFETY_PASSED if passed else Transition.SAFETY_FAILED,
                            reasons=checked.reasons)],
    }


def safety_fallback(state: TriageState, reason: str = "") -> dict[str, Any]:
    """AF_SAFETY / V_EXHAUSTED_SAFETY — validator down or unusable.

    Deliberately not a halt: route every case to a charge nurse so the
    no-approval-bypass invariant still holds while the validator is out.
    """
    return {
        "control_state": State.SAFETY_VALIDATING.value,
        "safety_passed": False,
        "escalation_reason": human_bridge.SAFETY_FAIL,
        "degraded": ["safety_validation"],
        "flags": ["safety_validator_down_all_to_charge"],
        "audit_log": [audit(state.case_id, State.SAFETY_VALIDATING,
                            "invoke_human_escalation",
                            "validator unusable, routing all cases to charge nurse"
                            + (f" ({reason})" if reason else ""),
                            Transition.V_EXHAUSTED_SAFETY)],
    }


def verdict_proposed(state: TriageState) -> dict[str, Any]:
    """A clean verdict, deciding whether a human should still confirm it.

    The spec gives this state no on-entry transition of its own — only
    ESCALATION_NEEDED and CLEARED_TO_QUEUE on the way out — so the "verdict
    recorded" line carries no transition rather than reusing SAFETY_PASSED and
    putting a second SAFETY_PASSED record in a trail meant to match the
    Transitions table row by row.
    """
    records = [audit(state.case_id, State.VERDICT_PROPOSED, "emit_event_log",
                     "verdict recorded")]

    if not confidence_ok(state.confidence, state.gate_disabled):
        records.append(
            audit(state.case_id, State.VERDICT_PROPOSED, "invoke_human_escalation",
                  f"confidence {state.confidence} below {CONFIDENCE_THRESHOLD}; "
                  "charge nurse confirms", Transition.ESCALATION_NEEDED)
        )
        return {
            "control_state": State.VERDICT_PROPOSED.value,
            "escalation_reason": human_bridge.LOW_CONFIDENCE,
            "audit_log": records,
        }

    return {"control_state": State.VERDICT_PROPOSED.value, "audit_log": records}
