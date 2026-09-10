"""awaiting_human_approval — the human gate (9c, 10·fail, 1b.z·*)."""

from __future__ import annotations

from typing import Any

from app.actors import human_bridge
from app.deterministic import actor_is_charge, assign_order_key, audit, bucket_for, now_iso
from app.graph.state import TriageState
from app.labels import Arrow
from app.states import AcuitySource, ClinicalStatus, State


def awaiting_human_approval(state: TriageState) -> dict[str, Any]:
    """The human gate. A real pause: the run suspends here until someone resumes it.

    Everything before `request_decision` must stay side-effect-free — LangGraph
    replays this node from the top when the run resumes, so any write above the
    interrupt would happen twice.
    """
    reason = state.escalation_reason or human_bridge.SAFETY_FAIL

    response = human_bridge.request_decision(
        reason,
        {
            "case_id": state.case_id,
            "nurse_proposed_acuity": state.nurse_proposed_acuity,
            "system_proposed_acuity": state.system_proposed_acuity,
            "acuity_gap": state.acuity_gap,
            "safety_verdict": state.safety_verdict.model_dump() if state.safety_verdict else None,
        },
    )

    resolver = (response or {}).get("resolver_role", "")
    decision = (response or {}).get("decision")
    authorized, why = actor_is_charge(resolver)

    # Arrow 12: the Human Escalation agent has returned. Recorded before the
    # response is authorized or applied, so a refused response still leaves
    # evidence that a response arrived.
    recorded = audit(state.case_id, State.AWAITING_HUMAN_APPROVAL, "emit_event_log",
                     f"escalation recorded: {decision} by {resolver}",
                     Arrow.ESCALATION_RECORDED)

    base: dict[str, Any] = {
        "control_state": State.AWAITING_HUMAN_APPROVAL.value,
        "clinical_status": ClinicalStatus.HUMAN_REVIEW.value,
        "resolver_role": resolver,
        "human_decision": decision,
    }

    if not authorized:
        # BLK: the attempt is refused and the case does not move.
        return base | {
            "audit_log": [recorded,
                          audit(state.case_id, State.AWAITING_HUMAN_APPROVAL,
                                "explain_denial", why, Arrow.BLK,
                                denying_layer="Prolog (authorization)")],
        }

    if reason in human_bridge.ACUITY_REASONS:
        chosen = (
            state.nurse_proposed_acuity if decision == "use_nurse_acuity"
            else state.system_proposed_acuity
        )
        arrival = state.arrival_time or now_iso()
        return base | {
            "acuity": chosen,
            "acuity_source": AcuitySource.HUMAN_CONFIRMED.value,
            "acuity_bucket": bucket_for(chosen).value if chosen is not None else None,
            "order_key": assign_order_key(chosen, arrival) if chosen is not None else None,
            "approved": True,
            "audit_log": [recorded,
                          audit(state.case_id, State.AWAITING_HUMAN_APPROVAL,
                                "apply_human_acuity",
                                f"charge nurse resolved acuity: {chosen}",
                                Arrow.GATE_ACUITY_RESOLVED)],
        }

    # Safety-fail branch: correct and revalidate. No override path exists.
    return base | {
        "correction_rounds": state.correction_rounds + 1,
        "safety_passed": False,
        "audit_log": [recorded,
                      audit(state.case_id, State.AWAITING_HUMAN_APPROVAL,
                            "apply_correction",
                            f"correction round {state.correction_rounds + 1}, re-running safety",
                            Arrow.GATE_SAFETY_CORRECTED)],
    }
