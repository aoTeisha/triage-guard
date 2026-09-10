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
from app.states import AcuityBucket, AcuitySource


def s(**kw) -> TriageState:
    return TriageState(case_id="t", **kw)


# ---- intake fan-out (arrows 4 / 16 / 17 / 18) -------------------------------


@pytest.mark.parametrize("outcome", [e.value for e in Event][:0] or [
    "DATA_PARSED", "MISSING_FIELDS_DETECTED", "SUBMISSION_FAILED", "INVALID_INPUT_DETECTED",
])
def test_each_intake_outcome_routes_to_its_own_branch(outcome):
    assert routers.route_intake(s(intake_outcome=outcome)) == Event(outcome)


# ---- acuity bands (9a / 9b / 9c), Z3 band totality --------------------------


@pytest.mark.parametrize("nurse,system", [(1, 1), (3, 3), (5, 5)])
def test_gap_zero_keeps_the_agreed_level(nurse, system):
    final, source, arrow = resolve_acuity(nurse, system)
    assert final == nurse
    assert source is AcuitySource.HUMAN_CONFIRMED
    assert arrow.value == "9a"


@pytest.mark.parametrize("nurse,system,expected", [(3, 2, 2), (2, 3, 2), (5, 4, 4)])
def test_gap_one_takes_the_more_acute(nurse, system, expected):
    final, source, arrow = resolve_acuity(nurse, system)
    assert final == expected
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
    assert routers.route_after_classify(s(retry_count={"acuity_classifier": 0})) is Route.RETRY


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


@pytest.mark.parametrize("acuity,expected", [
    (1, AcuityBucket.EMERGENT), (2, AcuityBucket.EMERGENT),
    (3, AcuityBucket.QUEUED), (4, AcuityBucket.QUEUED), (5, AcuityBucket.QUEUED),
])
def test_bucket_boundary_is_between_two_and_three(acuity, expected):
    assert bucket_for(acuity) is expected


def test_emergent_always_sorts_ahead_of_queued():
    early_queued = assign_order_key(4, "2026-01-01T00:00:00+00:00")
    late_emergent = assign_order_key(1, "2026-12-31T23:59:59+00:00")
    assert late_emergent < early_queued


def test_arrival_breaks_ties_inside_a_bucket():
    first = assign_order_key(3, "2026-01-01T00:00:00+00:00")
    second = assign_order_key(5, "2026-01-01T00:00:01+00:00")
    assert first < second
