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
from app.symbolic import opa, prolog

# ---- order_key and the display bucket (§ Queue ordering rule) ---------------


def bucket_for(acuity: int) -> AcuityBucket:
    """emergent = ESI 1-2, queued = ESI 3-5. A display label; order_key sorts."""
    return AcuityBucket.EMERGENT if acuity <= 2 else AcuityBucket.QUEUED


def assign_order_key(acuity: int, arrival_time: str) -> tuple[int, str]:
    """The single writer of order_key: (acuity, arrival_time).

    Lower ESI sorts first, so a more acute patient is always ahead;
    arrival_time breaks ties. Re-keyed whenever acuity changes, never by the timer.

    Refuses a missing arrival time: substituting "now" would silently send the
    patient to the back of their level (I2).
    """
    if not arrival_time:
        raise ValueError("order_key needs the arrival time stamped at intake")
    return (acuity, arrival_time)


# ---- acuity-gap resolution at the gate (arrows 9a / 9b / 9c) ----------------


def compute_acuity_gap(nurse: int, system: int) -> int:
    return abs(nurse - system)


def resolve_acuity(
    nurse: int, system: int
) -> tuple[int | None, AcuitySource | None, Arrow]:
    """Return (final_acuity, acuity_source, arrow) per the gap bands (I4).

        gap 0   -> agree, keep it              (9a, human_confirmed)
        gap 1   -> take the NURSE's value      (9b, auto_resolved)
        gap >=2 -> charge nurse decides        (9c, unresolved)

    The bands must be total and exclusive, so exactly one arm fires for every
    gap >= 0 (I4; to be proven with Z3). The 9c arm returns None rather than a
    sentinel number: there is no final acuity yet, and a placeholder integer here
    would be indistinguishable from a real ESI level downstream.
    """
    gap = compute_acuity_gap(nurse, system)
    if gap == 0:
        return nurse, AcuitySource.HUMAN_CONFIRMED, Arrow.ACUITY_AGREE
    if gap == 1:
        # The nurse holds a gap of 1. Was `min(nurse, system)` until 2026-09-13;
        # changed because the classifier over-triages systematically and would
        # otherwise win every close call unseen. Both inputs are logged at 9b.
        return nurse, AcuitySource.AUTO_RESOLVED, Arrow.ACUITY_GAP_MINOR
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


def audit_denial(case_id: str, control_state: State, why: str,
                 layer: str = "Prolog (authorization)") -> dict[str, Any]:
    """The BLK row every refused attempt writes (I18). `layer` names which
    engine refused — the gate is Prolog's (I14), move and release are OPA's
    (I5/I9).
    """
    return audit(case_id, control_state, "explain_denial", why, Arrow.BLK, denying_layer=layer)


# ---- symbolic-layer predicates ---------------------------------------------
# `verify_no_identifiers` is still the Python skeleton (redaction/I11 is
# outside the monitor's scope); the three guards below it are answered by the
# real engines in `app.symbolic`.


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
    """OPA authorization for the treatment move (I5 no bypass), evaluated by
    the real engine over `app/symbolic/policy/monitor.rego`. A refusal is the
    BLK row: the attempt dies, the case does not move.
    """
    gate = opa.evaluate({"action": "move",
                         "case": {"safety_passed": bool(safety_passed), "approved": bool(approved)},
                         "actor_role": actor_role})
    return gate["allow"], ("move authorized" if gate["allow"] else "; ".join(gate["deny_reasons"]))


def release_authorized(reason: str, actor_role: str) -> tuple[bool, str]:
    """OPA authorization for release (I9): a valid reason plus an authorized
    signer. State-independent — release can happen from any pause, so this
    takes no source-state argument.
    """
    gate = opa.evaluate({"action": "release", "reason": reason, "actor_role": actor_role})
    return gate["allow"], ("release authorized" if gate["allow"] else "; ".join(gate["deny_reasons"]))


def actor_is_charge(actor_role: str) -> tuple[bool, str]:
    """Prolog authorization guard on the human gate (I14): only a charge-role
    nurse may resolve an acuity discrepancy or sign off a safety correction.
    Answered by the real engine over `app/symbolic/rules/monitor.pl`.
    """
    return prolog.charge_role(actor_role)
