"""Tests for board.intake — the case-creating endpoints and the pauses a nurse
answers from the intake side, now served by the board app on one origin.
"""

from __future__ import annotations

import threading
from typing import get_args

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.labels import Transition
from board.api import app
from board.mock_cases import PLANTED, TRACE_VIOLATIONS, SubmissionType
from board.patient_lookup import CRM_BASE_URL

client = TestClient(app)


def _crm(national_id: str = "300000005"):
    return respx.get(f"{CRM_BASE_URL}/patients/by-national-id/{national_id}").mock(
        return_value=httpx.Response(
            200, json={"status": "found", "record": {"stable_patient_id": "P-1005"}}
        )
    )


@respx.mock
def test_lookup_is_served_under_the_api_prefix():
    _crm()

    response = client.get("/api/lookup/300000005")

    assert response.status_code == 200
    assert response.json()["status"] == "found"


@respx.mock
def test_submit_is_served_under_the_api_prefix_and_runs_the_graph():
    _crm()

    body = client.post(
        "/api/submit", json={"national_id": "300000005", "submission_type": "clean"}
    ).json()

    assert body["control_state"] == "monitoring"
    assert body["case_id"].startswith("case-")


@respx.mock
def test_a_patient_the_crm_does_not_hold_cannot_have_a_case_opened():
    respx.get(f"{CRM_BASE_URL}/patients/by-national-id/999999999").mock(
        return_value=httpx.Response(404, json={"detail": "not_found"})
    )

    response = client.post(
        "/api/submit", json={"national_id": "999999999", "submission_type": "clean"}
    )

    assert response.status_code == 422
    assert "not registered" in response.json()["detail"]


@respx.mock
def test_the_patient_picker_lists_the_crm_patients():
    respx.get(f"{CRM_BASE_URL}/patients").mock(
        return_value=httpx.Response(
            200, json={"patients": [{"national_id": "300000005", "name": "David Friedman"}]}
        )
    )

    body = client.get("/api/patients").json()

    assert body["patients"] == [{"national_id": "300000005", "name": "David Friedman"}]


@respx.mock
def test_the_patient_picker_is_a_503_when_the_crm_is_down():
    respx.get(f"{CRM_BASE_URL}/patients").mock(side_effect=httpx.ConnectError("refused"))

    assert client.get("/api/patients").status_code == 503


def test_an_unknown_submission_type_is_a_422_not_a_500():
    """Review Focus 3. `build_case` ends in `raise ValueError`, so the pydantic
    Literal on `submission_type` is the only thing between a typo and a stack
    trace. No CRM mock here on purpose: the request must be refused before
    anything reaches the lookup.
    """
    response = client.post(
        "/api/submit", json={"national_id": "300000005", "submission_type": "clen"}
    )

    assert response.status_code == 422


@respx.mock
def test_two_concurrent_submits_for_one_patient_do_not_open_two_cases():
    """Review Focus 5. Intake now runs inside the board process, which is
    already polling every five seconds, so two tabs submitting at once is newly
    easy to hit. `start_case` takes a patient-scoped lock, and the duplicate-case
    check — a patient with an active case cannot have a second one opened via
    intake — is what must then refuse the second one, either as a 409 or by the
    two submissions landing on one case rather than two open ones.

    Threads plus a barrier, matching how the board suite's existing
    single-active-writer concurrency tests force a real race —
    `ThreadPoolExecutor` would often let the first request finish before the
    second even starts.
    """
    from app.runner import all_case_ids

    _crm()
    payload = {"national_id": "300000005", "submission_type": "clean"}
    barrier = threading.Barrier(2)
    responses = {}

    def call(key):
        barrier.wait()
        responses[key] = client.post("/api/submit", json=payload)

    first = threading.Thread(target=call, args=("first",))
    second = threading.Thread(target=call, args=("second",))
    first.start()
    second.start()
    first.join()
    second.join()

    accepted = [r for r in responses.values() if r.status_code == 200]
    assert accepted, "at least one submission must succeed"
    # However the race resolved, the patient must not end up with two open
    # cases sitting in the queue at once.
    open_cases = [
        case_id for case_id in all_case_ids()
        if client.get(f"/api/case/{case_id}").json()["view"]["control_state"] == "monitoring"
    ]
    assert len(open_cases) <= 1


