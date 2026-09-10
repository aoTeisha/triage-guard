"""TriageFlow — the deterministic CrewAI Flow spine (SPECIFICATION.md § Transitions).

This is the workflow the spec asks for: "The system runs as a CrewAI Flow: a
deterministic workflow whose steps are wired in code, supported by specialist
agents and services." Each @start/@listen/@router method is a control-plane
step. The guards decide routing in code; the ONE LLM call (the Acuity
Classifier) sits behind invoke_acuity_classifier; everything else is
deterministic.

Skeleton stance:
  - Runs end-to-end with mock input/output and no external services.
  - Routers implement the branch points (intake outcomes, acuity gap, safety
    verdict) so the shape is real even though the producers are mocked.
  - The happy path (demo case 1) plus the four intake branches and the acuity
    gate are wired. Monitoring/treatment-move/release are represented as a
    terminal step to keep the skeleton small — extend from here.

Every step calls emit_event_log, so a run leaves a full audit trail you can
print. Nothing writes to state except the Flow steps themselves.
"""

from __future__ import annotations

from crewai.flow.flow import Flow, listen, or_, router, start

from app.flow import agents
from app.flow.deterministic import (
    assign_order_key,
    bucket_for,
    compute_acuity_gap,
    emit_event_log,
    now_iso,
    resolve_acuity,
    verify_no_identifiers,
    verify_output,
)
from app.flow.state import SafetyVerdict, TriageState
from app.guards import classify_intake_payload

# ---------------------------------------------------------------------------


