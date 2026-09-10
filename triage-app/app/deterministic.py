"""Deterministic actions and derivations (docs/SPECIFICATION.md § Actions).

Plain, testable code: no LLM, no network, no clock dependence beyond an injected
timestamp. These are the parts the spec marks deterministic — queue ordering,
acuity-gap resolution, the audit append, the symbolic predicates — and the
neuro-symbolic split forbids routing any of them through a model.

Nothing here touches the graph. Functions take values and return values, so every
rule in this file is unit-testable without building a StateGraph.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.labels import Arrow
from app.states import AcuityBucket, AcuitySource, State

# ---- order_key / bucket (§ Queue ordering rule) -----------------------------


def bucket_for(acuity: int) -> AcuityBucket:
    """emergent = ESI 1-2, queued = ESI 3-5. The primary sort key."""
    return AcuityBucket.EMERGENT if acuity <= 2 else AcuityBucket.QUEUED


def assign_order_key(acuity: int, arrival_time: str) -> tuple[int, str]:
    """The single writer of order_key: (bucket_rank, arrival_time).

    bucket_rank 0 (emergent) always sorts ahead of 1 (queued); arrival_time breaks
    ties within a bucket. Re-keyed whenever acuity changes and never by the timer
    alone, so "only a real acuity change reorders you" holds.
    """
    rank = 0 if bucket_for(acuity) is AcuityBucket.EMERGENT else 1
    return (rank, arrival_time)


# ---- acuity-gap resolution at the gate (arrows 9a / 9b / 9c) ----------------


def compute_acuity_gap(nurse: int, system: int) -> int:
    return abs(nurse - system)


def resolve_acuity(nurse: int, system: int) -> tuple[int | None, AcuitySource | None, Arrow]:
    """Return (final_acuity, acuity_source, arrow) per the Z3-proven bands.

        gap 0   -> agree, keep it              (9a, human_confirmed)
        gap 1   -> take the MORE acute (min)   (9b, auto_resolved)
        gap >=2 -> charge nurse decides        (9c, unresolved)

    The bands are total and exclusive (Z3, band totality), so exactly one arm fires
    for every gap >= 0. The 9c arm returns None rather than a sentinel number: there
    is no final acuity yet, and a placeholder integer here would be indistinguishable
    from a real ESI level downstream.
    """
    gap = compute_acuity_gap(nurse, system)
    if gap == 0:
        return nurse, AcuitySource.HUMAN_CONFIRMED, Arrow.ACUITY_AGREE
    if gap == 1:
        return min(nurse, system), AcuitySource.AUTO_RESOLVED, Arrow.ACUITY_GAP_MINOR
    return None, None, Arrow.ACUITY_GAP_MAJOR


# ---- audit (emit_event_log — every transition) ------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit(
    case_id: str,
    control_state: State,
    action: str,
    explanation: str,
    arrow: Arrow | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build one trace record.

    Returns the record rather than appending it: nodes hand it back inside their
    state update, and the `audit_log` reducer appends. That keeps the trail correct
    when LangGraph replays a node, which it does on every resume.
    """
    return {
        "at": now_iso(),
        "case_id": case_id,
        "control_state": control_state.value,
        "action": action,
        "explanation": explanation,
        "arrow": arrow.value if arrow else None,
        **extra,
    }


# ---- symbolic-layer predicates (SKELETON — swap for real engines) -----------


def verify_no_identifiers(payload: dict) -> tuple[bool, str]:
    """OPA no-identifiers invariant. Skeleton: a hard-coded key blocklist.

    Real deployment: an OPA/Rego policy over the payload's information-flow graph
    (Datalog). A present name/ID/DOB/phone key is a structural violation — not
    retryable, halts the case (V·halt·PII).
    """
    banned = {"name", "stable_patient_id", "date_of_birth", "dob", "phone"}
    leaked = sorted(banned & set(payload))
    if leaked:
        return False, f"identifier leaked into redacted payload: {', '.join(leaked)}"
    return True, "no identifiers in model input"


def move_authorized(
    safety_passed: bool, approved: bool, actor_role: str
) -> tuple[bool, str]:
    """OPA authorization for the treatment move (§ no-approval-bypass).

    Refused unless the case already passed safety AND holds approval AND the actor
    holds an authorized role. A refusal is the BLK row: the attempt dies, the case
    does not move.
    """
    if not safety_passed:
        return False, "move refused: safety not passed"
    if not approved:
        return False, "move refused: not approved"
    if actor_role not in {"nurse", "charge_nurse", "shift_lead"}:
        return False, f"move refused: role {actor_role!r} not authorized"
    return True, "move authorized"


def actor_is_charge(actor_role: str) -> tuple[bool, str]:
    """Prolog authorization guard on the human gate: only a charge-role nurse may
    resolve an acuity discrepancy or sign off a safety correction.
    """
    if actor_role in {"charge_nurse", "shift_lead"}:
        return True, f"{actor_role} holds charge role"
    return False, f"gate refused: role {actor_role!r} is not a charge role"
