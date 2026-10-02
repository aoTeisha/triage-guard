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

from app.guards.identifiers import find_identifiers
from app.labels import Transition
from app.states import AcuityBucket, AcuitySource, State
from app.symbolic import opa, prolog

# ---- order_key and the display bucket (§ Queue ordering rule) ---------------


def bucket_for(acuity: int) -> AcuityBucket:
    """emergent = ESI 1-2, queued = ESI 3-5. A display label; order_key sorts."""
    return AcuityBucket.EMERGENT if acuity <= 2 else AcuityBucket.QUEUED


def assign_order_key(acuity: int, arrival_time: str) -> tuple[int, float]:
    """The single writer of order_key: (acuity, arrival as epoch seconds).

    Lower ESI sorts first, so a more acute patient is always ahead; arrival
    breaks ties. Re-keyed whenever acuity changes, never by the timer.

    The tie-break is a number, not the ISO string. Comparing timestamps as text
    agrees with comparing them as moments only while every string has the same
    shape and zone: "…T10:00+03:00" sorts after "…T09:00+00:00" while being an
    hour earlier. The case still stores `arrival_time` as ISO, for display.

    Refuses a missing arrival time: substituting "now" would silently send the
    patient to the back of their level (I2). Refuses a naive one for the same
    reason — a timestamp with no zone cannot be placed on a shared clock.
    """
    if not arrival_time:
        raise ValueError("order_key needs the arrival time stamped at intake")
    arrived = datetime.fromisoformat(arrival_time)
    if arrived.tzinfo is None:
        raise ValueError(f"order_key needs a timezone-aware arrival time: {arrival_time!r}")
    return (acuity, arrived.timestamp())


# ---- acuity-gap resolution at the gate -----------------------------------


# The gap bands (I4), named once. `app/symbolic/z3_proofs.py` imports these to
# prove the bands total and exclusive, so changing a threshold here moves the
# proof with it instead of leaving it proving the old rule.
AGREE_GAP = 0       # nurse and system agree
MINOR_GAP = 1       # settled automatically, to the nurse's level; above this, the charge nurse


def compute_acuity_gap(nurse: int, system: int) -> int:
    return abs(nurse - system)


def resolve_acuity(
    nurse: int, system: int
) -> tuple[int | None, AcuitySource | None, Transition]:
    """Return (final_acuity, acuity_source, transition) per the gap bands (I4).

        gap 0   -> agree, keep it              (ACUITY_AGREE, human_confirmed)
        gap 1   -> take the NURSE's value      (ACUITY_GAP_MINOR, auto_resolved)
        gap >=2 -> charge nurse decides        (ACUITY_GAP_MAJOR, unresolved)

    The bands must be total and exclusive, so exactly one arm fires for every
    gap >= 0 — proven in app/symbolic/z3_proofs.py. The ACUITY_GAP_MAJOR arm returns None
    rather than a sentinel number: there is no final acuity yet, and a
    placeholder integer here would be indistinguishable from a real ESI level
    downstream.
    """
    gap = compute_acuity_gap(nurse, system)
    if gap == AGREE_GAP:
        return nurse, AcuitySource.HUMAN_CONFIRMED, Transition.ACUITY_AGREE
    if gap == MINOR_GAP:
        # The nurse holds a gap of 1. Was `min(nurse, system)` until 2026-09-13;
        # changed because the classifier over-triages systematically and would
        # otherwise win every close call unseen. Both inputs are logged at
        # ACUITY_GAP_MINOR.
        return nurse, AcuitySource.AUTO_RESOLVED, Transition.ACUITY_GAP_MINOR
    return None, None, Transition.ACUITY_GAP_MAJOR


# ---- audit (emit_event_log — every transition) ------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit(
    case_id: str,
    control_state: State,
    action: str,
    explanation: str,
    transition: Transition | None = None,
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
        "transition": transition.value if transition else None,
        **extra,
    }


# Engines named at the front of a `layer` string (e.g. "OPA (authorization)")
# get an `engines` chip on the audit row; a plain Python check (e.g. "monitor
# (idempotency)") names no engine and gets none.
_KNOWN_ENGINES = {"Prolog", "OPA", "Datalog", "Z3"}


def audit_denial(case_id: str, control_state: State, why: str,
                 layer: str = "Prolog (authorization)") -> dict[str, Any]:
    """The BLK row every refused attempt writes (I18). `layer` names which
    engine refused — the gate is Prolog's (I14), move and release are OPA's
    (I5/I9).
    """
    engine = layer.split()[0]
    extra = {"engines": [engine]} if engine in _KNOWN_ENGINES else {}
    return audit(case_id, control_state, "explain_denial", why, Transition.BLK,
                 denying_layer=layer, **extra)


