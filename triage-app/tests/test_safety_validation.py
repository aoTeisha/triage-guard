"""Safety validation's five rules, over the real Prolog engine.

Each rule gets a case that breaks it and a reason that names the contradiction,
because a verdict a charge nurse cannot act on is no better than no verdict.

What is deliberately absent: any test asserting that a level *should* be higher
or lower. SPECIFICATION.md:727 forbids clinical rules here — these check whether
the record can be true, not whether the medicine is right.
"""

from __future__ import annotations

import pytest

from app.actors import safety
from app.graph.state import TriageState
from app.labels import Transition
from app.states import AcuitySource
from app.symbolic import prolog

DECIDED = [{"action": "apply_human_acuity", "resolver_role": "charge_nurse",
            "transition": Transition.GATE_ACUITY_RESOLVED.value}]


def case(**overrides):
    """A case whose record is consistent: nurse 3, system 4, gap 1, auto-settled."""
    base = {
        "case_id": "case-safety",
        "acuity": 3,
        "acuity_source": "auto_resolved",
        "gap": 1,
        "nurse_proposal": 3,
        "system_proposal": 4,
        "classifier_down": False,
        "triage_records": [],
    }
    return base | overrides


def only_reason(verdict) -> str:
    assert verdict.verdict == "fail", verdict
    assert len(verdict.reasons) == 1, verdict.reasons
    return verdict.reasons[0]


def test_a_consistent_record_passes():
    verdict = safety.validate(case())
    assert verdict.verdict == "pass"
    assert verdict.reasons == ["no contradiction in the case record"]


# ---- rule 1: a level and its provenance travel together ----------------------


def test_an_acuity_with_no_source_fails():
    assert "cannot be attributed" in only_reason(safety.validate(case(acuity_source=None)))


def test_a_source_with_no_acuity_fails():
    assert "no acuity is set" in only_reason(safety.validate(case(acuity=None)))


# ---- rule 2: the automatic settle only exists for gaps 0 and 1 ---------------


@pytest.mark.parametrize("gap", [2, 3, 4])
def test_an_automatic_settle_on_a_major_gap_fails(gap):
    reason = only_reason(safety.validate(case(gap=gap)))
    assert "auto_resolved" in reason and str(gap) in reason


@pytest.mark.parametrize("gap", [0, 1])
def test_an_automatic_settle_within_its_bands_passes(gap):
    assert safety.validate(case(gap=gap, system_proposal=3 + gap)).verdict == "pass"


# ---- rule 3: a human-confirmed level needs a human decision in this triage ---


def test_human_confirmed_with_no_decision_in_the_log_fails():
    verdict = safety.validate(case(acuity_source="human_confirmed"))
    assert "holds no charge-nurse decision" in only_reason(verdict)


def test_human_confirmed_with_a_decision_passes():
    verdict = safety.validate(case(acuity_source="human_confirmed", triage_records=DECIDED))
    assert verdict.verdict == "pass"


def test_a_correction_at_the_gate_is_a_decision_too():
    corrected = [{"action": "apply_correction", "resolver_role": "charge_nurse",
                  "transition": Transition.GATE_SAFETY_CORRECTED.value}]
    verdict = safety.validate(case(acuity=1, acuity_source="human_confirmed",
                                   triage_records=corrected))
    assert verdict.verdict == "pass"


def test_a_gate_answer_that_settles_no_acuity_is_not_a_decision():
    """Asking for the check again settles nothing, so it vouches for nothing."""
    revalidated = [{"action": "request_revalidation", "resolver_role": "charge_nurse",
                    "transition": Transition.GATE_REVALIDATE.value}]
    verdict = safety.validate(case(acuity_source="human_confirmed", triage_records=revalidated))
    assert "holds no charge-nurse decision" in only_reason(verdict)


def test_a_decision_from_an_earlier_triage_does_not_count():
    """A re-file starts a new triage (I3). `current_triage` cuts the log at the
    `front_door_rerun` record, so last triage's decision vouches for nothing.
    """
    log = DECIDED + [{"action": "emit_event_log", "transition": Transition.FRONT_DOOR_RERUN}]
    assert safety.current_triage(log) == []      # the re-file record closes the old triage
    verdict = safety.validate(case(acuity_source="human_confirmed",
                                  triage_records=safety.current_triage(log)))
    assert "holds no charge-nurse decision" in only_reason(verdict)


@pytest.mark.parametrize("source, gap, system", [("agreed", 0, 3), ("nurse_fallback", None, None)])
def test_the_settles_no_charge_nurse_made_need_no_decision(source, gap, system):
    """Agreement and the classifier-down settle carry their own labels, so rule 3
    never asks them for a gate decision that never happened."""
    verdict = safety.validate(case(acuity_source=source, gap=gap, system_proposal=system))
    assert verdict.verdict == "pass"


# ---- the state's enum reaches the rules as its value -------------------------


@pytest.mark.parametrize("source, gap, system, code", [
    (AcuitySource.HUMAN_CONFIRMED, 0, 3, "human_confirmed_without_a_decision"),
    (AcuitySource.AUTO_RESOLVED, 2, 5, "auto_resolved_on_major_gap"),
])
def test_an_enum_source_fires_its_rule(source, gap, system, code):
    """The running graph holds `AcuitySource`, not a string. `str()` of a member
    is its name, which no rule matches, so rules 2 and 3 once never fired."""
    codes, error = prolog.safety_violations(case(acuity_source=source, gap=gap,
                                                 system_proposal=system))
    assert (codes, error) == ([code], "")


def test_facts_from_hands_over_the_source_value():
    state = TriageState(case_id="c1", acuity=3, acuity_source=AcuitySource.HUMAN_CONFIRMED)
    assert safety.facts_from(state)["acuity_source"] == "human_confirmed"


# ---- rule 4: the level came from somewhere -----------------------------------


def test_an_acuity_matching_neither_proposal_fails():
    reason = only_reason(safety.validate(case(acuity=1)))
    assert "neither the nurse's (3) nor the system's (4)" in reason


def test_a_charge_nurse_may_set_a_level_neither_side_proposed():
    """Rule 4 asks where a number came from, not whether it is permitted. A human
    decision is provenance enough (I3), and a correction is exactly that case.
    """
    verdict = safety.validate(case(acuity=1, acuity_source="human_confirmed",
                                   triage_records=DECIDED))
    assert verdict.verdict == "pass"


# ---- rule 5: the degrade path and the data must agree -----------------------


def test_a_downed_classifier_with_a_proposal_fails():
    verdict = safety.validate(case(classifier_down=True))
    assert "classifier is flagged unusable" in only_reason(verdict)


def test_a_downed_classifier_with_no_proposal_passes():
    """The real fallback: the nurse's own level, no system proposal (AF·classifier)."""
    verdict = safety.validate(case(classifier_down=True, system_proposal=None,
                                   gap=None, acuity_source="nurse_fallback"))
    assert verdict.verdict == "pass"


# ---- the engine itself ------------------------------------------------------


def test_an_engine_that_cannot_answer_fails_closed(monkeypatch):
    """SYSTEM_MODELING.md:101 — a validator that cannot answer routes the case to
    a human. It must never wave one through: no verdict at all, raised as
    `ValidatorUnavailable`, which the graph turns into the validator-down gate.
    """
    monkeypatch.setattr(prolog, "safety_violations", lambda c: ([], "prolog: no engine"))

    with pytest.raises(safety.ValidatorUnavailable) as caught:
        safety.validate(case())
    assert caught.value.engines == ["prolog"]
