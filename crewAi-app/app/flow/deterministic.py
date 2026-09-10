"""Deterministic actions and derivations (SPECIFICATION.md § Actions).

Everything in this file is plain, testable code with no LLM, no network, and no
clock dependence beyond an injected timestamp. These are the parts the spec
marks as deterministic — queue ordering, acuity-gap resolution, the audit
append — and the neuro-symbolic split forbids ever routing them through the
model. The single LLM step (the Acuity Classifier) lives in flow/agents.py.

Each function is a `mock/skeleton` stand-in only where noted: the ordering and
gap-resolution logic is the real rule from the spec, ready to keep. The
symbolic-layer calls (OPA/Z3/Prolog/Datalog) are represented by `verify_*`
predicates that currently return a fixed pass — swap those for real engine
calls without touching the Flow wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.flow.state import TriageState

# ---- order_key / bucket (SPECIFICATION.md § Queue ordering rule) ------------


def bucket_for(acuity: int) -> str:
    """emergent = ESI 1-2, queued = ESI 3-5. The primary sort key."""
    return "emergent" if acuity <= 2 else "queued"


def assign_order_key(acuity: int, arrival_time: str) -> tuple[int, str]:
    """The single writer of order_key: (bucket_rank, arrival_time).

    bucket_rank 0 (emergent) always sorts ahead of 1 (queued); arrival_time
    breaks ties within a bucket. Called at intake and again whenever acuity
    changes — never by the timer alone (acuity-write-authority invariant).
    """
    bucket_rank = 0 if bucket_for(acuity) == "emergent" else 1
    return (bucket_rank, arrival_time)


# ---- acuity-gap resolution at the gate (arrows 9a / 9b / 9c) ----------------


def compute_acuity_gap(nurse: int, system: int) -> int:
    return abs(nurse - system)


def resolve_acuity(nurse: int, system: int) -> tuple[int, str, str]:
    """Return (final_acuity, acuity_source, arrow) per the Z3-proven bands.

    gap 0  -> agree, keep it            (9a, human_confirmed)
    gap 1  -> take the MORE acute (min) (9b, auto_resolved)
    gap >=2 -> charge nurse decides     (9c, escalate — no final acuity yet)

    The bands are total and exclusive (Z3, band totality), so exactly one arm
    fires for every gap >= 0.
    """
    gap = compute_acuity_gap(nurse, system)
    if gap == 0:
        return nurse, "human_confirmed", "9a"
    if gap == 1:
        return min(nurse, system), "auto_resolved", "9b"
    return -1, "escalate", "9c"   # -1 = unresolved; the gate sets it


# ---- audit (emit_event_log — every transition) ------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_event_log(state: TriageState, action: str, explanation: str, **extra: Any) -> None:
    """Append one trace record. The append-only auditability constraint —
    every state-changing action lands here (SYSTEM_MODELING.md § Hard Constraints).
    """
    state.audit_log.append(
        {
            "at": now_iso(),
            "case_id": state.case_id,
            "control_state": state.control_state,
            "action": action,
            "explanation": explanation,
            **extra,
        }
    )


# ---- symbolic-layer predicates (SKELETON — fixed pass, swap for real engines)


def verify_no_identifiers(payload: dict) -> tuple[bool, str]:
    """OPA no-identifiers invariant. Skeleton: checks a hard-coded key blocklist.

    Real deployment: an OPA/Rego policy over the payload's information-flow
    graph (Datalog). For now a present name/ID/DOB/phone key is a structural
    violation (V·halt·PII).
    """
    banned = {"name", "stable_patient_id", "date_of_birth", "dob", "phone"}
    leaked = sorted(banned & set(payload))
    if leaked:
        return False, f"identifier leaked into redacted payload: {', '.join(leaked)}"
    return True, "no identifiers in model input"


def verify_output(agent: str, output: dict) -> tuple[bool, str]:
    """Output Verification (deterministic, post-agent). Skeleton: always passes.

    Real deployment: schema + value-range + invariant checks (Schema + Datalog
    + OPA), emitting VERIFICATION_PASSED / VERIFICATION_FAILED. Wire the real
    checks here; the Flow already routes V·pass / V·retry / V·halt off its result.
    """
    return True, f"{agent} output verified (skeleton)"


def move_authorized(state: TriageState, actor_role: str) -> tuple[bool, str]:
    """OPA authorization for the treatment move. Enforces no-approval-bypass:
    refused unless the case already passed safety AND holds approval.
    """
    if not state.safety_passed:
        return False, "move refused: safety not passed"
    if not state.approved:
        return False, "move refused: not approved"
    if actor_role not in {"nurse", "charge_nurse", "shift_lead"}:
        return False, f"move refused: role {actor_role!r} not authorized"
    return True, "move authorized"
