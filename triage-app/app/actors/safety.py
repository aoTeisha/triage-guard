"""Safety Validation — the deterministic verdict (docs/actors/safety_validator.jsonc).

**What this checks, and what it deliberately does not.** It does not re-judge the
medicine. SPECIFICATION.md:727 settles that: ESI treats danger-zone vitals as a
judgment in clinical context, its own worked examples show a heart rate of 102
staying at level 3 while an SpO2 of 91% is uptriaged for reasons no threshold
holds, and code cannot tell those apart. So no rule here overturns an acuity.

What it checks is whether the case's record *can be true* — a level with no
provenance, an automatic settle on a gap that was never automatic, a number
nobody proposed, a claim that a charge nurse decided when this triage's log holds
no gate decision. Contradictions, not opinions (I3, I13). The rules are Prolog's
(rules/safety.pl), each one case's fields checked against each other.

Two things it does not check, because nothing upstream lets them happen: who
wrote the acuity (only the gate writes one, and only after Prolog's
`may_resolve_gate` has authorized the resolver), and whether `chief_complaint`
and `vitals` are present (intake requires both, and redaction keeps them or
halts the case).

Every reason names the field and the contradiction. A verdict of "safety failed"
would tell a charge nurse nothing to act on, and the course material is explicit
that a validator's feedback has to be usable: "the feedback must be useful
information about *why* it failed" (neuro_symbolic_ai_architect_updated.md:271).

An engine that cannot answer is never a pass — the case goes to a human, which
is the documented degrade (SYSTEM_MODELING.md:101).
"""

from __future__ import annotations

from typing import Any

from app.labels import Transition
from app.schemas import SafetyVerdict
from app.symbolic import prolog

# The gate records that settle an acuity: a charge nurse choosing a level, or
# correcting one after a failed check. Rule 3 in safety.pl asks for one of these.
GATE_DECISIONS = frozenset({Transition.GATE_ACUITY_RESOLVED, Transition.GATE_SAFETY_CORRECTED})


class ValidatorUnavailable(RuntimeError):
    """Raised when a symbolic engine cannot answer (drives AF_SAFETY).

    `engines` names which ones, lower case ("prolog"), so the case can say
    exactly what is down.
    """

    def __init__(self, engines: list[str], detail: str):
        super().__init__(detail)
        self.engines = engines


# code -> how to say it to the person who has to fix it. Each takes the case.
_EXPLAIN = {
    "acuity_without_source": lambda c:
        f"acuity is {c.get('acuity')} but acuity_source is empty: the level cannot be attributed",
    "source_without_acuity": lambda c:
        f"acuity_source is {c.get('acuity_source')} but no acuity is set",
    "auto_resolved_on_major_gap": lambda c:
        f"acuity_source is auto_resolved, but the nurse/system gap was {c.get('gap')} — "
        "a gap of 2 or more is the charge nurse's to settle (I4)",
    "human_confirmed_without_a_decision": lambda c:
        "acuity_source is human_confirmed, but this triage's audit log holds no "
        "charge-nurse decision at the gate (I3)",
    "acuity_matches_no_proposal": lambda c:
        f"acuity is {c.get('acuity')}, which is neither the nurse's "
        f"({c.get('nurse_proposal')}) nor the system's ({c.get('system_proposal')}), "
        "and no human set it",
    "classifier_down_yet_proposed": lambda c:
        f"the classifier is flagged unusable, yet system_proposed_acuity is "
        f"{c.get('system_proposal')}",
}


def current_triage(audit_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records belonging to the triage running now.

    A `front_door_rerun` record is a re-file: everything before it belongs to a
    finished triage, and last triage's decision vouches for nothing in this one
    (I3). Same boundary `app.verification.check_trace` uses.
    """
    last_rerun = -1
    for i, record in enumerate(audit_log or []):
        if record.get("transition") == Transition.FRONT_DOOR_RERUN:
            last_rerun = i
    return list(audit_log or [])[last_rerun + 1:]


def decided_at_gate(triage_records: list[dict[str, Any]]) -> bool:
    """Whether a charge nurse settled the acuity at the gate in this triage."""
    return any(record.get("transition") in GATE_DECISIONS for record in triage_records)


def classifier_fell_back(triage_records: list[dict[str, Any]]) -> bool:
    """Whether this triage took the classifier fallback (`fallback_manual`).

    Read from this triage's records, not from `degraded`: that list is
    append-only history for the board, so a classifier outage in an earlier
    triage would stay in it and make a re-file's working classifier look down.
    """
    return any(record.get("transition") == Transition.V_EXHAUSTED_CLASSIFIER
               for record in triage_records)


def facts_from(state: Any) -> dict[str, Any]:
    """The facts the engine reasons over, read off the graph state.

    A plain mapping, not the state object: the engine receives facts, and keeping
    that boundary here means the node stays free of engine detail.
    """
    audit_log = getattr(state, "audit_log", None) or []
    source = getattr(state, "acuity_source", None)
    triage_records = current_triage(audit_log)
    return {
        "case_id": getattr(state, "case_id", None),
        "acuity": getattr(state, "acuity", None),
        # The state holds an `AcuitySource`; the rules and the reasons want its value.
        "acuity_source": getattr(source, "value", source),
        "gap": getattr(state, "acuity_gap", None),
        "nurse_proposal": getattr(state, "nurse_proposed_acuity", None),
        "system_proposal": getattr(state, "system_proposed_acuity", None),
        # This triage only, the boundary rule 3 uses too: last triage's outage
        # says nothing about the proposal this one holds.
        "classifier_down": classifier_fell_back(triage_records),
        "triage_records": triage_records,
    }


def validate(case: dict[str, Any]) -> SafetyVerdict:
    """Propose pass/fail on the settled acuity, with a reason per failed rule."""
    # Rule 3 in safety.pl reasons with whether the gate settled this triage's
    # acuity; the gate's own records answer that.
    codes, prolog_error = prolog.safety_violations(
        {**case, "human_decided": decided_at_gate(case.get("triage_records") or [])}
    )
    # An engine that could not answer means no verdict at all, not a failing
    # one: raise, so the case takes the validator-down path to a charge nurse,
    # who asks for the check again once the engine is back. A "fail" here would
    # demand a correction to the case, when the case itself is fine.
    if prolog_error:
        raise ValidatorUnavailable(["prolog"], prolog_error)

    reasons = [_EXPLAIN[code](case) for code in codes if code in _EXPLAIN]
    reasons += [f"unknown violation code from safety.pl: {code}"
                for code in codes if code not in _EXPLAIN]
    if reasons:
        return SafetyVerdict(verdict="fail", reasons=reasons)
    return SafetyVerdict(verdict="pass", reasons=["no contradiction in the case record"])
