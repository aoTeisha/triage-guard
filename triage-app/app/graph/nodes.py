"""Graph nodes — one function per control-plane state.

Every node follows the same contract, and it is the contract the spec's State rule
demands: call an actor, verify what it proposed, then return a partial state update.
Actors propose; nodes write; nothing else writes.

Nodes return dicts instead of mutating `state`. LangGraph replays a node on resume
(and on retry), so in-place mutation would double-count. The `audit_log`, `degraded`
and `flags` reducers in `state.py` turn each returned list into an append.
"""

from __future__ import annotations

from typing import Any

from app.actors import acuity_classifier, human_bridge, intake, normalizer, safety
from app.budgets import (
    CONFIDENCE_THRESHOLD,
    confidence_ok,
    correction_rounds_left,
    retry_budget_left,
)
from app.crm_client import fetch_patient
from app.deterministic import (
    actor_is_charge,
    assign_order_key,
    audit,
    bucket_for,
    compute_acuity_gap,
    now_iso,
    resolve_acuity,
)
from app.graph.state import TriageState
from app.labels import Arrow
from app.schemas import SafetyVerdict
from app.states import AcuitySource, ClinicalStatus, State
from app.verification import (
    verify_patient_record,
    verify_redacted_payload,
    verify_schema,
)


def _bump(state: TriageState, agent: str) -> dict[str, int]:
    """Increment this agent's shared retry counter (§ retry_budget_left)."""
    return {agent: state.retry_count.get(agent, 0) + 1}


# ---- intake_received (arrows 1a, 2) ----------------------------------------


def intake_received(state: TriageState) -> dict[str, Any]:
    """Entry. Records arrival and, when it legitimately can, a queue position."""
    arrival = state.arrival_time or now_iso()
    update: dict[str, Any] = {
        "control_state": State.INTAKE_RECEIVED.value,
        "arrival_time": arrival,
        "audit_log": [
            audit(state.case_id, State.INTAKE_RECEIVED, "route_channel",
                  "new case entered", Arrow.ENTRY),
            audit(state.case_id, State.INTAKE_RECEIVED, "emit_event_log",
                  "input normalized", Arrow.NORMALIZED),
        ],
    }

    # order_key needs a bucket, and the bucket needs an acuity. The only acuity
    # available at entry is the nurse's, which guards/fields.py states is mandatory
    # and "never inferred: absent means arrow 16, never a guessed value". So when it
    # is absent the case gets no queue position — it is heading for
    # missing_fields_requested and will be keyed on the way back through.
    if state.nurse_proposed_acuity is not None:
        update["order_key"] = assign_order_key(state.nurse_proposed_acuity, arrival)

    return update


# ---- parsing (arrow 3) -------------------------------------------------------


def parsing(state: TriageState) -> dict[str, Any]:
    """Run the deterministic validator. Routing happens in `route_intake`."""
    result = intake.parse_intake(state.raw_payload)
    check = verify_schema("intake_parser", result, type(result))

    if not check.passed:
        return {
            "control_state": State.PARSING.value,
            "retry_count": _bump(state, "intake_parser"),
            "audit_log": [audit(state.case_id, State.PARSING, "discard_output",
                                "; ".join(check.violations), Arrow.V_RETRY)],
        }

    parsed = check.checked
    return {
        "control_state": State.PARSING.value,
        "intake_outcome": parsed.outcome.value,
        "intake_reason": parsed.reason,
        "missing_fields": parsed.missing_fields,
        "parsed_fields": parsed.parsed_fields,
        "stable_patient_id": parsed.parsed_fields.get("stable_patient_id"),
        "nurse_proposed_acuity": parsed.parsed_fields.get("nurse_proposed_acuity"),
        "audit_log": [
            audit(state.case_id, State.PARSING, "invoke_intake_parser",
                  f"intake outcome: {parsed.outcome.value}", Arrow.RUN_VALIDATOR),
        ],
    }


# ---- the three non-happy intake terminals (16 / 17 / 18) --------------------


