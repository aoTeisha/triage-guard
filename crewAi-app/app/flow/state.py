"""Shared Flow state — the Data / Control / World planes as one structured state.

Mirrors the "Context / State variables (Data plane)" table in
docs/SPECIFICATION.md. This is the object every step reads and the one place
each step writes its own results back to (`self.state`), exactly as the spec's
State rule requires: "the agents propose; they do not write. Each Flow step
calls its agent, reads the result, and writes it into the shared Flow state."

Everything here is a *skeleton*: fields carry mock defaults so the Flow runs
end-to-end with no LLM, no CRM, and no formal-layer engine wired in. Replace
the mock producers in flow/agents.py step by step; the state shape stays.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---- Data-plane enums (SPECIFICATION.md § Context / State variables) --------

AcuitySource = Literal["system", "rule_forced", "auto_resolved", "human_confirmed"]
AcuityBucket = Literal["emergent", "queued"]
ReleaseReason = Literal["discharge", "ama", "transfer", "admit"]
ClinicalStatus = Literal[
    "waiting",
    "human_review",
    "reassessment_required",
    "treatment_started",
    "formal_validation",
    "patient_released",
]


class UrgencyScores(BaseModel):
    """BERT-scored distress/pain — mock zeros until the scorer is wired in."""

    sentiment: float = 0.0
    distress: float = 0.0
    pain: float = 0.0


class SafetyVerdict(BaseModel):
    """Deterministic pass/fail verdict from the Safety Validation step."""

    verdict: Literal["pass", "fail"] = "pass"
    reasons: list[str] = Field(default_factory=list)


class TriageState(BaseModel):
    """The whole case, across all three planes. `id` is auto-added by the Flow."""

    # ---- Control plane -------------------------------------------------------
    case_id: str = ""
    channel: Literal["website"] = "website"
    control_state: str = "intake_received"
    retry_count: dict[str, int] = Field(default_factory=dict)

    # ---- Raw + parsed intake (Data plane) -----------------------------------
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    parsed_fields: dict[str, Any] = Field(default_factory=dict)
    intake_outcome: Optional[str] = None
    intake_reason: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)

    # ---- Identity / history (held on the case, dropped from model payload) --
    stable_patient_id: Optional[str] = None
    patient_history: Optional[dict[str, Any]] = None
    crm_status: Optional[Literal["found", "not_found", "db_error"]] = None

    # ---- Redacted, model-facing payload (no identifiers) --------------------
    redacted_payload: dict[str, Any] = Field(default_factory=dict)
    urgency_scores: UrgencyScores = Field(default_factory=UrgencyScores)

    # ---- Acuity (Data plane) ------------------------------------------------
    nurse_proposed_acuity: Optional[int] = None
    system_proposed_acuity: Optional[int] = None
    confidence: Optional[float] = None
    acuity: Optional[int] = None          # the final, resolved acuity (9a/9b/9c only)
    acuity_gap: Optional[int] = None
    acuity_source: Optional[AcuitySource] = None

    # ---- Safety + approval flags --------------------------------------------
    safety_verdict: Optional[SafetyVerdict] = None
    safety_passed: bool = False
    approved: bool = False

    # ---- World plane --------------------------------------------------------
    clinical_status: Optional[ClinicalStatus] = None
    acuity_bucket: Optional[AcuityBucket] = None
    order_key: Optional[tuple[int, str]] = None
    arrival_time: Optional[str] = None
    release_reason: Optional[ReleaseReason] = None

    # ---- Human gate ---------------------------------------------------------
    escalation_reason: Optional[Literal["discrepancy", "safety_fail"]] = None
    human_decision: Optional[str] = None

    # ---- Degrade / failure bookkeeping --------------------------------------
    degraded: list[str] = Field(default_factory=list)   # names of agents in fallback
    flags: list[str] = Field(default_factory=list)      # review flags for the board

    # ---- Audit trail (the emit_event_log stream) ----------------------------
    audit_log: list[dict[str, Any]] = Field(default_factory=list)
