"""Control-plane states (docs/SPECIFICATION.md § Transitions, "Current (control)").

This is the spec's vocabulary, not the graph's. It holds *every* control state the
Transitions table names, including the ones this skeleton does not wire yet — so the
enum stays a faithful transcription and `graph/build.py` declares the gap explicitly
rather than the gap being invisible. `tests/test_edges.py` enforces that split.

The `str` mixin matters: LangGraph node names are strings, so a `State` member can be
passed straight to `add_node` / `add_edge` and still print as its spec name.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """One member per `Current (control)` value in the Transitions table."""

    # ---- intake slice ------------------------------------------------------
    INTAKE_RECEIVED = "intake_received"
    PARSING = "parsing"
    DATA_PARSED = "data_parsed"
    MISSING_FIELDS_REQUESTED = "missing_fields_requested"
    SUBMISSION_FAILED = "submission_failed"
    INPUT_REJECTED = "input_rejected"

    # ---- identity, redaction, classification -------------------------------
    RESOLVING_IDENTITY = "resolving_identity"
    REDACTING_ROUTING = "redacting_routing"
    CLASSIFYING = "classifying"
    ACUITY_PROPOSED = "acuity_proposed"

    # ---- safety and the human gate -----------------------------------------
    SAFETY_VALIDATING = "safety_validating"
    VERDICT_PROPOSED = "verdict_proposed"
    AWAITING_HUMAN_APPROVAL = "awaiting_human_approval"

    # ---- world plane -------------------------------------------------------
    MONITORING = "monitoring"
    REASSESSMENT_REQUIRED = "reassessment_required"
    CASE_CLOSED = "case_closed"

    # ---- failure / refusal --------------------------------------------------
    AGENT_FAILED = "agent_failed"
    ACTION_DENIED = "action_denied"


class ClinicalStatus(str, Enum):
    """World-plane status shown on the board (§ Context / State variables)."""

    WAITING = "waiting"
    HUMAN_REVIEW = "human_review"
    REASSESSMENT_REQUIRED = "reassessment_required"
    TREATMENT_STARTED = "treatment_started"
    FORMAL_VALIDATION = "formal_validation"
    PATIENT_RELEASED = "patient_released"


class AcuitySource(str, Enum):
    """How the final acuity was settled. Locked once set (§ acuity_source)."""

    SYSTEM = "system"
    RULE_FORCED = "rule_forced"
    AUTO_RESOLVED = "auto_resolved"
    HUMAN_CONFIRMED = "human_confirmed"


class AcuityBucket(str, Enum):
    """Primary queue sort key (§ Queue ordering rule)."""

    EMERGENT = "emergent"   # ESI 1-2
    QUEUED = "queued"       # ESI 3-5