@respx.mock
def test_two_concurrent_gate_answers_do_not_both_apply():
    """Review Focus 2's endpoint-level fallback. `board/tests/board/test_commands.py`'s
    version of this race skips because `app.mock_cases.DEMO_CASES` has no "gap" case;
    the plan names this file as where the coverage lands instead, reached through
    `/api/submit` with `submission_type="gap"`.

    A double-clicked "Use system's acuity" must resolve the gate exactly once.
    Whichever request loses either sees a 409 (the case is no longer at the gate)
    or 500 (`CaseLockTimeout`, if the other request still held the lock) — it must
    not silently report the winner's success as its own.
    """
    _crm()
    paused = client.post(
        "/api/submit", json={"national_id": "300000005", "submission_type": "gap"}
    ).json()
    case_id = paused["case_id"]

    barrier = threading.Barrier(2)
    responses = {}

    def resolve(key):
        barrier.wait()
        responses[key] = client.post(
            f"/api/case/{case_id}/resume",
            json={"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
        )

    first = threading.Thread(target=resolve, args=("first",))
    second = threading.Thread(target=resolve, args=("second",))
    first.start()
    second.start()
    first.join()
    second.join()

    applied = [r for r in responses.values() if r.status_code == 200]
    refused = [r for r in responses.values() if r.status_code != 200]
    assert len(applied) == 1, "exactly one gate answer may be applied"
    assert len(refused) == 1


@respx.mock
def test_resume_is_served_per_case_and_still_returns_a_bare_case_view():
    """The new URL, the old body. `/resume` has always replied with the case view
    and nothing wrapped around it, and that is what the page reads.
    """
    _crm()
    paused = client.post(
        "/api/submit", json={"national_id": "300000005", "submission_type": "gap"}
    ).json()

    resumed = client.post(
        f"/api/case/{paused['case_id']}/resume",
        json={"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
    ).json()

    assert resumed["status"] == "settled"
    assert resumed["control_state"] == "monitoring"
    assert resumed["acuity_source"] == "human_confirmed"
    # No wrapper keys leaked in from the shared writer.
    assert "accepted" not in resumed
    assert "refusal" not in resumed


@respx.mock
def test_an_unauthorized_resolver_is_refused_the_same_way_as_before():
    """A BLK reaches the browser as it always has: HTTP 200, the case still
    gated, and the refusal visible as a BLK row in the trail. The gate stays open
    so the same form can be answered by someone authorized.
    """
    _crm()
    paused = client.post(
        "/api/submit", json={"national_id": "300000005", "submission_type": "gap"}
    ).json()

    denied = client.post(
        f"/api/case/{paused['case_id']}/resume",
        json={"decision": "use_system_acuity", "resolver_role": "nurse"},
    )

    assert denied.status_code == 200
    body = denied.json()
    assert body["status"] == "awaiting_human_approval"
    assert body["gate"] is not None
    assert body["acuity"] is None
    assert any(r["transition"] == Transition.BLK.value for r in body["audit_log"])


def test_reassess_validates_the_payload_before_it_touches_the_case():
    """The endpoint's own `unusable_fields` check runs before it looks the case
    up, so an unknown case id carrying an unusable value is a 422 rather than a
    404. Deliberate: validating first avoids a database read for a request that
    could never be applied.

    A free-text complaint is the case that reaches this check, because pydantic
    only types `chief_complaint` as `str` — a complaint must be a code from the
    fixed set, since the model receives only approved, fixed-choice fields, or
    the model would be shown typed prose and the payload would be refused three
    nodes later by the privacy policy. An out-of-range
    acuity would not prove the ordering: `Field(ge=1, le=5)` makes pydantic
    reject that one before the handler body runs at all.
    """
    response = client.post(
        "/api/case/does-not-exist/reassess",
        json={
            "nurse_proposed_acuity": 3,
            "chief_complaint": "crushing chest pain since this morning",
            "vitals": {},
        },
    )

    assert response.status_code == 422
    assert "chief_complaint" in response.json()["detail"]


def test_reassess_refuses_an_impossible_vital():
    """A phone number typed into heart rate has the right type, so only the
    believability bound in `unusable_fields` refuses it."""
    response = client.post(
        "/api/case/does-not-exist/reassess",
        json={"nurse_proposed_acuity": 3, "chief_complaint": "chest_pain", "vitals": {"hr": 501234567}},
    )

    assert response.status_code == 422
    assert "vitals" in response.json()["detail"]


def test_reassess_404s_for_an_unknown_case_when_the_payload_is_valid():
    response = client.post(
        "/api/case/does-not-exist/reassess",
        json={"nurse_proposed_acuity": 3, "chief_complaint": "chest_pain", "vitals": {}},
    )

    assert response.status_code == 404


def test_fields_rejects_anything_that_is_not_an_intake_form_field():
    response = client.post("/api/case/any-case/fields", json={"case_id": "x", "bogus": 1})

    assert response.status_code == 422


def test_recover_404s_for_an_unknown_case():
    assert client.post("/api/case/no-such-case/recover").status_code == 404


def test_a_case_lock_timeout_is_a_409_not_a_500(monkeypatch):
    """`commands.answer_pause` carries no wrapper around `CaseLockTimeout`
    (another writer still holding the case past the 5s timeout), so these four
    endpoints wrap it themselves — the browser must not see a bare, unreadable
    500 for it.
    """
    from app.runner import CaseLockTimeout
    from board import intake as intake_module

    def boom(*args, **kwargs):
        raise CaseLockTimeout("case is locked by another writer")

    monkeypatch.setattr(intake_module.commands, "answer_pause", boom)

    calls = [
        ("/api/case/any-case/resume",
         {"decision": "use_system_acuity", "resolver_role": "charge_nurse"}),
        ("/api/case/any-case/reassess",
         {"nurse_proposed_acuity": 3, "chief_complaint": "chest_pain", "vitals": {}}),
        ("/api/case/any-case/fields", {}),
    ]
    for path, body in calls:
        response = client.post(path, json=body)
        assert response.status_code == 409, f"{path} returned {response.status_code}"

    assert client.post("/api/case/any-case/recover").status_code == 409


@respx.mock
def test_resume_keeps_its_old_refusal_wording_for_a_case_not_at_the_gate():
    """`/resume`'s refusal text is part of the preserved contract: it uses a
    bespoke "case is not awaiting human approval", not the generic `paused_at`
    template ("case is not paused at {node}") that `/fields` and `/recover`
    use. A nurse reading a raw node name in place of that sentence is a
    response-body change this endpoint is specifically written to avoid.
    """
    _crm()
    settled = client.post(
        "/api/submit", json={"national_id": "300000005", "submission_type": "clean"}
    ).json()

    denied = client.post(
        f"/api/case/{settled['case_id']}/resume",
        json={"decision": "use_system_acuity", "resolver_role": "charge_nurse"},
    )

    assert denied.status_code == 409
    assert denied.json()["detail"] == "case is not awaiting human approval"


# ---- real case: the nurse types the fields --------------------------------

REAL_FIELDS = {
    "nurse_proposed_acuity": 3,
    "chief_complaint": "chest_pain",
    "vitals": {"hr": 104, "bp": "148/92", "spo2": 95, "temp_c": 37.1},
}


@respx.mock
def test_a_complete_real_case_runs_the_clean_path():
    _crm()

    body = client.post("/api/submit", json={"national_id": "300000005", "fields": REAL_FIELDS}).json()

    assert body["control_state"] == "monitoring"
    assert body["acuity"] is not None


@respx.mock
def test_a_partial_real_case_is_sent_as_is_and_asks_for_the_rest():
    _crm()

    body = client.post(
        "/api/submit", json={"national_id": "300000005", "fields": {"chief_complaint": "chest_pain"}}
    ).json()

    assert "nurse_proposed_acuity" in body["missing_fields"]


@pytest.mark.parametrize("field", [{"acuity": 1}, {"free_text": "typed prose"}])
def test_a_real_case_refuses_a_field_that_is_not_on_the_form(field):
    """The real form has no free-text box: the model never sees free text (I12)."""
    response = client.post("/api/submit", json={"national_id": "300000005", "fields": field})

    assert response.status_code == 422


def test_a_real_case_refuses_a_complaint_outside_the_fixed_set():
    response = client.post(
        "/api/submit", json={"national_id": "300000005", "fields": {"chief_complaint": "sore"}}
    )

    assert response.status_code == 422


def test_a_submission_must_be_either_a_real_case_or_a_demo_case():
    both = {"national_id": "300000005", "submission_type": "clean", "fields": REAL_FIELDS}
    neither = {"national_id": "300000005"}

    assert client.post("/api/submit", json=both).status_code == 422
    assert client.post("/api/submit", json=neither).status_code == 422


# ---- trace-violation demo: plants records the trace check must catch --------

@pytest.mark.parametrize("rule", sorted(TRACE_VIOLATIONS))
@respx.mock
def test_each_planted_violation_trips_exactly_its_own_rule(rule):
    _crm()

    body = client.post(
        "/api/submit",
        json={"national_id": "300000005", "submission_type": "trace_violation", "violation": rule},
    ).json()

    assert body["trace_safety"] is False
    assert body["trace_violations"]
    assert all(rule.replace("_", " ") in v for v in body["trace_violations"]), body["trace_violations"]
    assert any(r["action"] == PLANTED for r in body["audit_log"])


@pytest.mark.parametrize("violation", [None, "not_a_rule"])
def test_a_trace_violation_needs_a_known_rule(violation):
    response = client.post(
        "/api/submit",
        json={"national_id": "300000005", "submission_type": "trace_violation", "violation": violation},
    )

    assert response.status_code == 422


@respx.mock
def test_planted_records_stay_out_of_the_notification_strip():
    _crm()
    client.post(
        "/api/submit",
        json={"national_id": "300000005", "submission_type": "trace_violation",
              "violation": "single_treatment_start"},
    )

    notes = client.get("/api/board").json()["notifications"]

    assert not any(n["action"] == PLANTED for n in notes)


# ---- demo scenarios: what each one sends, for the form's preview ----------

def test_demo_cases_serves_every_scenario_payload_as_it_will_be_sent():
    body = client.get("/api/demo-cases?national_id=300000005").json()

    assert set(body["cases"]) == set(get_args(SubmissionType))
    assert body["cases"]["clean"]["national_id"] == "300000005"
    assert "vitals" not in body["cases"]["missing"]
    assert "national_id" not in body["cases"]["failed"]
    # case_id is minted fresh on every submit, so a preview cannot show it.
    assert not any("case_id" in c for c in body["cases"].values())
    assert set(body["planted"]) == set(TRACE_VIOLATIONS)
    assert body["planted"]["single_treatment_start"] == ["move_confirmed", "move_confirmed"]


def test_demo_cases_needs_no_patient_lookup():
    """No CRM mock on purpose: the preview never looks the patient up."""
    assert client.get("/api/demo-cases").status_code == 200


def test_static_files_are_always_rechecked_by_the_browser():
    """A stale cached script against fresh HTML once left the form without its fields."""
    assert client.get("/static/intake.js").headers["cache-control"] == "no-cache"


def test_the_injection_scenario_no_longer_exists():
    """The form has no free-text box, so there is nothing for an injection to ride in."""
    response = client.post(
        "/api/submit", json={"national_id": "300000005", "submission_type": "injection"}
    )
    assert response.status_code == 422

    assert "injection" not in client.get("/api/demo-cases").json()["cases"]
