"""Router unit tests — the guards of the Transitions table, in isolation.

These replace the old `test_state_machine.py`. That suite tested a hand-written
transition table the Flow never consulted; these test the functions the compiled
graph actually calls, with no graph, no checkpointer and no I/O.
"""

from __future__ import annotations

import pytest

from app.budgets import MAX_CORRECTION_ROUNDS
from app.deterministic import assign_order_key, bucket_for, resolve_acuity
from app.events import Event
from app.graph import TriageState
from app.graph import routers
from app.labels import Route
from app.states import AcuityBucket, AcuitySource, ClinicalStatus


EARLY = "2026-09-19T10:00:00+00:00"
LATE = "2026-09-19T10:05:00+00:00"


def s(**kw) -> TriageState:
    return TriageState(case_id="t", **kw)


# ---- intake fan-out (arrows 4 / 16 / 17 / 18) -------------------------------


@pytest.mark.parametrize(
    "outcome",
    [e.value for e in Event][:0]
    or [
        "DATA_PARSED",
        "MISSING_FIELDS_DETECTED",
        "SUBMISSION_FAILED",
        "INVALID_INPUT_DETECTED",
    ],
)
def test_each_intake_outcome_routes_to_its_own_branch(outcome):
    assert routers.route_intake(s(intake_outcome=outcome)) == Event(outcome)


# ---- acuity bands (9a / 9b / 9c), Z3 band totality --------------------------


@pytest.mark.parametrize("nurse,system", [(1, 1), (3, 3), (5, 5)])
def test_gap_zero_keeps_the_agreed_level(nurse, system):
    final, source, arrow = resolve_acuity(nurse, system)
    assert final == nurse
    assert source is AcuitySource.HUMAN_CONFIRMED
    assert arrow.value == "9a"


@pytest.mark.parametrize("nurse,system", [(3, 2), (2, 3), (5, 4)])
def test_gap_one_settles_to_the_nurse(nurse, system):
    """Changed 2026-09-13. Was min(nurse, system) - "take the more acute".

    The classifier over-triages systematically (median ESI 2.0 where experts said 3.0),
    so it should not silently win every close call. See
    docs/plans/2026-09-13-acuity-classifier-design.md.
    """
    final, source, arrow = resolve_acuity(nurse, system)
    assert final == nurse
    assert source is AcuitySource.AUTO_RESOLVED
    assert arrow.value == "9b"


@pytest.mark.parametrize("nurse,system", [(5, 2), (1, 4), (5, 1)])
def test_gap_two_or_more_settles_nothing(nurse, system):
    """9c returns no acuity at all — not a sentinel that could be mistaken for one."""
    final, source, arrow = resolve_acuity(nurse, system)
    assert final is None
    assert source is None
    assert arrow.value == "9c"


def test_the_bands_are_total():
    """Exactly one arm fires for every reachable pair."""
    for nurse in range(1, 6):
        for system in range(1, 6):
            assert resolve_acuity(nurse, system)[2].value in {"9a", "9b", "9c"}


def test_a_case_with_no_system_acuity_escalates_rather_than_guessing():
    assert routers.route_acuity_gap(s(nurse_proposed_acuity=3)) is Route.ESCALATE


# ---- classifier degrade (V·retry / V·exhausted) -----------------------------


def test_classifier_retries_while_budget_remains():
    assert (
        routers.route_after_classify(s(retry_count={"acuity_classifier": 0}))
        is Route.RETRY
    )


def test_classifier_exhaustion_degrades_rather_than_halting():
    assert (
        routers.route_after_classify(s(retry_count={"acuity_classifier": 99}))
        is Route.EXHAUSTED
    )


def test_a_proposed_acuity_proceeds():
    assert routers.route_after_classify(s(system_proposed_acuity=3)) is Route.PROCEED


# ---- safety (10 / 10·fail / AF·safety) --------------------------------------


def test_a_passing_verdict_clears():
    from app.schemas import SafetyVerdict

    state = s(safety_verdict=SafetyVerdict(verdict="pass"), safety_passed=True)
    assert routers.route_after_safety(state) is Route.CLEARED


def test_a_failing_verdict_escalates():
    from app.schemas import SafetyVerdict

    state = s(safety_verdict=SafetyVerdict(verdict="fail"), safety_passed=False)
    assert routers.route_after_safety(state) is Route.ESCALATE


def test_a_missing_verdict_exhausts_into_the_human_route():
    assert (
        routers.route_after_safety(s(retry_count={"safety_validation": 99}))
        is Route.EXHAUSTED
    )


# ---- the gate (1b.z·* / BLK / loop guard) -----------------------------------


def test_a_non_charge_resolver_is_denied():
    assert routers.route_gate(s(resolver_role="nurse")) is Route.DENIED


@pytest.mark.parametrize("role", ["charge_nurse", "shift_lead"])
def test_a_charge_resolver_proceeds(role):
    assert routers.route_gate(s(resolver_role=role)) is Route.PROCEED


def test_the_correction_loop_is_bounded():
    """Without a bound, safety_fail -> gate -> safety_fail cycles forever. The spec
    requires a finite number of rounds; the number itself is a documented unknown.
    """
    spent = s(
        resolver_role="charge_nurse",
        escalation_reason="safety_fail",
        correction_rounds=MAX_CORRECTION_ROUNDS + 1,
    )
    assert routers.route_gate(spent) is Route.EXHAUSTED


# ---- queue ordering ------------------------------------------------------------


@pytest.mark.parametrize(
    "acuity,expected",
    [
        (1, AcuityBucket.EMERGENT),
        (2, AcuityBucket.EMERGENT),
        (3, AcuityBucket.QUEUED),
        (4, AcuityBucket.QUEUED),
        (5, AcuityBucket.QUEUED),
    ],
)
def test_bucket_boundary_is_between_two_and_three(acuity, expected):
    assert bucket_for(acuity) is expected


def test_arrival_breaks_ties_same_acuity():
    first = assign_order_key(3, EARLY)
    second = assign_order_key(3, LATE)
    assert first < second


# ---- awaiting_reassessment's three-way exit (move / release / reassess) ----


def _state(arrow=None, clinical_status=None):
    return s(
        clinical_status=clinical_status,
        audit_log=[{"arrow": arrow}] if arrow else [],
    )


def test_route_wait_resume_sends_blk_to_denied():
    assert routers.route_wait_resume(_state(arrow="BLK")) == Route.DENIED


def test_route_wait_resume_sends_release_to_released():
    assert routers.route_wait_resume(_state(arrow="REL")) == Route.RELEASED


def test_route_wait_resume_sends_a_case_already_in_treatment_back_to_moved():
    """Covers both the just-moved case and a stale timer firing afterward —
    both look identical to this router: clinical_status is treatment_started
    and the arrow is neither BLK nor REL.
    """
    assert (
        routers.route_wait_resume(
            _state(arrow="19", clinical_status=ClinicalStatus.TREATMENT_STARTED.value)
        )
        == Route.MOVED
    )
    assert (
        routers.route_wait_resume(
            _state(arrow="14", clinical_status=ClinicalStatus.TREATMENT_STARTED.value)
        )
        == Route.MOVED
    )


def test_route_wait_resume_defaults_to_proceed_for_a_normal_reassessment():
    assert (
        routers.route_wait_resume(_state(arrow="14", clinical_status="waiting"))
        == Route.PROCEED
    )