def missing_fields_requested(state: TriageState) -> dict[str, Any]:
    return {
        "control_state": State.MISSING_FIELDS_REQUESTED.value,
        "audit_log": [audit(state.case_id, State.MISSING_FIELDS_REQUESTED,
                            "notify_user", "request fields", Arrow.MISSING_FIELDS,
                            missing_fields=state.missing_fields)],
    }


def submission_failed(state: TriageState) -> dict[str, Any]:
    return {
        "control_state": State.SUBMISSION_FAILED.value,
        "audit_log": [audit(state.case_id, State.SUBMISSION_FAILED, "notify_user",
                            "resubmit or manual", Arrow.SUBMISSION_UNUSABLE)],
    }


def input_rejected(state: TriageState) -> dict[str, Any]:
    return {
        "control_state": State.INPUT_REJECTED.value,
        "audit_log": [audit(state.case_id, State.INPUT_REJECTED, "notify_user",
                            "invalid input", Arrow.INVALID_INPUT,
                            security=True, reason=state.intake_reason)],
    }


# ---- data_parsed (arrow 4 -> 4b) --------------------------------------------


def data_parsed(state: TriageState) -> dict[str, Any]:
    """Pass-through state from the Transitions table. It carries no logic of its
    own — it exists so the audit trail shows arrow 4 before arrow 4b, matching the
    spec rather than collapsing two documented steps into one.
    """
    return {
        "control_state": State.DATA_PARSED.value,
        "audit_log": [
            audit(state.case_id, State.DATA_PARSED, "emit_event_log",
                  "submission valid", Arrow.SUBMISSION_VALID),
            audit(state.case_id, State.DATA_PARSED, "fetch_patient_data",
                  "look up record", Arrow.LOOKUP),
        ],
    }


# ---- resolving_identity (4b·found / 4b·new / AF·db) -------------------------


def resolving_identity(state: TriageState) -> dict[str, Any]:
    """CRM lookup by stable patient ID. Non-critical and fail-open: a DB outage
    degrades to intake-only data rather than stopping the line.
    """
    try:
        result = fetch_patient(state.stable_patient_id or "", timeout=1.0)
        status, record = result.status, result.record
    except Exception:
        status, record = "db_error", None

    if status == "db_error":
        return {
            "control_state": State.RESOLVING_IDENTITY.value,
            "crm_status": status,
            "degraded": ["crm"],
            "flags": ["crm_down_intake_only"],
            "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                                "alert_technician", "DB unreachable, degraded",
                                Arrow.AF_DB)],
        }

    check = verify_patient_record(record)
    if not check.passed:
        return {
            "control_state": State.RESOLVING_IDENTITY.value,
            "retry_count": _bump(state, "crm"),
            "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                                "discard_output", "; ".join(check.violations),
                                Arrow.V_RETRY)],
        }

    found = status == "found"
    return {
        "control_state": State.RESOLVING_IDENTITY.value,
        "crm_status": status,
        "patient_history": check.checked if found else None,
        "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                            "fetch_patient_data",
                            "record found" if found else "new patient, no history",
                            Arrow.CRM_FOUND if found else Arrow.CRM_NEW)],
    }


def crm_fallback(state: TriageState, reason: str = "") -> dict[str, Any]:
    """AF·db — the CRM client raised rather than returning a status.

    Same outcome as the db_error branch inside `resolving_identity`: continue on
    intake-only data. Split into its own function so the error handler and the
    inline branch cannot drift apart.
    """
    return {
        "control_state": State.RESOLVING_IDENTITY.value,
        "crm_status": "db_error",
        "degraded": ["crm"],
        "flags": ["crm_down_intake_only"],
        "audit_log": [audit(state.case_id, State.RESOLVING_IDENTITY,
                            "alert_technician",
                            "CRM unreachable, degraded"
                            + (f" ({reason})" if reason else ""),
                            Arrow.AF_DB)],
    }


# ---- redacting_routing (arrows 5, 6, V·halt·PII, AF·PII) --------------------


