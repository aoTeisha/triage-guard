"""Helpers shared by every board test, whichever endpoint family it covers.

The per-family fixtures live one directory down — `tests/board/conftest.py` for
the projection and the manual status changes, `tests/intake/conftest.py` for
case creation and the pauses answered from the intake side. Both sets are
autouse and both build throwaway databases, so keeping them apart is what stops
one test from creating three databases and pointing the graph at a different one
than the reader.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from app import runner
from app.deterministic import assign_order_key, bucket_for, now_iso
from app.graph import build_graph
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


@contextmanager
def _wired_graph_db(monkeypatch, dsn: str):
    """Point `runner.graph`/`runner.DSN` at a compiled graph backed by `dsn`,
    for the duration of the `with` block. Shared by both suites' throwaway-db
    fixtures — building the graph and wiring the runner is identical either
    way; only what surrounds it (env vars, timer cache) differs.
    """
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
        compiled = build_graph(checkpointer=saver)
        monkeypatch.setattr(runner, "graph", lambda: compiled)
        monkeypatch.setattr(runner, "DSN", dsn)
        yield


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
