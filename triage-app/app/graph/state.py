"""Shared graph state — the Data / Control / World planes as one model.

Mirrors the "Context / State variables" table in docs/SPECIFICATION.md. Every node
reads this and returns a partial update; LangGraph merges the update into the
checkpointed state. Nodes never mutate `state` in place — returning a dict is what
makes a step replayable, and replay is what `interrupt()` and time-travel rely on.

Accumulating fields carry reducers so that a node returning `{"audit_log": [rec]}`
*appends*. Without a reducer the last writer would clobber the trail, which would
quietly break the append-only auditability constraint
(docs/SYSTEM_MODELING.md § Hard Constraints).
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field

from app.schemas import SafetyVerdict
from app.states import AcuityBucket, AcuitySource, ClinicalStatus, State


def merge_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Reducer for `retry_count`: per-agent max, so a replayed node cannot
    double-count an attempt and a concurrent branch cannot lose one.
    """
    merged = dict(left)
    for agent, count in right.items():
        merged[agent] = max(merged.get(agent, 0), count)
    return merged


class UrgencyScores(BaseModel):
    """BERT-scored distress/pain. Mock zeros until the scorer is wired in."""

    sentiment: float = 0.0
    distress: float = 0.0
    pain: float = 0.0


class TriageState(BaseModel):
    """One case, across all three planes."""

    # ---- Control plane -------------------------------------------------------
    case_id: str = ""
    channel: str = "website"
    control_state: State = State.INTAKE_RECEIVED
    retry_count: Annotated[dict[str, int], merge_counts] = Field(default_factory=dict)
    correction_rounds: int = 0

    # ---- Raw + parsed intake (Data plane) -----------------------------------
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    parsed_fields: dict[str, Any] = Field(default_factory=dict)
    intake_outcome: Optional[str] = None
    intake_reason: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)

    # ---- Identity / history (held on the case, never in the model payload) --
    stable_patient_id: Optional[str] = None
    patient_history: Optional[dict[str, Any]] = None
    crm_status: Optional[str] = None

    # ---- Redacted, model-facing payload -------------------------------------
    redacted_payload: dict[str, Any] = Field(default_factory=dict)
    urgency_scores: UrgencyScores = Field(default_factory=UrgencyScores)

    # ---- Acuity ---------------------------------------------------------------
    nurse_proposed_acuity: Optional[int] = None
    system_proposed_acuity: Optional[int] = None
    confidence: Optional[float] = None
    acuity: Optional[int] = None            # final, settled acuity only
    acuity_gap: Optional[int] = None
    acuity_source: Optional[AcuitySource] = None
    # `acuity_source` is defined as the provenance of the FINAL acuity, so 9b/9c
    # legitimately overwrite `rule_forced`. But § 639 wants the red-flag fact kept
    # "so red-flag firing rates can be tuned" — one field cannot do both jobs.
    red_flag_fired: bool = False
    # Set when the classifier is down: the discrepancy gate is disabled for the
    # outage and the case is flagged "cross-check off" (AF·classifier).
    gate_disabled: bool = False

    # ---- Safety + approval ----------------------------------------------------
    safety_verdict: Optional[SafetyVerdict] = None
    safety_passed: bool = False
    approved: bool = False

    # ---- World plane -----------------------------------------------------------
    clinical_status: Optional[ClinicalStatus] = None
    acuity_bucket: Optional[AcuityBucket] = None
    order_key: Optional[tuple[int, str]] = None
    arrival_time: Optional[str] = None

    # ---- Human gate -------------------------------------------------------------
    escalation_reason: Optional[str] = None      # "discrepancy" | "safety_fail"
    human_decision: Optional[str] = None
    resolver_role: Optional[str] = None
    # Role of whoever is driving the current attempted action, for OPA checks.
    actor_role: str = "nurse"

    # ---- Degrade / failure bookkeeping ---------------------------------------
    degraded: Annotated[list[str], operator.add] = Field(default_factory=list)
    flags: Annotated[list[str], operator.add] = Field(default_factory=list)
    failed_stage: Optional[State] = None      # for AF·recover / resume_at_failed_stage

    # ---- Audit trail (append-only) --------------------------------------------
    audit_log: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)
