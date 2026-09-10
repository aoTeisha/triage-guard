"""safety_validating and verdict_proposed — the deterministic safety verdict and
the confidence gate over it (arrows 10, 10·fail, 11, 11·pass, AF·safety).
"""

from __future__ import annotations

from typing import Any

from app.actors import human_bridge, safety
from app.budgets import CONFIDENCE_THRESHOLD, confidence_ok
from app.deterministic import audit
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Arrow
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
                                Arrow.V_RETRY_SAFETY)],
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
                            Arrow.SAFETY_PASSED if passed else Arrow.SAFETY_FAILED,
                            reasons=checked.reasons)],
    }


def safety_fallback(state: TriageState, reason: str = "") -> dict[str, Any]:
    """AF·safety / V·exhausted·safety — validator down or unusable.

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
                            Arrow.V_EXHAUSTED_SAFETY)],
    }


def verdict_proposed(state: TriageState) -> dict[str, Any]:
    """A clean verdict, deciding whether a human should still confirm it.

    The spec gives this state no on-entry arrow of its own — only 11 and 11·pass
    on the way out — so the "verdict recorded" line carries no arrow rather than
    reusing arrow 10 and putting a second arrow-10 record in a trail meant to diff
    against the Transitions table line by line.
    """
    records = [audit(state.case_id, State.VERDICT_PROPOSED, "emit_event_log",
                     "verdict recorded")]

    if not confidence_ok(state.confidence, state.gate_disabled):
        records.append(
            audit(state.case_id, State.VERDICT_PROPOSED, "invoke_human_escalation",
                  f"confidence {state.confidence} below {CONFIDENCE_THRESHOLD}; "
                  "charge nurse confirms", Arrow.ESCALATION_NEEDED)
        )
        return {
            "control_state": State.VERDICT_PROPOSED.value,
            "escalation_reason": human_bridge.LOW_CONFIDENCE,
            "audit_log": records,
        }

    return {"control_state": State.VERDICT_PROPOSED.value, "audit_log": records}
