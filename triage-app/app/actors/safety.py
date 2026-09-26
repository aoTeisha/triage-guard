"""Safety Validation — the deterministic verdict (docs/actors/safety_validator.jsonc).

**What this checks, and what it deliberately does not.** It does not re-judge the
medicine. SPECIFICATION.md:727 settles that: ESI treats danger-zone vitals as a
judgment in clinical context, its own worked examples show a heart rate of 102
staying at level 3 while an SpO2 of 91% is uptriaged for reasons no threshold
holds, and code cannot tell those apart. So no rule here overturns an acuity.

What it checks is whether the case's record *can be true* — a level with no
provenance, an automatic settle on a gap that was never automatic, a number
nobody proposed, a claim that a human decided when the log holds no decision,
clinical data that has gone missing. Contradictions, not opinions (I3, I13).

Two engines, each on the question it suits:

    Prolog   (rules/safety.pl)  one case's fields against each other
    Datalog  (symbolic/datalog) who wrote the acuity, and whether the data is there

Every reason names the field and the contradiction. A verdict of "safety failed"
would tell a charge nurse nothing to act on, and the course material is explicit
that a validator's feedback has to be usable: "the feedback must be useful
information about *why* it failed" (neuro_symbolic_ai_architect_updated.md:271).

An engine that cannot answer is a **fail**, never a pass — the case goes to a
human, which is the documented degrade (SYSTEM_MODELING.md:101).
"""

from __future__ import annotations

from typing import Any

from app.schemas import SafetyVerdict
from app.symbolic import datalog, prolog


class ValidatorUnavailable(RuntimeError):
    """Raised when the symbolic engine cannot be reached (drives AF_SAFETY)."""


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
        "charge-nurse decision (I3)",
    "acuity_matches_no_proposal": lambda c:
        f"acuity is {c.get('acuity')}, which is neither the nurse's "
        f"({c.get('nurse_proposal')}) nor the system's ({c.get('system_proposal')}), "
        "and no human set it",
    "classifier_down_yet_proposed": lambda c:
        f"the classifier is flagged unusable, yet system_proposed_acuity is "
        f"{c.get('system_proposal')}",
}


def facts_from(state: Any) -> dict[str, Any]:
    """The facts the engines reason over, read off the graph state.

    A plain mapping, not the state object: the engines receive facts, and keeping
    that boundary here means the node stays free of engine detail.
    """
    audit_log = getattr(state, "audit_log", None) or []
    triage = datalog.current_triage(audit_log)
    return {
        "case_id": getattr(state, "case_id", None),
        "acuity": getattr(state, "acuity", None),
        "acuity_source": getattr(state, "acuity_source", None),
        "gap": getattr(state, "acuity_gap", None),
        "nurse_proposal": getattr(state, "nurse_proposed_acuity", None),
        "system_proposal": getattr(state, "system_proposed_acuity", None),
        "classifier_down": "acuity_classifier" in (getattr(state, "degraded", None) or []),
        "payload": getattr(state, "redacted_payload", None) or {},
        "triage_records": triage,
    }


def validate(case: dict[str, Any]) -> SafetyVerdict:
    """Propose pass/fail on the settled acuity, with a reason per failed rule."""
    provenance = datalog.acuity_provenance(
        case.get("triage_records") or [],
        case.get("payload") or {},
        prolog.charge_roles(),
    )
    # Datalog answers "did an authorized human write this acuity in this triage",
    # which rule 3 in safety.pl then reasons with.
    codes, engine_error = prolog.safety_violations(
        {**case, "human_decided": bool(provenance["writers"])}
    )

    reasons = [_EXPLAIN[code](case) for code in codes if code in _EXPLAIN]
    reasons += [f"unknown violation code from safety.pl: {code}"
                for code in codes if code not in _EXPLAIN]
    reasons += [f"acuity was written by {role!r}, which holds no charge role (I3, I14)"
                for role in provenance["unauthorized"]]
    if provenance["missing_fields"]:
        reasons.append("the case no longer holds the clinical data the acuity was judged on: "
                       + ", ".join(provenance["missing_fields"]))
    if engine_error:
        # Fail closed: an engine that cannot answer sends the case to a human.
        reasons.append(f"safety engine could not answer, routing to a human: {engine_error}")

    if reasons:
        return SafetyVerdict(verdict="fail", reasons=reasons)
    return SafetyVerdict(verdict="pass", reasons=["no contradiction in the case record"])
