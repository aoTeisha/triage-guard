"""End-to-end tests for I19, I20, I21, I22 — the new safety invariants in
docs/SPECIFICATION.md's invariants table. Each test drives the real graph
through `app.runner`, against a real throwaway Postgres database, the same
way `board`'s own tests do (see `checkpoint_db` in this package's conftest,
copied from `board/tests/conftest.py`).
"""

from __future__ import annotations

import threading
import time as _time

import pytest

from app import runner
from app.crm_client import PatientLookupResult
from app.mock_cases import DEMO_CASES
from app.states import State


def _stub_found(monkeypatch, stable_patient_id: str):
    """Force `resolving_identity`'s CRM lookup to report `"found"` for
    `stable_patient_id`, regardless of what (if anything) is listening on
    `CRM_BASE_URL`. Patched at `app.graph.nodes.identity.fetch_patient` —
    that module imports the name directly, so patching `app.crm_client`'s
    copy would not take effect.
    """
    from app.graph.nodes import identity as identity_module

    def fake_fetch_patient(patient_id, *, timeout=1.0):
        if patient_id == stable_patient_id:
            return PatientLookupResult(status="found", record={"age_band": "adult"})
        return PatientLookupResult(status="not_found", record=None)

    monkeypatch.setattr(identity_module, "fetch_patient", fake_fetch_patient)


def test_i19_second_intake_for_the_same_found_patient_is_rejected(checkpoint_db, monkeypatch):
    _stub_found(monkeypatch, "P-i19")

    case_a = dict(DEMO_CASES["clean"])
    case_a["case_id"] = "case-i19-a"
    case_a["stable_patient_id"] = "P-i19"
    state_a, _ = runner.start_case(case_a)
    assert state_a["crm_status"] == "found"
    assert state_a["control_state"] != State.INPUT_REJECTED.value

    case_b = dict(DEMO_CASES["clean"])
    case_b["case_id"] = "case-i19-b"
    case_b["stable_patient_id"] = "P-i19"
    state_b, _ = runner.start_case(case_b)

    assert state_b["control_state"] == State.INPUT_REJECTED.value
    assert any(r.get("arrow") == "4b·duplicate" for r in state_b["audit_log"])


def test_i19_two_concurrent_intakes_for_the_same_patient_only_one_wins(checkpoint_db, monkeypatch):
    """The TOCTOU this test guards against: two `start_case` calls for the
    same patient, neither with a checkpoint committed yet when the other
    starts, racing into `resolving_identity`'s duplicate check at the same
    moment. Without a patient-scoped lock held for the whole `start_case`
    call (not just the check inside `resolving_identity`), both could pass
    `all_case_summaries` before either becomes visible to it.
    """
    _stub_found(monkeypatch, "P-i19-race")
    barrier = threading.Barrier(2)
    results = {}

    def submit(key, case_id):
        case = dict(DEMO_CASES["clean"])
        case["case_id"] = case_id
        case["stable_patient_id"] = "P-i19-race"
        barrier.wait()
        state, _ = runner.start_case(case)
        results[key] = state

    t1 = threading.Thread(target=submit, args=("a", "case-i19-race-a"))
    t2 = threading.Thread(target=submit, args=("b", "case-i19-race-b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    rejected = [r for r in results.values() if r["control_state"] == State.INPUT_REJECTED.value]
    accepted = [r for r in results.values() if r["control_state"] != State.INPUT_REJECTED.value]
    assert len(rejected) == 1
    assert len(accepted) == 1


def test_i19_a_new_intake_for_a_different_patient_is_not_rejected(checkpoint_db, monkeypatch):
    _stub_found(monkeypatch, "P-i19-other")

    case_a = dict(DEMO_CASES["clean"])
    case_a["case_id"] = "case-i19-c"
    case_a["stable_patient_id"] = "P-i19-other"
    runner.start_case(case_a)

    case_b = dict(DEMO_CASES["clean"])
    case_b["case_id"] = "case-i19-d"
    case_b["stable_patient_id"] = "P-different"
    state_b, _ = runner.start_case(case_b)

    assert state_b["control_state"] != State.INPUT_REJECTED.value


def test_i20_resubmitting_a_closed_case_id_is_refused(checkpoint_db):
    from app.actors import human_bridge

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = "case-i20"
    state, pending = runner.start_case(case)
    while pending and "gate" in pending:
        state, pending = runner.resume_case("case-i20", human_bridge.mock_resume_for(pending["gate"]))
    if pending and pending.get("waiting_room"):
        state, pending = runner.resume_case(
            "case-i20", {"event": "RELEASE_REQUESTED", "reason": "discharge", "actor_role": "charge_nurse"}
        )
    assert state["control_state"] == State.CASE_CLOSED.value

    with pytest.raises(runner.CaseClosedError):
        runner.start_case(case)


def test_i22_case_lock_serializes_two_holders(checkpoint_db):
    order = []
    barrier = threading.Event()

    def holder(name, hold_seconds):
        with runner.case_lock("case-i22"):
            order.append(f"{name}-start")
            barrier.wait(hold_seconds)
            order.append(f"{name}-end")

    t1 = threading.Thread(target=holder, args=("first", 0.2))
    t1.start()
    _time.sleep(0.05)  # let t1 actually acquire before t2 tries
    t2 = threading.Thread(target=holder, args=("second", 0))
    t2.start()
    t1.join()
    t2.join()

    # t2 must not start until t1 has fully finished — no interleaving.
    assert order == ["first-start", "first-end", "second-start", "second-end"]


def test_i22_case_lock_times_out_rather_than_hanging_forever(checkpoint_db):
    with runner.case_lock("case-i22-held"):
        with pytest.raises(runner.CaseLockTimeout):
            with runner.case_lock("case-i22-held", timeout_seconds=0.2):
                pass  # pragma: no cover — must never be entered


def test_i21_a_real_case_run_has_a_monotonic_audit_history(checkpoint_db):
    from app.actors import human_bridge
    from app.symbolic import datalog

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = "case-i21"
    state, pending = runner.start_case(case)
    while pending and "gate" in pending:
        state, pending = runner.resume_case("case-i21", human_bridge.mock_resume_for(pending["gate"]))

    history = runner.history("case-i21")
    ok, why = datalog.audit_log_is_monotonic(history)
    assert ok, why
