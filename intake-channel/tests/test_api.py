"""Tests for channel.api — the HTTP layer over the real graph.

`/submit` used to return a canned dict from `MOCK_PARSE_RESULTS`. It now runs
the case through `app.graph`, so these tests exercise the control plane through
the same door a nurse uses, including the pause at the acuity gate.
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from langgraph.types import Command

import channel.api as api_module
from app.mock_cases import DEMO_CASES
from app.runner import config_for, start_case
from channel.patient_lookup import CRM_BASE_URL

client = TestClient(api_module.app)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Mock actors, no tracing, and a throwaway checkpoint database per test."""
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    import os
    import uuid

    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    from app import runner
    from app.graph import build_graph

    admin_dsn = os.environ.get("POSTGRES_TEST_DSN", "postgresql://triage:triage@localhost:5434/postgres")
    name = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = admin_dsn.rsplit("/", 1)[0] + f"/{name}"

    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
        compiled = build_graph(checkpointer=saver)
        monkeypatch.setattr(runner, "graph", lambda: compiled)
        yield

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _crm(status: int = 200, body: dict | None = None):
    return respx.get(f"{CRM_BASE_URL}/patients/P-1005").mock(
        return_value=httpx.Response(
            status, json=body or {"status": "found", "record": {"stable_patient_id": "P-1005"}}
        )
    )


def _submit(kind: str, patient: str = "P-1005"):
    return client.post(
        "/submit", json={"stable_patient_id": patient, "submission_type": kind}
    ).json()


# ---- lookup (unchanged behaviour) -------------------------------------------


@respx.mock
def test_lookup_found_returns_200_with_status_found():
    _crm(200, {"status": "found", "record": {"stable_patient_id": "P-1005", "name": "David Friedman"}})

    r = client.get("/lookup/P-1005")

    assert r.status_code == 200
    assert r.json()["status"] == "found"
    assert r.json()["record"]["name"] == "David Friedman"


@respx.mock
def test_lookup_not_found_still_returns_200():
    """not_found is a normal outcome, not an HTTP error — the caller must not have
    to branch on status codes to render it.
    """
    respx.get(f"{CRM_BASE_URL}/patients/P-9999").mock(
        return_value=httpx.Response(404, json={"detail": "not_found"})
    )

    r = client.get("/lookup/P-9999")

    assert r.status_code == 200
    assert r.json()["status"] == "not_found"
    assert r.json()["record"] is None


@respx.mock
def test_lookup_db_error_still_returns_200():
    _crm(503, {"detail": "db_error"})

    r = client.get("/lookup/P-1005")

    assert r.json()["status"] == "db_error"


# ---- submit now drives the graph --------------------------------------------


@respx.mock
def test_a_clean_submission_runs_the_whole_pipeline():
    _crm()

    body = _submit("clean")

    assert body["outcome"] == "DATA_PARSED"
    assert body["control_state"] == "monitoring"
    assert body["status"] == "settled"
    assert body["safety_passed"] is True
    assert body["acuity"] is not None


@respx.mock
def test_a_submission_returns_the_real_audit_trail():
    """The UI shows the spec's arrows, not a canned response."""
    _crm()

    body = _submit("clean")
    arrows = [r["arrow"] for r in body["audit_log"] if r["arrow"]]

    assert arrows[0] == "1a"
    # Ends on the Waiting Room Monitor's timer (arrow 13), the invoke half of the
    # 13/14 pair, not on the 11·pass transition that got the case there.
    assert arrows[-2:] == ["11·pass", "13"]
    assert arrows.index("7") < arrows.index("8")   # classifier invoke -> propose


@respx.mock
def test_missing_fields_stops_and_names_the_gaps():
    _crm()

    body = _submit("missing")

    assert body["outcome"] == "MISSING_FIELDS_DETECTED"
    assert body["control_state"] == "missing_fields_requested"
    assert "nurse_proposed_acuity" in body["missing_fields"]


@respx.mock
def test_an_unusable_submission_stops():
    _crm()

    body = _submit("failed")

    assert body["outcome"] == "SUBMISSION_FAILED"
    assert body["control_state"] == "submission_failed"


@respx.mock
def test_injection_is_rejected_and_never_classified():
    _crm()

    body = _submit("injection")

    assert body["outcome"] == "INVALID_INPUT_DETECTED"
    assert body["reason"].startswith("injection")
    assert body["acuity"] is None


@respx.mock
def test_submission_survives_a_crm_outage():
    """Fail-open: a CRM outage degrades the case, it does not block intake."""
    _crm(503, {"detail": "db_error"})

    body = _submit("clean")

    assert body["outcome"] == "DATA_PARSED"
    assert "crm" in body["degraded"]


@respx.mock
def test_case_ids_are_unique_across_submissions():
    _crm()

    assert _submit("clean")["case_id"] != _submit("clean")["case_id"]


# ---- the human gate, through the UI's endpoints ------------------------------


@respx.mock
def test_a_major_acuity_gap_pauses_and_surfaces_the_gate():
    _crm()

    body = _submit("gap")

    assert body["status"] == "awaiting_human_approval"
    assert body["gate"]["gate"] == "discrepancy"
    assert body["gate"]["required_role"] == "charge_nurse"
    assert body["acuity"] is None


