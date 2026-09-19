"""awaiting_human_approval — the node where a case pauses for a charge
nurse's decision, whether that's resolving a nurse/system acuity
disagreement, confirming a low-confidence acuity, or reviewing a failed
safety validation.
"""

from __future__ import annotations

from typing import Any

from app.actors import human_bridge
from app.budgets import GATE_REMINDER_DELAY_MINUTES
from app.deterministic import actor_is_charge, assign_order_key, audit, audit_denial, bucket_for
from app.graph.state import TriageState
from app.labels import Arrow
from app.monitor import timers
from app.states import AcuitySource, ClinicalStatus, State


def awaiting_human_approval(state: TriageState) -> dict[str, Any]:
    """The human gate. A real pause: the run suspends here until someone
    calls `resume_case` with a decision.

    Everything before `request_decision` must stay side-effect-free —
    LangGraph replays this node from the top every time the run resumes, so
    any write placed before the pause would run twice. `timers.schedule` is
    an exception: it's safe to call twice because it's idempotent on
    `timer_id` (a repeat call with the same id is a no-op), so a replay just
    re-schedules the same two reminder timers instead of duplicating them.
    """
    for cycle, delay in GATE_REMINDER_DELAY_MINUTES.items():
        timers.schedule(timers.connection(), case_id=state.case_id, kind="gate_reminder",
                         cycle=cycle, due_at=timers.due_in(delay))

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

    # Record that a decision came back, before checking whether it's
    # authorized or applying it — so even a refused response leaves evidence
    # that someone actually answered.
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
        # Whoever responded isn't authorized as a charge nurse: refuse the
        # attempt and leave the case exactly where it was.
        return base | {
            "audit_log": [recorded, audit_denial(state.case_id, State.AWAITING_HUMAN_APPROVAL, why)],
        }

    if reason in human_bridge.ACUITY_REASONS:
        chosen = (
            state.nurse_proposed_acuity if decision == "use_nurse_acuity"
            else state.system_proposed_acuity
        )
        arrival = state.arrival_time
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
