"""The shared state for one case as it moves through the graph, grouped into
three areas below: which workflow step it's at (Control plane), the intake
data it carries (Data plane), and where it sits in the physical queue
(World plane).

Every node reads this state and returns a partial update; LangGraph merges
that update into the checkpointed state. Nodes never mutate `state` in
place — returning a dict is what makes a step replayable, and replay is what
`interrupt()` (pausing a node) and time-travel debugging rely on.

Fields that accumulate over time (like `audit_log`) carry a reducer function
so that a node returning `{"audit_log": [rec]}` *appends* to the existing
list instead of replacing it. Without a reducer, the last node to write
would silently overwrite the whole trail instead of adding to it, breaking
the requirement that the audit log be append-only.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field

from app.schemas import SafetyVerdict
from app.states import AcuityBucket, AcuitySource, ClinicalStatus, State


def merge_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Merge function for `retry_count`: takes the max per agent, so replaying
    a node after a resume can't double-count an attempt, and a concurrent
    branch updating the same agent's count can't accidentally lose one.
    """
    merged = dict(left)
    for agent, count in right.items():
        merged[agent] = max(merged.get(agent, 0), count)
    return merged


class TriageState(BaseModel):
    """One case's full state as it moves through the graph."""

    # ---- Control plane: which workflow step the case is currently at ---------
    case_id: str = ""
    channel: str = "website"
    control_state: State = State.INTAKE_RECEIVED
    retry_count: Annotated[dict[str, int], merge_counts] = Field(default_factory=dict)
    correction_rounds: int = 0

    # ---- Data plane: the raw and parsed intake submission ---------------------
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    parsed_fields: dict[str, Any] = Field(default_factory=dict)
    intake_outcome: Optional[str] = None
    intake_reason: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)

    # ---- Identity / history (held on the case only — never sent to the model) --
    stable_patient_id: Optional[str] = None
    patient_history: Optional[dict[str, Any]] = None
    crm_status: Optional[str] = None

    # ---- The redacted payload actually sent to the acuity-classifying model ----
    redacted_payload: dict[str, Any] = Field(default_factory=dict)

    # ---- Acuity: how urgent the case is, and who decided ----------------------
    nurse_proposed_acuity: Optional[int] = None
    system_proposed_acuity: Optional[int] = None
    confidence: Optional[float] = None
    acuity: Optional[int] = None            # final, settled acuity only
    acuity_gap: Optional[int] = None
    acuity_source: Optional[AcuitySource] = None
    # Set when the acuity classifier is down. Disables the check that
    # compares the nurse's proposed acuity against the model's (there's no
    # model proposal to compare against), and flags the case as running
    # without that cross-check.
    gate_disabled: bool = False

    # ---- Safety validation + human approval ------------------------------------
    safety_verdict: Optional[SafetyVerdict] = None
    safety_passed: bool = False
    approved: bool = False

    # ---- World plane: where the case sits in the physical waiting queue --------
    clinical_status: Optional[ClinicalStatus] = None
    acuity_bucket: Optional[AcuityBucket] = None
    order_key: Optional[tuple[int, str]] = None
    arrival_time: Optional[str] = None
    # How many times a reassessment timer has fired for this case.
    # Incremented on every fire, so a later cycle never reuses an earlier
    # one's `timer_id` and its now-stale `due_at`.
    reassessment_cycle: int = 0

    # ---- Human approval gate ----------------------------------------------------
    escalation_reason: Optional[str] = None      # "discrepancy" | "safety_fail"
    human_decision: Optional[str] = None
    resolver_role: Optional[str] = None
    # Role of whoever is driving the current attempted action (e.g. "nurse",
    # "charge_nurse"), checked by the authorization policy before an action
    # like resolving the gate is allowed.
    actor_role: str = "nurse"

    # ---- Degrade / failure bookkeeping ---------------------------------------
    degraded: Annotated[list[str], operator.add] = Field(default_factory=list)
    flags: Annotated[list[str], operator.add] = Field(default_factory=list)
    # Which state the case was in when an agent crashed, so a recovery event
    # knows where to resume it from.
    failed_stage: Optional[State] = None

    # ---- Audit trail (append-only) --------------------------------------------
    audit_log: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)
