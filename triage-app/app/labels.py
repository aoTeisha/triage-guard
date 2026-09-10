"""Arrow labels and internal route labels.

`Arrow` is spec traceability: every audit record carries the arrow from the
Transitions table, so a run's log can be diffed against docs/SPECIFICATION.md line
by line.

The numbering is not arbitrary. For each agent in the Actors table the arrows come
in an **invoke / propose** pair — the odd arrow calls the agent, the even one
records what it proposed back:

    3 / 4    Intake Parser          (4 splits into 16 / 17 / 18 by outcome)
    7 / 8    Acuity Classifier
    9a-c / 10  Safety Validation
    11 / 12  Human Escalation
    13 / 14  Waiting Room Monitor

This is why arrow 12 is a self-loop on `awaiting_human_approval` and why 11's
action is `invoke_human_escalation`: they are agent round-trips, not case
movements. Arrows that are neither (4b, 5, 6, 20x) are on-entry actions or
notifications named in the Actions column.

`Route` is graph-internal — branch labels for conditional edges that have no spec
event of their own (verification outcomes, authorization refusal).

Split from states/events because these change when the *graph wiring* changes,
whereas states and events change only when the spec's control plane does.
"""

from __future__ import annotations

from enum import Enum


class Arrow(str, Enum):
    """Arrow column of the Transitions table. Written to every audit record."""

    # ---- intake ------------------------------------------------------------
    ENTRY = "1a"
    RESUBMIT = "1a·resubmit"
    REJECTED_RETURN = "1a·rejected"
    NORMALIZED = "2"
    RUN_VALIDATOR = "3"
    SUBMISSION_VALID = "4"
    FIELDS_RESUBMITTED = "1b.x"
    MISSING_FIELDS = "16"
    SUBMISSION_UNUSABLE = "17"
    INVALID_INPUT = "18"

    # ---- identity ----------------------------------------------------------
    LOOKUP = "4b"
    CRM_FOUND = "4b·found"
    CRM_NEW = "4b·new"

    # ---- redaction / classification ----------------------------------------
    BUILD_PAYLOAD = "5"
    PAYLOAD_CLEAN = "6"
    RUN_CLASSIFIER = "7"
    ACUITY_PROPOSED = "8"

    # ---- acuity gate (Z3 band totality) -------------------------------------
    ACUITY_AGREE = "9a"
    ACUITY_GAP_MINOR = "9b"
    ACUITY_GAP_MAJOR = "9c"

    # ---- safety / approval ---------------------------------------------------
    SAFETY_PASSED = "10"
    SAFETY_FAILED = "10·fail"
    ESCALATION_NEEDED = "11"
    CLEARED_TO_QUEUE = "11·pass"
    ESCALATION_RECORDED = "12"
    APPROVAL_REQUESTED = "20"
    GATE_REMINDER_1 = "20a"
    GATE_REMINDER_2 = "20b"
    GATE_ACUITY_RESOLVED = "1b.z·acuity"
    GATE_SAFETY_CORRECTED = "1b.z·safety"

    # ---- monitoring / world plane (not wired in this slice) ------------------
    TIMER_RUNNING = "13"
    REASSESSMENT_DUE = "14"
    FRONT_DOOR_RERUN = "15"
    MOVE_AUTHORIZED = "1b.y"
    MOVE_CONFIRMED = "19"
    FORMAL_VALIDATION = "FV"
    RELEASE = "REL"

    # ---- agent failure (§ Per-agent failure model) ---------------------------
    AF_DB = "AF·db"
    AF_PII = "AF·PII"
    AF_CLASSIFIER = "AF·classifier"
    AF_SAFETY = "AF·safety"
    AF_HUMAN_BRIDGE = "AF·human_bridge"
    AF_RECOVER = "AF·recover"

    # ---- output verification (§ On output verification) ----------------------
    V_PASS = "V·pass"
    V_RETRY = "V·retry"
    V_EXHAUSTED = "V·exhausted"
    V_HALT = "V·halt"
    V_HALT_PII = "V·halt·PII"
    V_RETRY_CLASSIFIER = "V·retry·classifier"
    V_EXHAUSTED_CLASSIFIER = "V·exhausted·classifier"
    V_RETRY_SAFETY = "V·retry·safety"
    V_EXHAUSTED_SAFETY = "V·exhausted·safety"

    # ---- governance ----------------------------------------------------------
    BLK = "BLK"


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