# ---- symbolic-layer predicates ---------------------------------------------
# All four are answered by the real engines in `app.symbolic`.


def verify_no_identifiers(payload: dict) -> tuple[bool | None, str]:
    """What the model may see (I11, I12), decided by OPA over
    `app/symbolic/policy/privacy.rego`: only approved fields, each a closed value,
    no identifier key at any depth. Then the regex scan for an identifier typed
    *inside* an allowed value, which Rego's RE2 cannot express.

    The payload builder already redacted values, so a hit here means redaction
    missed something: a structural violation, not retryable, halts the case
    (V_HALT_PII).

    `None` means OPA could not answer and the regex scan found nothing: the
    payload is not proven clean, so it must not reach the model, but nothing was
    found either. The caller skips the model instead of halting. A regex hit
    while OPA is down is still a leak (`False`).
    """
    gate = opa.evaluate({"payload": payload}, policy=opa.PRIVACY_POLICY, query=opa.PRIVACY_QUERY)
    leaked = list(gate["deny_reasons"])
    found = [f"{kind} in {path}" for path, kind in find_identifiers(payload)]
    if _engine_down(gate) and not found:
        return None, f"privacy check unavailable, payload not proven clean: {'; '.join(leaked)}"
    if not gate["allow"] and not leaked:
        leaked.append("the privacy policy did not allow the payload (no case_id?)")
    leaked += found
    if leaked:
        return False, f"payload refused for the model: {'; '.join(leaked)}"
    return True, "payload approved for the model"


# When the engine that authorizes an action cannot answer, the action is never
# simply blocked: a shift lead signs instead, before it happens, and the facts the
# engine would have checked are checked here. A `why` starting with this marks
# such an action, so the caller can record the case as running degraded.
SHIFT_LEAD_STANDS_IN = "authorized by shift lead"
STAND_IN_ROLE = "shift_lead"
# Same set as `release_reasons` in `app/symbolic/policy/monitor.rego`.
RELEASE_REASONS = frozenset({"discharge", "ama", "transfer", "admit"})


def _engine_down(gate: dict) -> bool:
    reasons = gate["deny_reasons"]
    return bool(reasons) and all(r.startswith("engine_unavailable:opa") for r in reasons)


def _stand_in(actor_role: str, engine: str, facts_ok: bool, facts_why: str) -> tuple[bool, str]:
    """The shift lead's sign-off when `engine` cannot answer."""
    if actor_role != STAND_IN_ROLE:
        return False, f"{engine} unavailable: shift lead sign-off required (role {actor_role!r})"
    if not facts_ok:
        return False, f"{engine} unavailable, and {facts_why}"
    return True, f"{SHIFT_LEAD_STANDS_IN}: {engine} unavailable"


def move_authorized(
    safety_passed: bool, approved: bool, actor_role: str, safety_waived: bool = False
) -> tuple[bool, str]:
    """OPA authorization for the treatment move (no bypass: a patient moves only
    after passing safety and being approved), evaluated by the real engine over
    `app/symbolic/policy/monitor.rego`. A refusal is the BLK row: the attempt
    dies, the case does not move. OPA unable to answer: a shift lead may move a
    patient who passed safety and was approved. A shift lead's clearance while the
    safety check could not run (`safety_waived`) stands in for the pass.
    """
    gate = opa.evaluate({"action": "move",
                         "case": {"safety_passed": bool(safety_passed), "approved": bool(approved),
                                  "safety_waived": bool(safety_waived)},
                         "actor_role": actor_role})
    if _engine_down(gate):
        return _stand_in(actor_role, "OPA", bool((safety_passed or safety_waived) and approved),
                         "the patient has not passed safety and been approved")
    return gate["allow"], ("move authorized" if gate["allow"] else "; ".join(gate["deny_reasons"]))


def release_authorized(reason: str, actor_role: str) -> tuple[bool, str]:
    """OPA authorization for release: a valid reason plus an authorized signer.
    State-independent — release can happen from any pause, so this takes no
    source-state argument. OPA unable to answer: a shift lead may release with
    a valid reason.
    """
    gate = opa.evaluate({"action": "release", "reason": reason, "actor_role": actor_role})
    if _engine_down(gate):
        return _stand_in(actor_role, "OPA", reason in RELEASE_REASONS,
                         f"{reason!r} is not a release reason")
    return gate["allow"], ("release authorized" if gate["allow"] else "; ".join(gate["deny_reasons"]))


def actor_is_charge(actor_role: str) -> tuple[bool, str]:
    """Prolog authorization guard on the human gate (I14): only a charge-role
    nurse may resolve an acuity discrepancy or sign off a safety correction.
    Answered by the real engine over `app/symbolic/rules/monitor.pl`.
    """
    return prolog.charge_role(actor_role)
