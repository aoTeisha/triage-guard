"""redacting_routing — build the model-facing payload (BUILD_PAYLOAD, PAYLOAD_CLEAN,
PRIVACY_REFUSED, PRIVACY_GATE_DOWN)."""

from __future__ import annotations

from typing import Any

from app.actors import normalizer
from app.deterministic import audit
from app.graph.state import TriageState
from app.guards.identifiers import redact_identifiers
from app.labels import Transition
from app.states import State
from app.verification import Violation, verify_redacted_payload


def redacting_routing(state: TriageState) -> dict[str, Any]:
    """Build the model-facing payload and prove it carries no identifiers.

    The identifier drop is deterministic schema-drop; the check after it is the
    OPA no-identifiers invariant plus the regex scan. Whatever the check says,
    the model sees only a payload it approved:

    - approved: on to the classifier.
    - refused: a block, not a failure. The check did its job; the case goes on
      without the model, on the nurse's acuity, and the technician is told,
      because redaction or the data upstream missed something.
    - OPA could not answer: the same detour, as an outage.

    Only a crash of this step halts the case (`_on_redaction_error`).
    """
    payload = normalizer.build_model_payload(
        state.case_id, state.parsed_fields, state.patient_history, state.age_band,
    )

    check = verify_redacted_payload(payload)
    if check.category is Violation.UNAVAILABLE:
        # OPA could not answer. Keep the payload (the board and the CRM
        # write-back read its complaint and vitals), but it is not proven clean,
        # so the model is skipped and the case settles on the nurse's acuity.
        return {
            "control_state": State.REDACTING_ROUTING.value,
            "redacted_payload": check.checked,
            "payload_unverified": True,
            "payload_refused": False,
            "degraded": ["opa"],
            "audit_log": [audit(state.case_id, State.REDACTING_ROUTING,
                                "alert_technician",
                                "; ".join(check.violations) + "; model skipped",
                                Transition.PRIVACY_GATE_DOWN, engines=["OPA"])],
        }
    if not check.passed:
        # Refused. Nothing of the refused payload is kept: not for the model, not
        # for the board, not for the CRM write-back, which reads its complaint
        # and vitals. That also drops any payload a previous triage left, so
        # routing reads this one. The case's own fields still hold everything
        # the rest of the triage needs.
        # OPA's reasons quote the value they refused; the record keeps the
        # reason, not the identifier.
        why = redact_identifiers("; ".join(check.violations))
        layers = list(check.layers)
        engines = {"engines": ["OPA"]} if any(layer.startswith("OPA") for layer in layers) else {}
        return {
            "control_state": State.REDACTING_ROUTING.value,
            "redacted_payload": {},
            "payload_unverified": False,
            "payload_refused": True,
            "flags": ["privacy_refused"],
            "audit_log": [audit(state.case_id, State.REDACTING_ROUTING,
                                "alert_technician", why + "; model skipped",
                                Transition.PRIVACY_REFUSED,
                                denying_layer=" + ".join(layers), **engines)],
        }

    audit_records = [
        audit(state.case_id, State.REDACTING_ROUTING, "build_model_payload",
              "build model payload", Transition.BUILD_PAYLOAD),
        audit(state.case_id, State.REDACTING_ROUTING, "emit_event_log",
              "payload clean", Transition.PAYLOAD_CLEAN, engines=["OPA"]),
    ]
    return {
        "control_state": State.REDACTING_ROUTING.value,
        "redacted_payload": check.checked,
        "payload_unverified": False,
        "payload_refused": False,
        "audit_log": audit_records,
    }
