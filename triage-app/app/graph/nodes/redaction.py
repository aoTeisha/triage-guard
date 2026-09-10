"""redacting_routing — build the model-facing payload (arrows 5, 6, V·halt·PII, AF·PII)."""

from __future__ import annotations

from typing import Any

from app.actors import normalizer
from app.deterministic import audit
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Arrow
from app.states import State
from app.verification import verify_redacted_payload


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
