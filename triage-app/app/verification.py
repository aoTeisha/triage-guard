"""Output Verification.

Post-agent, deterministic, and on the symbolic side of the neuro-symbolic split.
Guards ask whether a step may run; this asks whether what it returned is usable.
No proposal reaches shared state without passing through here.

The spec splits failures two ways, and the split decides the route, so it is
modelled as a type rather than a bool:

  RECOVERABLE  schema mismatch, value out of range, null/wrong type. The step is
               healthy, its output is malformed. Discard and re-invoke on the
               agent's retry budget (V_RETRY), then that agent's AF row
               (V_EXHAUSTED).
  STRUCTURAL   an identifier reached a redacted payload, or a safety invariant
               broke. A retry cannot fix it. Discard and halt (V_HALT).

One rule never bends: a malformed or unsafe output is never written to state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError

from app.budgets import MAX_CORRECTION_ROUNDS
from app.deterministic import verify_no_identifiers
from app.labels import Transition


class Violation(str, Enum):
    RECOVERABLE = "recoverable"
    STRUCTURAL = "structural"


@dataclass(frozen=True)
class VerificationResult:
    """`VERIFICATION_PASSED` with the checked output, or `VERIFICATION_FAILED`
    with the violation list and its category.
    """

    passed: bool
    violations: tuple[str, ...] = ()
    category: Violation | None = None
    checked: Any = None

    @property
    def structural(self) -> bool:
        return self.category is Violation.STRUCTURAL


def _passed(checked: Any) -> VerificationResult:
    return VerificationResult(passed=True, checked=checked)


def verify_schema(agent: str, output: Any, model: type[BaseModel]) -> VerificationResult:
    """Schema + value-range check against the agent's declared output model.

    Pydantic does the work: field presence, types, and the `ge`/`le` bounds that
    `AcuityProposal` puts on the ESI level. A model that returns acuity 47 fails
    here instead of being written to state.
    """
    if isinstance(output, model):
        return _passed(output)
    if not isinstance(output, dict):
        return VerificationResult(
            passed=False,
            violations=(f"{agent}: expected dict or {model.__name__}, got {type(output).__name__}",),
            category=Violation.RECOVERABLE,
        )
    try:
        return _passed(model.model_validate(output))
    except ValidationError as exc:
        violations = tuple(
            f"{agent}: {'.'.join(str(p) for p in err['loc'])} — {err['msg']}"
            for err in exc.errors()
        )
        return VerificationResult(
            passed=False, violations=violations, category=Violation.RECOVERABLE
        )


def verify_redacted_payload(payload: dict[str, Any]) -> VerificationResult:
    """The `redacting_routing` check (V_HALT_PII).

    Structural by definition: an identifier in the model-facing payload is a
    privacy-invariant breach, and re-running the same drop on the same input would
    leak it again. Never retried.
    """
    ok, why = verify_no_identifiers(payload)
    if not ok:
        return VerificationResult(
            passed=False, violations=(why,), category=Violation.STRUCTURAL
        )
    if not payload.get("case_id"):
        # The payload must be keyed by case_id — without it the proposal cannot be
        # attributed to a case, which breaks the audit chain. Recoverable: rebuild.
        return VerificationResult(
            passed=False,
            violations=("redacted payload is not keyed by case_id",),
            category=Violation.RECOVERABLE,
        )
    return _passed(payload)


def verify_patient_record(record: Any) -> VerificationResult:
    """CRM shape check. A record that is present but not a mapping is malformed
    (recoverable); an absent record is a legitimate new-patient result, not a
    failure, and is verified as passing.
    """
    if record is None:
        return _passed(None)
    if not isinstance(record, dict):
        return VerificationResult(
            passed=False,
            violations=(f"crm: expected record mapping, got {type(record).__name__}",),
            category=Violation.RECOVERABLE,
        )
    return _passed(record)


# ---- post-run trace check ----------------------------------------------------
# The runtime guards decide each step as it happens; this re-reads the finished
# audit log and reports where an invariant broke anyway. It exists to catch a
# bug in those guards, so it shares no logic with them (only the round-limit
# constant) and never blocks or alters a case.

_RECORD_KEYS = ("at", "case_id", "control_state", "action", "explanation", "transition")
_SAFETY_VERDICTS = frozenset({Transition.SAFETY_PASSED, Transition.SAFETY_FAILED,
                              Transition.V_EXHAUSTED_SAFETY})
# Anything that sends a case to the human gate: the case may not queue until a
# charge nurse has answered, in the same triage.
_NEEDS_HUMAN = frozenset({Transition.ACUITY_GAP_MAJOR, Transition.ESCALATION_NEEDED,
                          Transition.SAFETY_FAILED, Transition.V_EXHAUSTED_SAFETY})
_HUMAN_ANSWERED = frozenset({Transition.GATE_ACUITY_RESOLVED, Transition.GATE_SAFETY_CORRECTED})

# What the current triage has established so far. Re-filing a case sends it
# back through intake from scratch (a `front_door_rerun` record) and starts a
# new triage, so this resets there too.
_FRESH_TRIAGE = {
    "verdict": None,        # latest safety verdict
    "needs_human": False,   # something sent the case to the charge nurse
    "answered": False,      # the charge nurse answered
    "queued": False,        # the case entered the queue, so it may move
    "failed": False,        # safety failed at least once
    "corrected": False,     # a correction came after the latest failure
    "fixed": False,         # ...and a safety pass came after that correction
    "correcting": False,    # a correction is waiting for its safety verdict
    "revalidated": 0,       # corrections sent back to safety before a senior took over
    "senior": False,        # a shift lead holds the case; rounds stop counting
}


def check_trace(audit_log: list[dict[str, Any]]) -> VerificationResult:
    """Read a whole audit log in order and report every record that breaks a rule.

    The rules: a case reaches the queue or treatment only after a safety pass and
    any required human answer (no bypass); treatment starts once (single
    treatment start); a safety failure needs a correction and a new pass before
    the queue (correct then revalidate); corrections past the round limit go to a
    senior, not back to safety (bounded correction loop); every record has the
    fixed fields and belongs to this case (audit record); nothing but refusals
    after release (closed case).

    A triage is one pass through the log: it starts at the top, and starts
    over each time the case is re-filed from scratch and sent back through
    intake (a `front_door_rerun` record). What one triage established (a
    safety pass, a human answer, a queue entry, correction rounds) does not
    carry into the next. Refused attempts (`blk`) are never moves, and are
    the only kind of record allowed after a release. Each violation names
    the index of the record where the rule broke.
    """
    violations: list[str] = []

    def flag(i: int, rule: str, what: str) -> None:
        violations.append(f"record {i}: {rule}: {what}")

    tri = dict(_FRESH_TRIAGE)
    starts = 0
    closed = False
    case_id = audit_log[0].get("case_id") if audit_log else None
    for i, record in enumerate(audit_log):
        t = record.get("transition")

        # Every record has the same fields and belongs to the same case.
        missing = [k for k in _RECORD_KEYS if k not in record]
        if missing:
            flag(i, "audit record", f"missing {', '.join(missing)}")
        if record.get("case_id") != case_id:
            flag(i, "audit record", f"belongs to case {record.get('case_id')}, not {case_id}")
        if t == Transition.BLK and not record.get("denying_layer"):
            flag(i, "audit record", "refusal names no denying_layer")

        # Once released, a case can only refuse.
        if closed and t != Transition.BLK:
            flag(i, "closed case", f"{t or record.get('action')} written after the case closed")
        if t == Transition.RELEASE:
            closed = True

        if t == Transition.FRONT_DOOR_RERUN:
            tri = dict(_FRESH_TRIAGE)
        if t in _NEEDS_HUMAN:
            tri["needs_human"] = True
        if t in _HUMAN_ANSWERED:
            tri["answered"] = True
        if t == Transition.SENIOR_ESCALATION:
            tri["senior"] = True
        if t == Transition.GATE_SAFETY_CORRECTED:
            tri["corrected"] = tri["correcting"] = True

        if t in _SAFETY_VERDICTS:
            tri["verdict"] = t
            if tri["correcting"] and not tri["senior"]:
                tri["revalidated"] += 1
                if tri["revalidated"] > MAX_CORRECTION_ROUNDS:
                    flag(i, "bounded correction loop", f"correction round {tri['revalidated']} revalidated "
                                  "instead of handing the case to a senior")
            tri["correcting"] = False
            if t == Transition.SAFETY_PASSED:
                tri["fixed"] = tri["corrected"]
            else:
                tri["failed"], tri["corrected"], tri["fixed"] = True, False, False

        if t == Transition.CLEARED_TO_QUEUE:
            if tri["verdict"] != Transition.SAFETY_PASSED:
                flag(i, "no bypass", "cleared_to_queue without safety_passed in this triage")
            elif tri["needs_human"] and not tri["answered"]:
                flag(i, "no bypass", "cleared_to_queue before the required human answer")
            elif tri["failed"] and not tri["fixed"]:
                flag(i, "correct then revalidate", "cleared_to_queue after a safety failure that was not "
                              "corrected and revalidated")
            tri["queued"] = True

        if t == Transition.MOVE_CONFIRMED:
            starts += 1
            if not tri["queued"]:
                flag(i, "no bypass", "move_confirmed before cleared_to_queue in this triage")
            if starts > 1:
                flag(i, "single treatment start", "treatment started a second time")

    if violations:
        return VerificationResult(passed=False, violations=tuple(violations),
                                  category=Violation.STRUCTURAL)
    return _passed(None)
