"""The Datalog layer (`app.symbolic.datalog`): invariants over *all* timers and
cases at once — the deadline arm of the temporal monitor (I15/I16). Pure:
rows in, findings out.
"""

from __future__ import annotations

from app.symbolic import datalog


def _timer(case_id, fire_state, kind="reassessment", schedule_seq=0):
    return {"timer_id": f"{case_id}:{kind}:{schedule_seq}", "case_id": case_id, "kind": kind, "fire_state": fire_state}


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


def test_an_opa_wrapped_engine_refusal_does_not_count_as_watching():
    """fire.dispatch/_send_reminder wrap the refusal marker inside their own
    "opa denied ..." message, not as a bare prefix — the check must still
    catch it."""
    for last_error in ("opa denied dispatch: engine_unavailable:opa (boom)",
                       "opa denied notify: engine_unavailable:opa (boom)"):
        rows = [{**_timer("a", "FAILED"), "last_error": last_error}]
        assert datalog.tick_invariants(rows, [_case("a")])["unwatched"] == ["a"]


# ---- I19: no duplicate active case -----------------------------------------

def _case_summary(case_id, control_state="monitoring", stable_patient_id="P-1"):
    return {"case_id": case_id, "control_state": control_state, "stable_patient_id": stable_patient_id}


def test_no_duplicate_when_patient_has_no_other_case():
    rows = [_case_summary("a", stable_patient_id="P-1")]
    assert datalog.find_duplicate_active_case("P-2", rows) is None


def test_finds_the_other_active_case_for_the_same_patient():
    rows = [_case_summary("a", stable_patient_id="P-1")]
    assert datalog.find_duplicate_active_case("P-1", rows) == "a"


def test_a_closed_case_for_the_same_patient_is_not_a_duplicate():
    rows = [_case_summary("a", control_state="case_closed", stable_patient_id="P-1")]
    assert datalog.find_duplicate_active_case("P-1", rows) is None


def test_an_input_rejected_case_for_the_same_patient_is_not_a_duplicate():
    """`input_rejected` is terminal too (build.py routes it straight to END,
    same as case_closed) — it must not chain into blocking every later
    resubmission for the same patient forever (production bug, P-1003)."""
    rows = [_case_summary("a", control_state="input_rejected", stable_patient_id="P-1")]
    assert datalog.find_duplicate_active_case("P-1", rows) is None


def test_a_chain_of_input_rejected_cases_never_blocks_a_later_resubmission():
    """The exact production shape: case A was correctly rejected against a
    then-open case (now closed and gone from the active set); B and C were
    each wrongly rejected against the previous ghost. A fourth submission
    must succeed — none of A, B, C may count as an active duplicate."""
    rows = [
        _case_summary("a", control_state="input_rejected", stable_patient_id="P-1"),
        _case_summary("b", control_state="input_rejected", stable_patient_id="P-1"),
        _case_summary("c", control_state="input_rejected", stable_patient_id="P-1"),
    ]
    assert datalog.find_duplicate_active_case("P-1", rows) is None


def test_no_stable_patient_id_never_matches():
    rows = [_case_summary("a", stable_patient_id=None)]
    assert datalog.find_duplicate_active_case(None, rows) is None
    assert datalog.find_duplicate_active_case("", rows) is None


def test_no_other_cases_at_all_is_not_a_duplicate():
    """The very first case ever started: `all_case_summaries` returns an
    empty list, so neither `active` nor `same_patient` gets asserted at all
    this call. Must not raise (pyDatalog only errors on an undefined
    *negated* predicate, not an undefined positive one)."""
    assert datalog.find_duplicate_active_case("P-1", []) is None


# ---- I21: audit log append-only (provenance check) -------------------------

def _rec(n):
    return {"at": f"t{n}", "action": f"action-{n}"}


def test_monotonic_audit_history_passes():
    history = [{"audit_log": []}, {"audit_log": [_rec(1)]}, {"audit_log": [_rec(1), _rec(2)]}]
    ok, why = datalog.audit_log_is_monotonic(history)
    assert ok is True
    assert why == ""


def test_a_shrunk_audit_log_fails():
    history = [{"audit_log": [_rec(1), _rec(2)]}, {"audit_log": [_rec(1)]}]
    ok, why = datalog.audit_log_is_monotonic(history)
    assert ok is False
    assert "checkpoint 1" in why


def test_a_rewritten_record_fails_even_at_the_same_length():
    history = [{"audit_log": [_rec(1)]}, {"audit_log": [{"at": "t1", "action": "changed"}]}]
    ok, why = datalog.audit_log_is_monotonic(history)
    assert ok is False


def test_empty_history_is_trivially_monotonic():
    assert datalog.audit_log_is_monotonic([]) == (True, "")
