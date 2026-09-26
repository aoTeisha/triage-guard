"""Safety validation's six rules, over the real Prolog and Datalog engines.

Each rule gets a case that breaks it and a reason that names the contradiction,
because a verdict a charge nurse cannot act on is no better than no verdict.

What is deliberately absent: any test asserting that a level *should* be higher
or lower. SPECIFICATION.md:727 forbids clinical rules here — these check whether
the record can be true, not whether the medicine is right.
"""

from __future__ import annotations

import pytest

from app.actors import safety
from app.symbolic import prolog

DECIDED = [{"action": "apply_human_acuity", "resolver_role": "charge_nurse"}]


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
        "payload": {"chief_complaint": "chest_pain", "vitals": {"hr": 96}},
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


def test_a_decision_from_an_earlier_triage_does_not_count():
    """A re-file starts a new triage (I3). `current_triage` cuts the log at the
    `front_door_rerun` record, so last triage's decision vouches for nothing.
    """
    from app.labels import Transition
    from app.symbolic import datalog

    log = DECIDED + [{"action": "emit_event_log", "transition": Transition.FRONT_DOOR_RERUN}]
    assert datalog.current_triage(log) == []      # the re-file record closes the old triage
    verdict = safety.validate(case(acuity_source="human_confirmed",
                                  triage_records=datalog.current_triage(log)))
    assert "holds no charge-nurse decision" in only_reason(verdict)


def test_an_acuity_written_by_an_unauthorized_role_fails():
    written_by_a_porter = [{"action": "apply_human_acuity", "resolver_role": "porter"}]
    verdict = safety.validate(case(acuity_source="human_confirmed",
                                  triage_records=written_by_a_porter))
    assert verdict.verdict == "fail"
    assert any("porter" in reason and "no charge role" in reason for reason in verdict.reasons)


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
                                   gap=None, acuity_source="human_confirmed",
                                   triage_records=DECIDED))
    assert verdict.verdict == "pass"


# ---- rule 6: the clinical data the acuity was judged on ---------------------


@pytest.mark.parametrize("field", ["chief_complaint", "vitals"])
def test_a_case_missing_its_clinical_data_fails(field):
    payload = {k: v for k, v in case()["payload"].items() if k != field}
    reason = only_reason(safety.validate(case(payload=payload)))
    assert field in reason


# ---- the engine itself ------------------------------------------------------


def test_an_engine_that_cannot_answer_fails_closed(monkeypatch):
    """SYSTEM_MODELING.md:101 — a validator that cannot answer routes the case to
    a human. It must never wave one through.
    """
    monkeypatch.setattr(prolog, "safety_violations", lambda c: ([], "prolog: no engine"))

    verdict = safety.validate(case())
    assert verdict.verdict == "fail"
    assert any("routing to a human" in reason for reason in verdict.reasons)


def test_the_role_set_comes_from_prolog_not_a_copy():
    """`acuity_provenance` asks Prolog which roles may write an acuity, rather
    than holding a fourth hand-written copy of the set (see the sync comments in
    rules/monitor.pl and policy/monitor.rego).
    """
    assert prolog.charge_roles() == frozenset({"charge_nurse", "shift_lead"})
