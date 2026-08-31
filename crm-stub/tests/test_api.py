"""Tests for the FastAPI layer — status-code mapping for the three outcomes,
write-back, and the runtime simulate-down toggle.
"""

import pytest
from fastapi.testclient import TestClient

import crm.api as api_module
from crm.repository import CRMRepository
from crm.seed import seed


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "api.db"
    seed(str(db))
    # point the app's repo at the temp DB
    api_module.repo = CRMRepository(db_path=str(db))
    return TestClient(api_module.app)


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["available"] is True


def test_get_found(client):
    r = client.get("/patients/P-1001")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "found"
    assert body["record"]["name"] == "Alon Mizrahi"


def test_get_not_found_is_404(client):
    r = client.get("/patients/P-0000")
    assert r.status_code == 404
    assert r.json()["detail"] == "not_found"


def test_get_db_error_is_503(client):
    client.post("/admin/simulate-down", params={"enabled": True})
    r = client.get("/patients/P-1001")
    assert r.status_code == 503
    assert r.json()["detail"] == "db_error"
    # health also reports down
    assert client.get("/health").json()["available"] is False


def test_patch_writeback(client):
    r = client.patch(
        "/patients/P-1002",
        json={"new_visit": {"date": "2026-08-30", "acuity": 4, "notes": "sprain"}},
    )
    assert r.status_code == 200
    got = client.get("/patients/P-1002").json()
    assert got["record"]["prior_visits"][-1]["notes"] == "sprain"


def test_simulate_down_toggle_back(client):
    client.post("/admin/simulate-down", params={"enabled": True})
    assert client.get("/health").json()["available"] is False
    client.post("/admin/simulate-down", params={"enabled": False})
    assert client.get("/health").json()["available"] is True
