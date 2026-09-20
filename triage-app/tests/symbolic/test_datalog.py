"""The Datalog layer (`app.symbolic.datalog`): invariants over *all* timers and
cases at once — the deadline arm of the temporal monitor (I15/I16). Pure:
rows in, findings out.
"""

from __future__ import annotations

from app.symbolic import datalog


def _timer(case_id, fire_state, kind="reassessment", cycle=0):
    return {"timer_id": f"{case_id}:{kind}:{cycle}", "case_id": case_id, "kind": kind, "fire_state": fire_state}


def _case(case_id, control_state="monitoring", clinical_status="waiting"):
    return {"case_id": case_id, "control_state": control_state, "clinical_status": clinical_status}


def test_nothing_to_report_on_empty_input():
    assert datalog.tick_invariants([], []) == {"unwatched": [], "orphan": []}


def test_a_waiting_case_with_a_live_timer_is_watched():
    for state in ("SCHEDULED", "DUE", "DISPATCHING", "FAILED", "UNKNOWN"):
        assert datalog.tick_invariants([_timer("a", state)], [_case("a")])["unwatched"] == []


def test_a_waiting_case_whose_timers_are_all_finished_is_unwatched():
    """I16: a queued patient with no live reassessment timer has no deadline."""
    for state in ("DELIVERED", "CANCELLED", "ESCALATED_TO_HUMAN"):
        assert datalog.tick_invariants([_timer("a", state)], [_case("a")])["unwatched"] == ["a"]


def test_only_the_waiting_room_needs_a_watcher():
    rows = [_timer("a", "DELIVERED")]
    assert datalog.tick_invariants(rows, [_case("a", clinical_status="treatment_started")])["unwatched"] == []
    assert datalog.tick_invariants(rows, [_case("a", control_state="reassessment_required",
                                                 clinical_status="reassessment_required")])["unwatched"] == []


def test_a_reminder_does_not_count_as_watching():
    rows = [_timer("a", "SCHEDULED", kind="gate_reminder")]
    assert datalog.tick_invariants(rows, [_case("a")])["unwatched"] == ["a"]


def test_a_live_timer_for_a_missing_or_closed_case_is_an_orphan():
    rows = [_timer("ghost", "FAILED"), _timer("closed", "SCHEDULED"), _timer("ok", "SCHEDULED")]
    cases = [_case("ghost", control_state=None, clinical_status=None),
             _case("closed", control_state="case_closed", clinical_status="patient_released"),
             _case("ok")]
    assert datalog.tick_invariants(rows, cases)["orphan"] == [("closed", "closed:reassessment:0"),
                                                             ("ghost", "ghost:reassessment:0")]


def test_a_finished_timer_for_a_missing_case_is_not_an_orphan():
    assert datalog.tick_invariants([_timer("ghost", "CANCELLED")],
                                   [_case("ghost", control_state=None, clinical_status=None)])["orphan"] == []


def test_a_failed_timer_from_an_engine_refusal_does_not_count_as_watching():
    """I15/I16: a FAILED timer stuck on a genuine engine outage is retried by
    claim_retryable forever, but must not count as coverage here — the case
    should read as unwatched, same as if it had no timer at all."""
    for last_error in ("engine_unavailable:prolog (boom)", "layer_disagreement: bppy=DISPATCH prolog=cancel"):
        rows = [{**_timer("a", "FAILED"), "last_error": last_error}]
        assert datalog.tick_invariants(rows, [_case("a")])["unwatched"] == ["a"]

    # An ordinary FAILED (not an engine refusal) still counts as watched.
    rows = [{**_timer("a", "FAILED"), "last_error": "case not found"}]
    assert datalog.tick_invariants(rows, [_case("a")])["unwatched"] == []
