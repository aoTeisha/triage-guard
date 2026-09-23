"""Transition labels and internal route labels.

`Transition` names every step a case can take — one member per transition in
docs/SPECIFICATION.md. Every audit record carries one under the `transition`
key, so a run's trail reads as a list of named steps ("missing_fields",
"cleared_to_queue") that can be checked against the spec. The value is
always the member name in lower case. The diagram's "Arrow" column and its
numbers stay in the spec's own vocabulary; the spec's own number-to-name
table maps a diagram arrow to the member it means.

For each agent in the Actors table the transitions come in an **invoke /
propose** pair — the first calls the agent, the second records what it
proposed back:

    RUN_VALIDATOR / SUBMISSION_VALID      Intake Parser (the propose half splits
                                          into MISSING_FIELDS / SUBMISSION_UNUSABLE /
                                          INVALID_INPUT by outcome)
    RUN_CLASSIFIER / ACUITY_PROPOSED      Acuity Classifier
    ACUITY_AGREE..GAP_MAJOR / SAFETY_PASSED  Safety Validation
    ESCALATION_NEEDED / ESCALATION_RECORDED  Human Escalation
    TIMER_RUNNING / REASSESSMENT_DUE      Waiting Room Monitor

This is why ESCALATION_RECORDED is a self-loop on `awaiting_human_approval`
and why ESCALATION_NEEDED's action is `invoke_human_escalation`: they are agent
round-trips, not case movements. Transitions that are neither (LOOKUP,
BUILD_PAYLOAD, PAYLOAD_CLEAN, APPROVAL_REQUESTED) are on-entry actions or
notifications named in the Actions column.

`Route` is graph-internal — branch labels for conditional edges that have no spec
event of their own (verification outcomes, authorization refusal).

Split from states/events because these change when the *graph wiring* changes,
whereas states and events change only when the spec's control plane does.
"""

from __future__ import annotations

from enum import Enum


class Transition(str, Enum):
    """One Transitions-table row. Written to every audit record's `transition` key."""

    # ---- intake ------------------------------------------------------------
    ENTRY = "entry"
    RESUBMIT = "resubmit"
    REJECTED_RETURN = "rejected_return"
    NORMALIZED = "normalized"
    RUN_VALIDATOR = "run_validator"
    SUBMISSION_VALID = "submission_valid"
    FIELDS_RESUBMITTED = "fields_resubmitted"
    MISSING_FIELDS = "missing_fields"
    SUBMISSION_UNUSABLE = "submission_unusable"
    INVALID_INPUT = "invalid_input"

    # ---- identity ----------------------------------------------------------
    LOOKUP = "lookup"
    CRM_FOUND = "crm_found"
    CRM_NEW = "crm_new"
    DUPLICATE_CASE = "duplicate_case"

    # ---- redaction / classification ----------------------------------------
    BUILD_PAYLOAD = "build_payload"
    PAYLOAD_CLEAN = "payload_clean"
    RUN_CLASSIFIER = "run_classifier"
    ACUITY_PROPOSED = "acuity_proposed"

    # ---- acuity gate (gap bands, I4) -----------------------------------------
    ACUITY_AGREE = "acuity_agree"
    ACUITY_GAP_MINOR = "acuity_gap_minor"
    ACUITY_GAP_MAJOR = "acuity_gap_major"

    # ---- safety / approval ---------------------------------------------------
    SAFETY_PASSED = "safety_passed"
    SAFETY_FAILED = "safety_failed"
    ESCALATION_NEEDED = "escalation_needed"
    CLEARED_TO_QUEUE = "cleared_to_queue"
    ESCALATION_RECORDED = "escalation_recorded"
    APPROVAL_REQUESTED = "approval_requested"
    GATE_ACUITY_RESOLVED = "gate_acuity_resolved"
    GATE_SAFETY_CORRECTED = "gate_safety_corrected"

    # ---- monitoring / world plane --------------------------------------------
    TIMER_RUNNING = "timer_running"
    REASSESSMENT_DUE = "reassessment_due"
    FRONT_DOOR_RERUN = "front_door_rerun"
    MOVE_AUTHORIZED = "move_authorized"
    MOVE_CONFIRMED = "move_confirmed"
    FORMAL_VALIDATION = "formal_validation"
    RELEASE = "release"

    # ---- agent failure (§ Per-agent failure model) ---------------------------
    AF_DB = "af_db"
    AF_PII = "af_pii"
    AF_CLASSIFIER = "af_classifier"
    AF_SAFETY = "af_safety"
    AF_HUMAN_BRIDGE = "af_human_bridge"
    AF_RECOVER = "af_recover"
    SENIOR_ESCALATION = "senior_escalation"   # correction loop handed to a shift lead (I8)

    # ---- output verification (§ On output verification) ----------------------
    V_PASS = "v_pass"
    V_RETRY = "v_retry"
    V_EXHAUSTED = "v_exhausted"
    V_HALT = "v_halt"
    V_HALT_PII = "v_halt_pii"
    V_RETRY_CLASSIFIER = "v_retry_classifier"
    V_EXHAUSTED_CLASSIFIER = "v_exhausted_classifier"
    V_RETRY_SAFETY = "v_retry_safety"
    V_EXHAUSTED_SAFETY = "v_exhausted_safety"

    # ---- governance ----------------------------------------------------------
    BLK = "blk"


class Route(str, Enum):
    """Conditional-edge labels with no spec event of their own.

    Used where the branch is an implementation concern (did verification pass?
    is the actor authorized?) rather than a domain event.
    """

    PROCEED = "proceed"
    DENIED = "denied"
    RETRY = "retry"
    EXHAUSTED = "exhausted"
    HALT = "halt"
    ESCALATE = "escalate"
    CLEARED = "cleared"
    MOVED = "moved"
    RELEASED = "released"
