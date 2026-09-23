"""intake_received, parsing, the three non-happy intake outcomes (ENTRY,
NORMALIZED, RUN_VALIDATOR, MISSING_FIELDS, SUBMISSION_UNUSABLE, INVALID_INPUT),
the pause that completes an incomplete intake (FIELDS_RESUBMITTED, RESUBMIT),
and the data_parsed pass-through (SUBMISSION_VALID).
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.actors import intake
from app.deterministic import assign_order_key, audit, now_iso
from app.graph.nodes._shared import _bump, is_release, release_case
from app.guards import NURSE_SUPPLIED_FIELDS
from app.graph.state import TriageState
from app.labels import Transition
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
                  "new case entered", Transition.ENTRY),
            audit(state.case_id, State.INTAKE_RECEIVED, "emit_event_log",
                  "input normalized", Transition.NORMALIZED),
        ],
    }

    # order_key needs an acuity. The only acuity
    # available at entry is the nurse's, which guards/fields.py states is mandatory
    # and "never inferred: absent means MISSING_FIELDS, never a guessed value". So when it
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
                                "; ".join(check.violations), Transition.V_RETRY)],
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
                  f"intake outcome: {parsed.outcome.value}", Transition.RUN_VALIDATOR),
        ],
    }


def missing_fields_requested(state: TriageState) -> dict[str, Any]:
    return {
        "control_state": State.MISSING_FIELDS_REQUESTED.value,
        "audit_log": [audit(state.case_id, State.MISSING_FIELDS_REQUESTED,
                            "notify_user", "request fields", Transition.MISSING_FIELDS,
                            missing_fields=state.missing_fields)],
    }


def submission_failed(state: TriageState) -> dict[str, Any]:
    return {
        "control_state": State.SUBMISSION_FAILED.value,
        "audit_log": [audit(state.case_id, State.SUBMISSION_FAILED, "notify_user",
                            "resubmit or manual", Transition.SUBMISSION_UNUSABLE)],
    }


def awaiting_intake_fix(state: TriageState) -> dict[str, Any]:
    """Pause until the nurse supplies what intake lacked (FIELDS_RESUBMITTED
    and RESUBMIT), then re-parse the same case. Continuing the same case keeps
    its arrival time, so the patient keeps their place in line (I2, I10).
    """
    submitted = interrupt({"case_id": state.case_id, "intake_fix_pending": True,
                           "missing_fields": state.missing_fields})
    if is_release(submitted):
        return release_case(state, submitted, state.control_state)
    # Only empty form fields may be filled. Overwriting one that already has a
    # value could swap the patient's identity (`stable_patient_id`) mid-case.
    accepted = {k: v for k, v in (submitted or {}).items()
                if k in NURSE_SUPPLIED_FIELDS and state.raw_payload.get(k) is None}
    ignored = sorted(set(submitted or {}) - set(accepted))
    return {
        "raw_payload": {**state.raw_payload, **accepted},
        "audit_log": [audit(state.case_id, state.control_state, "fields_submitted",
                            "nurse supplied the missing intake fields",
                            Transition.FIELDS_RESUBMITTED, ignored_fields=ignored)],
    }


def input_rejected(state: TriageState) -> dict[str, Any]:
    return {
        "control_state": State.INPUT_REJECTED.value,
        "audit_log": [audit(state.case_id, State.INPUT_REJECTED, "notify_user",
                            "invalid input", Transition.INVALID_INPUT,
                            security=True, reason=state.intake_reason)],
    }


def data_parsed(state: TriageState) -> dict[str, Any]:
    """Pass-through state from the Transitions table. It carries no logic of its
    own — it exists so the audit trail shows SUBMISSION_VALID before LOOKUP, matching the
    spec rather than collapsing two documented steps into one.
    """
    return {
        "control_state": State.DATA_PARSED.value,
        "audit_log": [
            audit(state.case_id, State.DATA_PARSED, "emit_event_log",
                  "submission valid", Transition.SUBMISSION_VALID),
            audit(state.case_id, State.DATA_PARSED, "fetch_patient_data",
                  "look up record", Transition.LOOKUP),
        ],
    }