@respx.mock
def test_a_charge_nurse_can_resolve_the_gate_and_the_case_completes():
    _crm()
    paused = _submit("gap")

    resumed = client.post(
        f"/resume/{paused['case_id']}",
        json={"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
    ).json()

    assert resumed["status"] == "settled"
    assert resumed["control_state"] == "monitoring"
    assert resumed["acuity_source"] == "human_confirmed"


@respx.mock
def test_an_unauthorized_resolver_is_refused_through_the_api():
    """BLK reaches the browser as a refusal, not as a silent success — and
    the case is still gated afterwards, so the same form can be answered
    again by someone who is authorized.
    """
    _crm()
    paused = _submit("gap")

    denied = client.post(
        f"/resume/{paused['case_id']}",
        json={"decision": "use_system_acuity", "resolver_role": "nurse"},
    ).json()

    assert denied["status"] == "awaiting_human_approval"
    assert denied["gate"] is not None
    assert denied["acuity"] is None
    assert any(r["arrow"] == "BLK" for r in denied["audit_log"])

    resolved = client.post(
        f"/resume/{paused['case_id']}",
        json={"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["acuity_source"] == "human_confirmed"


@respx.mock
def test_a_case_can_be_read_back_after_the_request_that_created_it():
    """State is checkpointed, so a later GET sees the same case."""
    _crm()
    submitted = _submit("clean")

    fetched = client.get(f"/case/{submitted['case_id']}").json()

    assert fetched["case_id"] == submitted["case_id"]
    assert fetched["control_state"] == "monitoring"


def test_reading_an_unknown_case_is_a_404():
    assert client.get("/case/case-does-not-exist").status_code == 404


# ---- the reassessment re-filing pause, through the UI's endpoint -----------


def _reach_refile_pause(case_id: str = "case-reassess-1"):
    """Start a clean case, let it clear to the queue, then fire its timer."""
    from app import runner

    case = dict(DEMO_CASES["clean"])
    case["case_id"] = case_id
    start_case(case, thread_id=case_id)
    runner.graph().invoke(
        Command(resume={"event": "REASSESSMENT_TIMEOUT", "fire_id": "f1"}),
        config_for(case_id),
    )
    return case


def test_reassess_endpoint_moves_the_case_on_with_fresh_observations():
    case = _reach_refile_pause()

    resp = client.post(
        f"/reassess/{case['case_id']}",
        json={
            "nurse_proposed_acuity": 1,
            "chief_complaint": "worsening chest pain",
            "vitals": {"hr": 140, "bp": "90/60", "spo2": 88, "temp_c": 38.2},
        },
    )

    assert resp.status_code == 200
    assert resp.json()["acuity"] == 1


def test_a_refile_always_parses_cleanly_and_never_ends_the_run():
    """The merge in `awaiting_reassessment_submission` carries `free_text` and
    `case_id` over from the original submission, and both are in
    REQUIRED_FIELDS. If someone rewrites that node to build a payload from
    scratch, every re-file would route to missing_fields_requested -> END and
    silently drop a patient who is physically still in the waiting room. This
    test is the tripwire for that.
    """
    case = _reach_refile_pause("case-reassess-2")

    resp = client.post(
        f"/reassess/{case['case_id']}",
        json={"nurse_proposed_acuity": 3, "chief_complaint": "unchanged", "vitals": {}},
    )

    assert resp.status_code == 200
    view = resp.json()
    assert view["outcome"] == "DATA_PARSED"
    assert view["control_state"] not in ("missing_fields_requested", "input_rejected")


def test_reassess_endpoint_404s_for_an_unknown_case():
    resp = client.post(
        "/reassess/does-not-exist",
        json={"nurse_proposed_acuity": 3, "chief_complaint": "x", "vitals": {}},
    )
    assert resp.status_code == 404


def test_reassess_endpoint_refuses_a_case_not_awaiting_a_refile():
    case = dict(DEMO_CASES["clean"])
    case["case_id"] = "case-not-waiting"
    start_case(case, thread_id=case["case_id"])  # paused at monitoring, not re-filing

    resp = client.post(
        f"/reassess/{case['case_id']}",
        json={"nurse_proposed_acuity": 3, "chief_complaint": "x", "vitals": {}},
    )

    assert resp.status_code == 409


def test_resume_endpoint_refuses_a_case_parked_at_the_refile_pause():
    """/resume answers the human-approval gate, not the re-filing pause. A
    ResumeRequest body has neither `nurse_proposed_acuity`, `chief_complaint`
    nor `vitals`, so without a guard `awaiting_reassessment_submission` would
    happily consume it, sending `None` into the case's clinical fields and
    dropping a patient who is still physically in the waiting room.
    """
    from app.runner import snapshot as raw_snapshot

    case = _reach_refile_pause("case-resume-guard")

    resp = client.post(
        f"/resume/{case['case_id']}",
        json={"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
    )

    assert resp.status_code == 409

    # `/case/{case_id}` (case_view) doesn't project raw_payload/nurse_proposed_acuity,
    # so check the persisted state directly -- proving the guard actually stopped the
    # run before it could consume the pause, not just that it returned an error code.
    values = raw_snapshot(case["case_id"])
    assert values["nurse_proposed_acuity"] == case["nurse_proposed_acuity"]
    assert values["raw_payload"]["chief_complaint"] == case["chief_complaint"]

    fetched = client.get(f"/case/{case['case_id']}").json()
    assert fetched["control_state"] == "reassessment_required"
