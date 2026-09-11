"""Offline fixtures. No LLM, no key, and deliberately no crm-stub running:
rule 8 says the board renders through a CRM outage, and the way to assert that
is to never start one.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.deterministic import assign_order_key, bucket_for, now_iso
from app.states import AcuitySource, ClinicalStatus, State


@pytest.fixture(autouse=True)
def no_crm(monkeypatch):
    """The board must render through a CRM outage, so the suite runs with the CRM
    unreachable — pinned here rather than left to depend on whether a crm-stub
    happens to be running on :8000, which made the suite pass or fail by accident.
    """
    from app import crm_client

    monkeypatch.setattr(crm_client, "CRM_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture
def checkpoint_db(monkeypatch, tmp_path):
    """A throwaway checkpoint file, wired into both `runner.graph` (writes) and
    `runner.DB_PATH` (the board's enumeration reads). They must be the same file
    or the board would list one store and read another.
    """
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    from langgraph.checkpoint.sqlite import SqliteSaver

    from app import runner
    from app.graph import build_graph

    path = tmp_path / "ckpt.db"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    compiled = build_graph(checkpointer=SqliteSaver(conn))
    monkeypatch.setattr(runner, "graph", lambda: compiled)
    monkeypatch.setattr(runner, "DB_PATH", path)
    yield path
    conn.close()


def make_state(
    case_id: str,
    acuity: int,
    arrival: str,
    status: ClinicalStatus = ClinicalStatus.WAITING,
    **extra,
) -> dict:
    """A persisted-case shape, built the way the graph builds it: order_key comes
    from `assign_order_key`, never from the test.
    """
    return {
        "case_id": case_id,
        "stable_patient_id": f"P-{case_id}",
        "crm_status": "found",
        "control_state": State.MONITORING.value,
        "clinical_status": status.value,
        "acuity": acuity,
        "acuity_bucket": bucket_for(acuity).value,
        "acuity_source": AcuitySource.SYSTEM.value,
        "order_key": assign_order_key(acuity, arrival),
        "arrival_time": arrival,
        "audit_log": [],
        **extra,
    }


@pytest.fixture
def now():
    return now_iso()
