"""classifying and acuity_proposed — the single LLM step and the deterministic
gap resolution over it (RUN_CLASSIFIER, ACUITY_PROPOSED, ACUITY_AGREE,
ACUITY_GAP_MINOR, ACUITY_GAP_MAJOR, V_RETRY_CLASSIFIER, V_EXHAUSTED_CLASSIFIER).
"""

from __future__ import annotations

from typing import Any

from app.actors import acuity_classifier, human_bridge
from app.deterministic import assign_order_key, audit, bucket_for, compute_acuity_gap, resolve_acuity
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Transition
from app.states import AcuitySource, State
from app.verification import verify_schema


def classifying(state: TriageState) -> dict[str, Any]:
    """The single LLM step. The model proposes; the gate settles the level."""
    invoked = audit(state.case_id, State.CLASSIFYING, "invoke_acuity_classifier",
                    "run classifier",
                    Transition.RUN_CLASSIFIER)
    proposal = acuity_classifier.classify(state.redacted_payload)
    check = verify_schema("acuity_classifier", proposal, type(proposal))

    if not check.passed:
        return {
            "control_state": State.CLASSIFYING.value,
            "retry_count": _bump(state, "acuity_classifier"),
            "audit_log": [invoked,
                          audit(state.case_id, State.CLASSIFYING, "discard_output",
                                "; ".join(check.violations),
                                Transition.V_RETRY_CLASSIFIER)],
        }

    checked = check.checked
    return {
        "control_state": State.CLASSIFYING.value,
        "system_proposed_acuity": checked.system_proposed_acuity,
        "confidence": checked.confidence,
        "acuity_source": checked.acuity_source,
        "audit_log": [invoked,
                      audit(state.case_id, State.CLASSIFYING, "emit_event_log",
                            f"acuity proposed: {checked.system_proposed_acuity} "
                            f"({checked.acuity_source})", Transition.ACUITY_PROPOSED)],
    }


def classifier_fallback(state: TriageState, reason: str = "") -> dict[str, Any]:
    """AF_CLASSIFIER / V_EXHAUSTED_CLASSIFIER.

    Drop the system acuity, fall back to the nurse's, disable the discrepancy gate
    for the outage, and flag the case for later review.
    """
    nurse = state.nurse_proposed_acuity
    arrival = state.arrival_time
    update: dict[str, Any] = {
        "control_state": State.CLASSIFYING.value,
        "system_proposed_acuity": None,
        "gate_disabled": True,
        "degraded": ["acuity_classifier"],
        "flags": ["cross_check_off_review_later"],
        "audit_log": [audit(state.case_id, State.CLASSIFYING, "fallback_manual",
                            "classifier unusable, using nurse acuity; gate disabled"
                            + (f" ({reason})" if reason else ""),
                            Transition.V_EXHAUSTED_CLASSIFIER)],
    }
    if nurse is not None:
        update |= {
            "acuity": nurse,
            "acuity_source": AcuitySource.HUMAN_CONFIRMED.value,
            "acuity_bucket": bucket_for(nurse).value,
            "order_key": assign_order_key(nurse, arrival),
        }
    return update


def acuity_proposed(state: TriageState) -> dict[str, Any]:
    """Deterministic gap resolution over the gap bands (I4)."""
    nurse = state.nurse_proposed_acuity
    system = state.system_proposed_acuity
    arrival = state.arrival_time

    gap = compute_acuity_gap(nurse, system)
    final, source, transition = resolve_acuity(nurse, system)

    if final is None:      # ACUITY_GAP_MAJOR — charge nurse decides, no acuity settled here
        return {
            "control_state": State.ACUITY_PROPOSED.value,
            "acuity_gap": gap,
            "escalation_reason": human_bridge.DISCREPANCY,
            "audit_log": [audit(state.case_id, State.ACUITY_PROPOSED,
                                "invoke_human_escalation",
                                "gap >= 2, charge nurse decides", transition)],
        }

    return {
        "control_state": State.ACUITY_PROPOSED.value,
        "acuity": final,
        "acuity_gap": gap,
        "acuity_source": source.value if source else None,
        "acuity_bucket": bucket_for(final).value,
        "order_key": assign_order_key(final, arrival),
        "audit_log": [audit(state.case_id, State.ACUITY_PROPOSED,
                            "assign_order_key", f"acuity settled: {final}", transition)],
    }
