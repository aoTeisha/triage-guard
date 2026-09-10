"""Events that move the control plane (docs/SPECIFICATION.md § Events, § Transitions).

Events are what the Transitions table keys on. In the graph they are the return
values of the router functions in `graph/routers.py`, which is why they double as
conditional-edge labels: an edge map reads `{Event.DATA_PARSED: State.DATA_PARSED}`,
i.e. exactly one row of the spec's table.
"""

from __future__ import annotations

from enum import Enum


class Event(str, Enum):
    """One member per `Event` value in the Transitions table."""

    # ---- intake ------------------------------------------------------------
    CASE_SUBMITTED = "CASE_SUBMITTED"
    MESSAGE_NORMALIZED = "MESSAGE_NORMALIZED"
    DATA_PARSED = "DATA_PARSED"
    MISSING_FIELDS_DETECTED = "MISSING_FIELDS_DETECTED"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    INVALID_INPUT_DETECTED = "INVALID_INPUT_DETECTED"
    FIELDS_SUBMITTED = "FIELDS_SUBMITTED"

    # ---- identity / redaction / classification -----------------------------
    PATIENT_RESOLVED = "PATIENT_RESOLVED"
    REDACT_ROUTE_DONE = "REDACT_ROUTE_DONE"
    ACUITY_PROPOSED = "ACUITY_PROPOSED"

    # ---- safety / approval --------------------------------------------------
    VERDICT_PROPOSED = "VERDICT_PROPOSED"
    ESCALATION_PROPOSED = "ESCALATION_PROPOSED"
    APPROVAL_RESPONSE_RECEIVED = "APPROVAL_RESPONSE_RECEIVED"

    # ---- verification (§ On output verification) ---------------------------
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"

    # ---- failure / governance ----------------------------------------------
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_RECOVERED = "AGENT_RECOVERED"
    ACTION_DENIED = "ACTION_DENIED"
    EVENT_LOGGED = "EVENT_LOGGED"

    # ---- world plane (not wired in this slice) ------------------------------
    REASSESSMENT_TIMEOUT = "REASSESSMENT_TIMEOUT"
    DETERIORATION_DETECTED = "DETERIORATION_DETECTED"
    MOVE_REQUESTED = "MOVE_REQUESTED"
    TRANSITION_ACCEPTED = "TRANSITION_ACCEPTED"
    TREATMENT_COMPLETE = "TREATMENT_COMPLETE"
    RELEASE_REQUESTED = "RELEASE_REQUESTED"


class IntakeOutcome(str, Enum):
    """The four mutually exclusive results of validating one submission.

    A strict subset of `Event`, kept separate because the Intake Parser returns
    exactly one of these four and nothing else — the type says so.
    """

    DATA_PARSED = Event.DATA_PARSED.value
    MISSING_FIELDS_DETECTED = Event.MISSING_FIELDS_DETECTED.value
    SUBMISSION_FAILED = Event.SUBMISSION_FAILED.value
    INVALID_INPUT_DETECTED = Event.INVALID_INPUT_DETECTED.value