def redacting_routing(state: TriageState) -> dict[str, Any]:
    """Build the model-facing payload and prove it carries no identifiers.

    Critical, fail-closed. The identifier drop is deterministic schema-drop; the
    check after it is the OPA no-identifiers invariant. A leak here is structural,
    never retried.
    """
    drop_free_text = "pii_bert_ner" in state.degraded
    payload = normalizer.build_model_payload(
        state.case_id, state.parsed_fields, state.patient_history,
        drop_free_text=drop_free_text,
    )

    check = verify_redacted_payload(payload)
    if not check.passed:
        arrow = Arrow.V_HALT_PII if check.structural else Arrow.V_RETRY
        return {
            "control_state": State.REDACTING_ROUTING.value,
            "retry_count": _bump(state, "pii_schema_drop"),
            "failed_stage": State.REDACTING_ROUTING.value,
            "audit_log": [audit(state.case_id, State.REDACTING_ROUTING,
                                "alert_technician", "; ".join(check.violations),
                                arrow)],
        }

    try:
        scores = normalizer.score_urgency(payload)
        # model_dump(): LangGraph warns that checkpointing custom classes will
        # be blocked in a future version. Pydantic re-validates the dict on the
        # way back in, so the field stays typed.
        scorer_update: dict[str, Any] = {"urgency_scores": scores.model_dump()}
    except normalizer.ScorerUnavailable as exc:
        # Fail-open, safe-drop: no unredacted prose reaches the model.
        scorer_update = {
            "degraded": ["pii_bert_ner"],
            "flags": ["urgency_scores_unavailable"],
            "audit_log": [audit(state.case_id, State.REDACTING_ROUTING,
                                "alert_technician", str(exc), Arrow.AF_PII)],
        }

    audit_records = [
        audit(state.case_id, State.REDACTING_ROUTING, "build_model_payload",
              "build model payload", Arrow.BUILD_PAYLOAD),
        audit(state.case_id, State.REDACTING_ROUTING, "emit_event_log",
              "payload clean", Arrow.PAYLOAD_CLEAN),
    ]
    return {
        "control_state": State.REDACTING_ROUTING.value,
        "redacted_payload": check.checked,
        **scorer_update,
        "audit_log": audit_records + scorer_update.get("audit_log", []),
    }


# ---- classifying (arrows 7, 8, V·retry/exhausted·classifier) ----------------


def classifying(state: TriageState) -> dict[str, Any]:
    """The single LLM step. A red-flag match settles the level without the model."""
    invoked = audit(state.case_id, State.CLASSIFYING, "invoke_acuity_classifier",
                    "run classifier (red-flag pre-check, then model)",
                    Arrow.RUN_CLASSIFIER)
    proposal = acuity_classifier.classify(state.redacted_payload)
    check = verify_schema("acuity_classifier", proposal, type(proposal))

    if not check.passed:
        return {
            "control_state": State.CLASSIFYING.value,
            "retry_count": _bump(state, "acuity_classifier"),
            "audit_log": [invoked,
                          audit(state.case_id, State.CLASSIFYING, "discard_output",
                                "; ".join(check.violations),
                                Arrow.V_RETRY_CLASSIFIER)],
        }

    checked = check.checked
    return {
        "control_state": State.CLASSIFYING.value,
        "system_proposed_acuity": checked.system_proposed_acuity,
        "confidence": checked.confidence,
        "acuity_source": checked.acuity_source,
        "red_flag_fired": checked.acuity_source == "rule_forced",
        "audit_log": [invoked,
                      audit(state.case_id, State.CLASSIFYING, "emit_event_log",
                            f"acuity proposed: {checked.system_proposed_acuity} "
                            f"({checked.acuity_source})", Arrow.ACUITY_PROPOSED)],
    }


