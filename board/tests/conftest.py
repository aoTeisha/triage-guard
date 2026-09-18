"""Offline fixtures. No LLM, no key, and deliberately no crm-stub running:
the board is required to keep rendering even when the CRM (the external
patient-lookup service) is down, and the only reliable way to prove that is
to never start one.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from app.deterministic import assign_order_key, bucket_for, now_iso
from app.monitor import timers
from app.states import AcuitySource, ClinicalStatus, State

# Admin connection used only to create/drop each test's throwaway database.
# Needs `docker compose -f db/docker-compose.yml up -d postgres` running.
ADMIN_DSN = os.environ.get("POSTGRES_TEST_DSN", "postgresql://triage:triage@localhost:5434/postgres")


def _throwaway_db() -> str:
    name = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"


def _drop_db(dsn: str) -> None:
    name = dsn.rsplit("/", 1)[1]
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def no_crm(monkeypatch):
    """Forces every test to run as if the CRM (patient-lookup service) is
    unreachable. Pinned here explicitly rather than left to depend on whether
    a crm-stub process happens to be running on :8000 — that made the suite
    pass or fail by accident depending on what else was running locally.
    """
    from app import crm_client

    monkeypatch.setattr(crm_client, "CRM_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture(autouse=True)
def isolated_timers(monkeypatch):
    """`timers.connection()` caches one connection per process, shared by the
    graph's `monitoring` node and the background sweeper. Give every test its
    own throwaway database so they can't leak state through that shared
    connection — not just tests that ask for `checkpoint_db` directly:
    `/api/board` and `/api/heartbeat` now open this same connection on every
    call (to report whether the sweeper looks alive), so any test hitting
    either endpoint needs its own isolated database, whether or not it
    creates a case.
    """
    dsn = _throwaway_db()
    monkeypatch.setenv("TRIAGE_CHECKPOINT_DB", dsn)
    timers.connection.cache_clear()
    yield
    timers.connection.cache_clear()
    _drop_db(dsn)


@pytest.fixture
def checkpoint_db(monkeypatch):
    """A throwaway checkpoint database, wired into both `runner.graph` (where
    cases are written) and `runner.DSN` (which the board reads to list
    cases). They must be the same database, or the board would be reading a
    different store than the one the test just wrote to.
    """
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    from langgraph.checkpoint.postgres import PostgresSaver

    from app import runner
    from app.graph import build_graph

    dsn = _throwaway_db()
    try:
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            compiled = build_graph(checkpointer=saver)
            monkeypatch.setattr(runner, "graph", lambda: compiled)
            monkeypatch.setattr(runner, "DSN", dsn)
            yield dsn
    finally:
        _drop_db(dsn)


def make_state(
    case_id: str,
    acuity: int,
    arrival: str,
    status: ClinicalStatus = ClinicalStatus.WAITING,
    **extra,
) -> dict:
    """Builds a case dict shaped the way the graph actually persists one:
    `order_key` comes from `assign_order_key` — the same function the graph
    itself calls — rather than being hand-computed by the test.
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
