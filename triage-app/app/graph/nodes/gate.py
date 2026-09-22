"""awaiting_human_approval — the node where a case pauses for a charge
nurse's decision, whether that's resolving a nurse/system acuity
disagreement, confirming a low-confidence acuity, or reviewing a failed
safety validation.
"""

from __future__ import annotations

from typing import Any

from app.actors import human_bridge
from app.budgets import GATE_REMINDER_DELAY_MINUTES, SENIOR_REMINDER_DELAY_MINUTES
from app.deterministic import assign_order_key, audit, audit_denial, bucket_for
from app.graph.nodes._shared import is_release, release_case
from app.graph.state import TriageState
from app.labels import Arrow
from app.monitor import timers
from app.states import AcuitySource, ClinicalStatus, State
from app.symbolic import prolog

# Fields a safety correction may change (I7). Acuity only for now: I3 already
# lets a charge nurse set it at the gate. Extend once Safety Validation's checks
# are defined and it is known what else a correction can fix.
CORRECTABLE_FIELDS = frozenset({"acuity"})


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
    # A timer id is case:kind:schedule_seq and scheduling an existing id is a
    # no-op, so each gate visit needs its own sequence numbers or a second
    # visit gets no reminders (I15). The audit log's length is fixed while this
    # node replays on resume and grows between visits, so it numbers the visit.
    # Each visit reserves one number per rung, so `visit + rung` is unique and
    # its parity is the rung — which is how `fire._recipient_class` reads it
    # back to decide who gets nudged.
    visit = len(state.audit_log) * len(GATE_REMINDER_DELAY_MINUTES)
    for rung, delay in GATE_REMINDER_DELAY_MINUTES.items():
        timers.schedule(timers.connection(), case_id=state.case_id, kind="gate_reminder",
                         schedule_seq=visit + rung, due_at=timers.due_in(delay))

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

    if is_release(response):
        return release_case(state, response, State.AWAITING_HUMAN_APPROVAL)

    resolver = (response or {}).get("resolver_role", "")
    decision = (response or {}).get("decision")
    authorized, why = prolog.may_resolve_gate(resolver, senior_required=state.senior_required)

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

    if decision == "escalate_further" and not state.senior_required:
        # Handed up by choice rather than by running out of rounds (I8).
        return base | {
            "audit_log": [recorded,
                          audit(state.case_id, State.AWAITING_HUMAN_APPROVAL,
                                "escalate_further", f"{resolver} escalated to a senior",
                                Arrow.SENIOR_ESCALATION)],
        }

    # Safety-fail branch: correct and revalidate. No override path exists.
    # A correction must change something the case holds, or the same input
    # fails the same way (I7); refusing it uses up no round.
    changes = {k: v for k, v in ((response or {}).get("corrections") or {}).items()
               if k in CORRECTABLE_FIELDS and v != getattr(state, k)}
    if "acuity" in changes and not (type(changes["acuity"]) is int and 1 <= changes["acuity"] <= 5):
        del changes["acuity"]
    if not changes:
        why = f"correction required: change at least one of {sorted(CORRECTABLE_FIELDS)}"
        return base | {"audit_log": [recorded, audit_denial(state.case_id, State.AWAITING_HUMAN_APPROVAL, why,
                                                            layer="gate (I7 correction check)")]}

    update: dict[str, Any] = {
        "correction_rounds": state.correction_rounds + 1,
        "safety_passed": False,
        "audit_log": [recorded,
                      audit(state.case_id, State.AWAITING_HUMAN_APPROVAL,
                            "apply_correction",
                            f"correction round {state.correction_rounds + 1}: {changes}; re-running safety",
                            Arrow.GATE_SAFETY_CORRECTED)],
    }
    if "acuity" in changes:
        new = changes["acuity"]
        update |= {
            "acuity": new,
            "acuity_source": AcuitySource.HUMAN_CONFIRMED.value,
            "acuity_bucket": bucket_for(new).value,
            "order_key": assign_order_key(new, state.arrival_time),   # a real acuity change (I2)
        }
    return base | update


def escalate_to_senior(state: TriageState) -> dict[str, Any]:
    """The correction loop ran out, or a charge nurse chose "Escalate further":
    a shift lead must now decide, and the case stops counting rounds (I8). It
    returns to the gate pause, so it stays open and releasable, and one reminder
    goes to any shift lead (I15).
    """
    timers.schedule(timers.connection(), case_id=state.case_id, kind="senior_reminder",
                     schedule_seq=len(state.audit_log), due_at=timers.due_in(SENIOR_REMINDER_DELAY_MINUTES))
    return {
        "senior_required": True,
        "audit_log": [audit(state.case_id, State.AWAITING_HUMAN_APPROVAL, "escalate_to_senior",
                            "correction loop handed to a shift lead", Arrow.SENIOR_ESCALATION)],
    }
