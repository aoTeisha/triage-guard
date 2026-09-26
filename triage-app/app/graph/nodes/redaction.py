"""redacting_routing — build the model-facing payload (BUILD_PAYLOAD, PAYLOAD_CLEAN, V_HALT_PII)."""

from __future__ import annotations

from typing import Any

from app.actors import normalizer
from app.deterministic import audit
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Transition
from app.states import State
from app.verification import verify_redacted_payload


def redacting_routing(state: TriageState) -> dict[str, Any]:
    """Build the model-facing payload and prove it carries no identifiers.

    Critical, fail-closed. The identifier drop is deterministic schema-drop; the
    check after it is the OPA no-identifiers invariant. A leak here is structural,
    never retried.
    """
    payload = normalizer.build_model_payload(
        state.case_id, state.parsed_fields, state.patient_history, state.age_band,
    )

    check = verify_redacted_payload(payload)
    if not check.passed:
        transition = Transition.V_HALT_PII if check.structural else Transition.V_RETRY
        return {
            "control_state": State.REDACTING_ROUTING.value,
            "retry_count": _bump(state, "pii_schema_drop"),
            "failed_stage": State.REDACTING_ROUTING.value,
            "audit_log": [audit(state.case_id, State.REDACTING_ROUTING,
                                "alert_technician", "; ".join(check.violations),
                                transition)],
        }

    audit_records = [
        audit(state.case_id, State.REDACTING_ROUTING, "build_model_payload",
              "build model payload", Transition.BUILD_PAYLOAD),
        audit(state.case_id, State.REDACTING_ROUTING, "emit_event_log",
              "payload clean", Transition.PAYLOAD_CLEAN),
    ]
    return {
        "control_state": State.REDACTING_ROUTING.value,
        "redacted_payload": check.checked,
        "audit_log": audit_records,
    }