def classifier_fallback(state: TriageState, reason: str = "") -> dict[str, Any]:
    """AF·classifier / V·exhausted·classifier.

    Drop the system acuity, fall back to the nurse's, disable the discrepancy gate
    for the outage, and flag the case for later review.
    """
    nurse = state.nurse_proposed_acuity
    arrival = state.arrival_time or now_iso()
    update: dict[str, Any] = {
        "control_state": State.CLASSIFYING.value,
        "system_proposed_acuity": None,
        "gate_disabled": True,
        "degraded": ["acuity_classifier"],
        "flags": ["cross_check_off_review_later"],
        "audit_log": [audit(state.case_id, State.CLASSIFYING, "fallback_manual",
                            "classifier unusable, using nurse acuity; gate disabled"
                            + (f" ({reason})" if reason else ""),
                            Arrow.V_EXHAUSTED_CLASSIFIER)],
    }
    if nurse is not None:
        update |= {
            "acuity": nurse,
            "acuity_source": AcuitySource.HUMAN_CONFIRMED.value,
            "acuity_bucket": bucket_for(nurse).value,
            "order_key": assign_order_key(nurse, arrival),
        }
    return update


# ---- acuity_proposed (arrows 9a / 9b / 9c) ----------------------------------


def acuity_proposed(state: TriageState) -> dict[str, Any]:
    """Deterministic gap resolution over the Z3-proven bands."""
    nurse = state.nurse_proposed_acuity
    system = state.system_proposed_acuity
    arrival = state.arrival_time or now_iso()

    gap = compute_acuity_gap(nurse, system)
    final, source, arrow = resolve_acuity(nurse, system)

    if final is None:      # 9c — charge nurse decides, no acuity settled here
        return {
            "control_state": State.ACUITY_PROPOSED.value,
            "acuity_gap": gap,
            "escalation_reason": human_bridge.DISCREPANCY,
            "audit_log": [audit(state.case_id, State.ACUITY_PROPOSED,
                                "invoke_human_escalation",
                                "gap >= 2, charge nurse decides", arrow)],
        }

    return {
        "control_state": State.ACUITY_PROPOSED.value,
        "acuity": final,
        "acuity_gap": gap,
        "acuity_source": source.value if source else None,
        "acuity_bucket": bucket_for(final).value,
        "order_key": assign_order_key(final, arrival),
        "audit_log": [audit(state.case_id, State.ACUITY_PROPOSED,
                            "assign_order_key", f"acuity settled: {final}", arrow)],
    }


# ---- safety_validating (arrows 10, 10·fail, AF·safety) ----------------------


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


# ---- verdict_proposed (arrows 11 / 11·pass) ---------------------------------


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


# ---- awaiting_human_approval (9c / 10·fail / 1b.z·*) ------------------------


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


# ---- terminals ----------------------------------------------------------------


def monitoring(state: TriageState) -> dict[str, Any]:
    """Cleared to the queue. This slice ends here."""
    return {
        "control_state": State.MONITORING.value,
        "approved": True,
        "clinical_status": ClinicalStatus.WAITING.value,
        "audit_log": [
            audit(state.case_id, State.MONITORING, "emit_event_log",
                  "cleared to queue", Arrow.CLEARED_TO_QUEUE),
            audit(state.case_id, State.MONITORING, "start_reassessment_timer",
                  "queued, timer running", Arrow.TIMER_RUNNING),
        ],
    }


def agent_failed(state: TriageState) -> dict[str, Any]:
    """Halt. Leaves only on AGENT_RECOVERED, which re-enters at the failed stage."""
    return {
        "control_state": State.AGENT_FAILED.value,
        "audit_log": [audit(state.case_id, State.AGENT_FAILED, "alert_technician",
                            f"halted at {state.failed_stage or 'unknown stage'}; "
                            "awaiting AGENT_RECOVERED", Arrow.AF_RECOVER)],
    }


def action_denied(state: TriageState) -> dict[str, Any]:
    """The BLK row. Writes no case state — only the refusal record.

    The case stays exactly where it was; a later event re-enters the graph.
    """
    return {
        "control_state": State.ACTION_DENIED.value,
        "audit_log": [audit(state.case_id, State.ACTION_DENIED, "emit_event_log",
                            "attempted action refused; case did not move", Arrow.BLK)],
    }
