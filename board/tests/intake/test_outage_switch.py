"""The outage switch on the board: a component switched down stays down for
every patient until switched back, and every action still has someone who can
take it.
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import board.api as api_module
from app import outages
from app.labels import Transition
from board.patient_lookup import CRM_BASE_URL

client = TestClient(api_module.app)


@pytest.fixture
def switch(monkeypatch):
    monkeypatch.setenv("DEMO_OUTAGES", "1")
    for component in outages.COMPONENTS:
        outages.set_down(component, False)
    yield
    for component in outages.COMPONENTS:
        outages.set_down(component, False)


def _crm():
    return respx.get(f"{CRM_BASE_URL}/patients/by-national-id/300000005").mock(
        return_value=httpx.Response(200, json={"status": "found",
                                               "record": {"stable_patient_id": "P-1005"}}))


def _submit(kind: str = "clean"):
    return client.post("/api/submit", json={"national_id": "300000005",
                                            "submission_type": kind}).json()


def _down(component: str, down: bool = True):
    return client.put(f"/api/outages/{component}", json={"down": down})


def _transitions(view):
    return [r.get("transition") for r in view["audit_log"]]


# ---- the switch itself -------------------------------------------------------------


def test_the_switch_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("DEMO_OUTAGES", raising=False)

    assert client.get("/api/outages").json()["enabled"] is False
    assert _down("opa").status_code == 404


def test_switching_a_component_down_and_back(switch):
    body = _down("opa").json()
    assert body["down"] == ["opa"]
    assert client.get("/api/outages").json()["down"] == ["opa"]

    assert _down("opa", False).json()["down"] == []


def test_an_unknown_component_is_a_404(switch):
    assert _down("not_a_component").status_code == 404


# ---- every patient, every action ---------------------------------------------------


@respx.mock
def test_llm_down_new_cases_queue_on_the_nurses_acuity(switch):
    _crm()
    _down("llm")

    body = _submit()

    assert body["control_state"] == "monitoring"
    assert "acuity_classifier" in body["degraded"]
    # Still down for the next patient: the switch stays until switched back.
    assert client.get("/api/outages").json()["down"] == ["llm"]


@respx.mock
def test_opa_down_the_model_is_skipped_and_a_shift_lead_moves_and_releases(switch, monkeypatch):
    from app import crm_client

    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")
    _crm()
    _down("opa")

    queued = _submit()
    assert queued["control_state"] == "monitoring"
    assert Transition.PRIVACY_GATE_DOWN in _transitions(queued)
    case = queued["case_id"]

    refused = client.post(f"/api/case/{case}/move-to-treatment", json={"actor_role": "charge_nurse"})
    assert "shift lead sign-off required" in str(refused.json())

    moved = client.post(f"/api/case/{case}/move-to-treatment", json={"actor_role": "shift_lead"})
    assert moved.status_code == 200
    released = client.post(f"/api/case/{case}/release",
                           json={"reason": "discharge", "actor_role": "shift_lead"})
    assert released.status_code == 200
    view = client.get(f"/api/case/{case}").json()["view"]
    assert view["control_state"] == "case_closed"


@respx.mock
def test_prolog_down_goes_to_the_validator_down_gate(switch):
    _crm()
    _down("prolog")

    paused = _submit()

    assert paused["gate"]["gate"] == "validator_down"
    assert paused["validator_down"] == ["prolog"]


@respx.mock
def test_datalog_down_does_not_touch_the_safety_check(switch):
    """Datalog runs only the sweeper's deadline pass; safety validation is Prolog's."""
    _crm()
    _down("datalog")

    body = _submit()

    assert body["control_state"] == "monitoring"
    assert body.get("validator_down", []) == []


@respx.mock
def test_prolog_down_a_shift_lead_answers_and_revalidates_once_it_is_back(switch):
    _crm()
    _down("prolog")
    paused = _submit()
    case = paused["case_id"]

    refused = client.post(f"/api/case/{case}/resume",
                          json={"decision": "revalidate", "resolver_role": "charge_nurse"}).json()
    assert refused["gate"]["gate"] == "validator_down"

    _down("prolog", False)
    resumed = client.post(f"/api/case/{case}/resume",
                          json={"decision": "revalidate", "resolver_role": "charge_nurse"}).json()
    assert resumed["control_state"] == "monitoring"
    assert resumed["trace_safety"] is True
