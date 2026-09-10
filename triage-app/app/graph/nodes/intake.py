"""intake_received, parsing, and the three non-happy intake terminals
(arrows 1a, 2, 3, 16, 17, 18) plus the data_parsed pass-through (arrow 4).
"""

from __future__ import annotations

from typing import Any

from app.actors import intake
from app.deterministic import assign_order_key, audit, now_iso
from app.graph.nodes._shared import _bump
from app.graph.state import TriageState
from app.labels import Arrow
from app.states import State
from app.verification import verify_schema


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
