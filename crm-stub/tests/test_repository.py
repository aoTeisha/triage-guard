"""Tests for the CRM repository — the three fetch outcomes, write-back, and
the db-error simulation that exercises the fail-open path.
"""

import json
import sqlite3

import pytest

from crm.models import FetchStatus, PatchStatus
from crm.repository import CRMRepository, SCHEMA, _now_iso


@pytest.fixture
def repo(tmp_path):
    """A repository on a temporary file DB, seeded with one known patient."""
    db = tmp_path / "test.db"
    r = CRMRepository(db_path=str(db))
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.execute(
        """INSERT INTO patients
           (stable_patient_id, name, date_of_birth,
            known_conditions, prior_visits, last_updated)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "P-1001", "Alon Mizrahi", "1958-03-12",
            json.dumps(["hypertension", "type 2 diabetes"]),
            json.dumps([{"date": "2025-11-02", "acuity": 3, "notes": "chest tightness"}]),
            _now_iso(),
        ),
    )
    conn.commit()
    conn.close()
    return r


# -- the three fetch outcomes --------------------------------------------

def test_fetch_found(repo):
    result = repo.fetch_patient_data("P-1001")
    assert result.status is FetchStatus.FOUND
    assert result.db_reachable is True
    assert result.record.name == "Alon Mizrahi"
    assert "hypertension" in result.record.known_conditions
    assert result.record.prior_visits[0].acuity == 3


def test_fetch_not_found(repo):
    result = repo.fetch_patient_data("P-9999")
    assert result.status is FetchStatus.NOT_FOUND
    assert result.record is None
    # not_found is a normal empty, NOT a DB failure
    assert result.db_reachable is True


def test_fetch_db_error_via_flag(repo):
    repo._simulate_down = True
    result = repo.fetch_patient_data("P-1001")
    assert result.status is FetchStatus.DB_ERROR
    assert result.db_reachable is False  # this is what trips fail-open


# -- write-back -----------------------------------------------------------

def test_patch_appends_visit(repo):
    res = repo.patch_patient_data(
        "P-1001", {"new_visit": {"date": "2026-08-30", "acuity": 2, "notes": "SOB"}}
    )
    assert res.status is PatchStatus.OK
    after = repo.fetch_patient_data("P-1001")
    assert len(after.record.prior_visits) == 2
    assert after.record.prior_visits[-1].notes == "SOB"
    assert after.record.last_updated is not None


def test_patch_upserts_new_patient(repo):
    res = repo.patch_patient_data(
        "P-2002",
        {"name": "New Person", "date_of_birth": "2000-01-01",
         "known_conditions": ["asthma"]},
    )
    assert res.status is PatchStatus.OK
    fetched = repo.fetch_patient_data("P-2002")
    assert fetched.status is FetchStatus.FOUND
    assert fetched.record.known_conditions == ["asthma"]


def test_patch_db_error(repo):
    repo._simulate_down = True
    res = repo.patch_patient_data("P-1001", {"new_visit": {"date": "x", "acuity": 3}})
    assert res.status is PatchStatus.DB_ERROR
    assert res.ok is False


# -- availability ---------------------------------------------------------

def test_is_available_true(repo):
    assert repo.is_available() is True


def test_is_available_false_when_down(repo):
    repo._simulate_down = True
    assert repo.is_available() is False


# -- env-flag path (not just constructor arg) ----------------------------

def test_db_error_via_env(repo, monkeypatch):
    repo._simulate_down = None  # fall back to env
    monkeypatch.setenv("CRM_SIMULATE_DOWN", "true")
    assert repo.fetch_patient_data("P-1001").status is FetchStatus.DB_ERROR
    assert repo.is_available() is False