class TriageFlow(Flow[TriageState]):
    """The control-plane pipeline. State is the shared context between steps."""

    # ---- intake_received → parsing (arrows 1a, 2) --------------------------
    @start()
    def intake_received(self) -> None:
        s = self.state
        s.control_state = "intake_received"
        s.arrival_time = s.arrival_time or now_iso()
        # order_key is assigned at entry even before acuity — a provisional
        # queued bucket, re-keyed once acuity is settled.
        s.order_key = assign_order_key(s.nurse_proposed_acuity or 5, s.arrival_time)
        emit_event_log(s, "route_channel", "new case entered", arrow="1a")
        emit_event_log(s, "emit_event_log", "input normalized", arrow="2")

    # ---- parsing: run the DETERMINISTIC intake validator (arrow 3) ---------
    @router(intake_received)
    def parsing(self) -> str:
        s = self.state
        s.control_state = "parsing"
        # Deterministic four-way classification from the guards. No LLM.
        event = classify_intake_payload(s.raw_payload)
        s.intake_outcome = event
        emit_event_log(s, "invoke_intake_parser", f"intake outcome: {event}", arrow="3")

        if event == "DATA_PARSED":
            s.parsed_fields = dict(s.raw_payload)
            s.stable_patient_id = s.parsed_fields.get("stable_patient_id")
            s.nurse_proposed_acuity = s.parsed_fields.get("nurse_proposed_acuity")
            return "route_data_parsed"
        if event == "MISSING_FIELDS_DETECTED":
            return "route_missing_fields"
        if event == "SUBMISSION_FAILED":
            return "route_submission_failed"
        return "route_input_rejected"   # INVALID_INPUT_DETECTED

    # ---- the three non-happy intake branches (arrows 16 / 17 / 18) ---------
    @listen("route_missing_fields")
    def missing_fields_requested(self) -> None:
        s = self.state
        s.control_state = "missing_fields_requested"
        emit_event_log(s, "notify_user", "request fields", arrow="16")

    @listen("route_submission_failed")
    def submission_failed(self) -> None:
        s = self.state
        s.control_state = "submission_failed"
        emit_event_log(s, "notify_user", "resubmit or manual", arrow="17")

    @listen("route_input_rejected")
    def input_rejected(self) -> None:
        s = self.state
        s.control_state = "input_rejected"
        emit_event_log(s, "notify_user", "invalid input", arrow="18", security=True)

    # ---- data_parsed → resolving_identity (arrow 4b), DETERMINISTIC CRM ----
    @listen("route_data_parsed")
    def resolving_identity(self) -> None:
        s = self.state
        s.control_state = "resolving_identity"
        result = agents.fetch_patient_data(s.stable_patient_id or "")
        s.crm_status = result["status"]
        if result["status"] == "found":
            s.patient_history = result["record"]
            emit_event_log(s, "fetch_patient_data", "record found", arrow="4b·found")
        elif result["status"] == "not_found":
            emit_event_log(s, "fetch_patient_data", "new patient, no history", arrow="4b·new")
        else:  # db_error → fail-open degrade
            s.degraded.append("crm")
            s.flags.append("crm_down_intake_only")
            emit_event_log(
                s, "alert_technician", "DB unreachable, degraded", arrow="AF·db"
            )

    # ---- redacting_routing: DETERMINISTIC schema-drop + OPA check ----------
    @router(resolving_identity)
    def redacting_routing(self) -> str:
        s = self.state
        s.control_state = "redacting_routing"
        payload, scores = agents.build_model_payload(s.parsed_fields, s.patient_history)
        payload["case_id"] = s.case_id
        s.redacted_payload = payload
        s.urgency_scores = scores
        emit_event_log(s, "build_model_payload", "build model payload", arrow="5")

        ok, why = verify_no_identifiers(payload)
        if not ok:
            s.control_state = "agent_failed"
            emit_event_log(s, "alert_technician", why, arrow="V·halt·PII")
            return "halt"
        emit_event_log(s, "emit_event_log", "payload clean", arrow="6")
        return "classify"

    # ---- classifying: the ONE LLM step (arrows 7 → 8) ----------------------
    @listen("classify")
    def classifying(self) -> None:
        s = self.state
        s.control_state = "classifying"
        proposal = agents.invoke_acuity_classifier(s.redacted_payload)

        ok, why = verify_output("acuity_classifier", proposal)
        if not ok:
            s.degraded.append("acuity_classifier")
            emit_event_log(s, "fallback_manual", why, arrow="V·exhausted·classifier")
            return
        s.system_proposed_acuity = proposal["system_proposed_acuity"]
        s.confidence = proposal["confidence"]
        s.acuity_source = proposal["acuity_source"]
        emit_event_log(
            s, "invoke_acuity_classifier",
            f"acuity proposed: {s.system_proposed_acuity} ({s.acuity_source})",
            arrow="8",
        )

    # ---- acuity_proposed: DETERMINISTIC gap resolution (9a/9b/9c) ----------
    @router(classifying)
    def acuity_proposed(self) -> str:
        s = self.state
        s.control_state = "acuity_proposed"
        nurse = s.nurse_proposed_acuity or 5
        system = s.system_proposed_acuity or nurse
        s.acuity_gap = compute_acuity_gap(nurse, system)

        final, source, arrow = resolve_acuity(nurse, system)
        if arrow == "9c":   # gap >= 2 → charge nurse decides
            s.escalation_reason = "discrepancy"
            emit_event_log(s, "invoke_human_escalation", "gap >=2, charge nurse decides", arrow="9c")
            return "escalate_acuity"

        s.acuity = final
        s.acuity_source = source if source != "human_confirmed" else s.acuity_source or source
        s.acuity_bucket = bucket_for(final)
        s.order_key = assign_order_key(final, s.arrival_time or now_iso())
        emit_event_log(s, "assign_order_key", f"acuity settled: {final} ({arrow})", arrow=arrow)
        return "safety"

    # ---- awaiting_human_approval (acuity discrepancy branch, 1b.z·acuity) --
    @listen("escalate_acuity")
    def gate_acuity(self) -> str:
        s = self.state
        s.control_state = "awaiting_human_approval"
        s.clinical_status = "human_review"
        response = agents.invoke_human_escalation(s, "discrepancy")
        s.human_decision = response["decision"]
        chosen = (
            s.nurse_proposed_acuity if response["decision"] == "use_nurse_acuity"
            else s.system_proposed_acuity
        )
        s.acuity = chosen
        s.acuity_source = "human_confirmed"
        s.acuity_bucket = bucket_for(chosen or 5)
        s.order_key = assign_order_key(chosen or 5, s.arrival_time or now_iso())
        s.approved = True
        emit_event_log(s, "apply_human_acuity", "charge nurse resolved acuity", arrow="1b.z·acuity")
        return "safety"

    # ---- safety_validating: DETERMINISTIC verdict (arrows 9 → 10 / 10·fail)-
    @router(or_("safety", gate_acuity))
    def safety_validating(self) -> str:
        s = self.state
        s.control_state = "safety_validating"
        verdict = agents.invoke_safety_validation(s)
        s.safety_verdict = SafetyVerdict(**verdict)
        if verdict["verdict"] == "pass":
            s.safety_passed = True
            emit_event_log(s, "emit_event_log", "safety passed", arrow="10")
            return "cleared"
        s.escalation_reason = "safety_fail"
        emit_event_log(s, "invoke_human_escalation", "safety failed, human decides", arrow="10·fail")
        return "safety_gate"

    # ---- verdict_proposed → monitoring (arrow 11·pass) ---------------------
    @listen("cleared")
    def monitoring(self) -> None:
        s = self.state
        s.control_state = "monitoring"
        s.approved = True
        s.clinical_status = "waiting"
        emit_event_log(s, "start_reassessment_timer", "cleared to queue", arrow="11·pass")

    # ---- safety-fail gate: correct-and-revalidate only (1b.z·safety) -------
    @listen("safety_gate")
    def gate_safety(self) -> None:
        s = self.state
        s.control_state = "awaiting_human_approval"
        s.clinical_status = "human_review"
        # MOCK correction; the real gate enforces that acuity / clinical_status
        # / safety_verdict actually changed, and re-runs safety (no override).
        agents.invoke_human_escalation(s, "safety_fail")
        emit_event_log(
            s, "apply_correction",
            "correct-and-revalidate (skeleton: not looping back)",
            arrow="1b.z·safety",
        )
