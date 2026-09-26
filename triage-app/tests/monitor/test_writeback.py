"""I17: each visit's clinical data reaches the CRM within T of the CRM being
reachable. The first attempt is made at release; a failed one becomes a
`crm_writeback` timer the sweeper retries until the CRM takes it.

The CRM is stubbed at `crm_client.patch_patient` — what these tests pin is the
decision and the record, not the HTTP call.
"""

from __future__ import annotations

from langgraph.types import Command

from app import crm_client
from app.mock_cases import DEMO_CASES
from app.monitor import bthreads, fire, timers
from app.runner import config_for, hydrate
from app.symbolic import datalog, prolog
from tests.test_release_everywhere import RELEASE


def _released(graph, run, monkeypatch, outcome: str):
    """Run the clean case to the queue, then release it, with the CRM answering `outcome`."""
    written: list[tuple[str, dict]] = []

    def fake_patch(stable_patient_id, visit, *, timeout=5.0):
        written.append((stable_patient_id, visit))
        return outcome

    monkeypatch.setattr(crm_client, "patch_patient", fake_patch)
    # thread == case_id, as in production: the timer is keyed by the case id and
    # the sweeper looks the case up by it, so the two must agree here too.
    _, _, thread = run(DEMO_CASES["clean"], thread=DEMO_CASES["clean"]["case_id"])
    graph.invoke(Command(resume=RELEASE), config_for(thread))
    return thread, hydrate(graph.get_state(config_for(thread)).values), written


def _actions(state) -> list[str]:
    return [r["action"] for r in state["audit_log"]]


# ---- at release ------------------------------------------------------------------


def test_a_release_writes_the_visit_when_the_crm_is_up(graph, run, monkeypatch, conn):
    thread, state, written = _released(graph, run, monkeypatch, "ok")

    assert "crm_updated" in _actions(state)
    assert written and written[0][1] == {"new_visit": crm_client.visit_record(state)}
    assert written[0][1]["new_visit"]["acuity"] == state["acuity"]
    assert timers.all_rows(conn) == [] or all(t["kind"] != "crm_writeback" for t in timers.all_rows(conn))


def test_a_release_with_the_crm_down_defers_and_schedules_the_retry(graph, run, monkeypatch, conn):
    thread, state, _ = _released(graph, run, monkeypatch, "db_error")

    assert "crm_writeback_deferred" in _actions(state)
    pending = [t for t in timers.all_rows(conn) if t["case_id"] == thread and t["kind"] == "crm_writeback"]
    assert len(pending) == 1


def test_the_write_back_record_comes_before_the_release_record(graph, run, monkeypatch):
    """I20: nothing but refusals after a release. The CRM record is written
    into the same update, ahead of `sign_release`, so `check_trace` stays clean."""
    _, state, _ = _released(graph, run, monkeypatch, "ok")
    actions = _actions(state)
    assert actions.index("crm_updated") < actions.index("sign_release")
    assert state["audit_log"][-1]["action"] == "sign_release"


def test_a_patient_with_no_crm_record_is_skipped_not_retried(graph, run, monkeypatch, conn):
    """Your call from the identity work: an unregistered patient continues with
    no internal id, so there is nothing to write the visit to."""
    monkeypatch.setattr(crm_client, "CRM_BASE_URL", "http://127.0.0.1:1")   # lookup fails: no id
    called = []
    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: called.append(a) or "ok")

    _, _, thread = run(DEMO_CASES["clean"])
    graph.invoke(Command(resume=RELEASE), config_for(thread))
    state = hydrate(graph.get_state(config_for(thread)).values)

    assert state["stable_patient_id"] is None
    assert "crm_writeback_skipped" in _actions(state)
    assert called == []
    assert all(t["kind"] != "crm_writeback" for t in timers.all_rows(conn))


def test_the_visit_carries_no_identifier_and_no_prose():
    record = crm_client.visit_record({"released_at": "2026-09-26T10:00:00+00:00", "acuity": 3,
                                      "redacted_payload": {"chief_complaint": "chest_pain"},
                                      "stable_patient_id": "P-1001", "national_id": "300000001"})
    assert record == {"date": "2026-09-26", "acuity": 3, "notes": "chest pain"}


# ---- the retry, through the monitor's layers --------------------------------------


def _writeback_ctx(**over):
    return {"timer_id": "t", "kind": "crm_writeback", "fire_state": "DUE", "control_state": "case_closed",
            "case_exists": True, "pause_active": False, "notify_count": 0, "notify_budget": 5,
            "recipient_class": "any_charge_nurse"} | over


def test_bppy_and_prolog_both_choose_writeback():
    selected, proposed = bthreads.select_action(_writeback_ctx())
    assert (selected, proposed) == ("WRITEBACK", "WRITEBACK")
    assert prolog.timer_action(_writeback_ctx())[0] == "writeback"


def test_the_retry_delivers_once_the_crm_is_back(graph, run, monkeypatch, conn):
    thread, _, _ = _released(graph, run, monkeypatch, "db_error")
    timer = next(t for t in timers.all_rows(conn) if t["case_id"] == thread and t["kind"] == "crm_writeback")

    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")
    assert fire.handle(conn, timer, graph=graph) == "DELIVERED"


def test_the_retry_stays_failed_while_the_crm_is_down(graph, run, monkeypatch, conn):
    thread, _, _ = _released(graph, run, monkeypatch, "db_error")
    timer = next(t for t in timers.all_rows(conn) if t["case_id"] == thread and t["kind"] == "crm_writeback")

    assert fire.handle(conn, timer, graph=graph) == "FAILED"
    row = next(t for t in timers.all_rows(conn) if t["timer_id"] == timer["timer_id"])
    assert row["last_error"] == "crm_unreachable"     # what claim_retryable picks up again


def test_a_write_back_for_an_open_case_is_refused_by_opa(graph, run, monkeypatch, conn):
    """Only a closed case has a visit to write."""
    _, _, thread = run(DEMO_CASES["clean"], thread=DEMO_CASES["clean"]["case_id"])   # still at the waiting-room pause
    timer_id = timers.schedule(conn, case_id=thread, kind="crm_writeback", schedule_seq=0,
                               due_at="2000-01-01T00:00:00Z")
    timer = next(t for t in timers.all_rows(conn) if t["timer_id"] == timer_id)
    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")

    assert fire.handle(conn, timer, graph=graph) == "CANCELLED"
    row = next(t for t in timers.all_rows(conn) if t["timer_id"] == timer_id)
    assert "case is not closed" in row["last_error"]


def test_a_pending_write_back_is_not_an_orphan():
    """The tick invariant flags a live timer on a closed case. A write-back is
    live on a closed case by definition, so it is the one kind exempted."""
    findings = datalog.tick_invariants(
        [{"case_id": "c", "kind": "crm_writeback", "timer_id": "c:crm_writeback:0", "fire_state": "FAILED",
          "last_error": "crm_unreachable"}],
        [{"case_id": "c", "control_state": "case_closed", "clinical_status": "patient_released"}],
    )
    assert findings["orphan"] == []
