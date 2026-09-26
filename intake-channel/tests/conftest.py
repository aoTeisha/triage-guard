"""Offline fixtures for intake-channel. Every test runs its cases in a
throwaway Postgres database, never the shared `triage` one.

Until 2026-09-26 this file did not exist: `/submit` went through the real
`app.runner`, so every test run left open cases for real CRM patients in the
shared database, and the triage-app suite then rejected its own clean demo
case as a duplicate (I19). Mirrors board/tests/conftest.py.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from app.graph import build_graph
from app.monitor import timers

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
def isolated_case_store(monkeypatch):
    """One throwaway database per test for the runner's graph, `runner.DSN`
    (the duplicate check and the case lock) and the timer store — the three
    places a case leaves a trace. Nothing here reaches the shared database.
    """
    from app import crm_client, runner

    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    # A release writes the visit to the CRM (I17); tests must not.
    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")

    dsn = _throwaway_db()
    monkeypatch.setenv("TRIAGE_CHECKPOINT_DB", dsn)
    timers.connection.cache_clear()
    try:
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            compiled = build_graph(checkpointer=saver)
            monkeypatch.setattr(runner, "graph", lambda: compiled)
            monkeypatch.setattr(runner, "DSN", dsn)
            yield
    finally:
        timers.connection.cache_clear()
        _drop_db(dsn)
