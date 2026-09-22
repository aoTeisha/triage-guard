"""Shared fixtures. Every test runs offline: no LLM, no CRM stub, no Langfuse."""

from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from app.graph import build_graph
from app.monitor import timers
from app.runner import hydrate

# Admin connection used only to create/drop each test's throwaway database.
# Needs `docker compose -f db/docker-compose.yml up -d postgres` running.
ADMIN_DSN = os.environ.get("POSTGRES_TEST_DSN", "postgresql://triage:triage@localhost:5434/postgres")


def _throwaway_db() -> str:
    """A fresh Postgres database for one test. Same isolation guarantee the
    old `tmp_path`-file-per-test gave, adapted to a real server: each test
    gets its own database instead of its own SQLite file.
    """
    name = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return ADMIN_DSN.rsplit("/", 1)[0] + f"/{name}"


def _drop_db(dsn: str) -> None:
    name = dsn.rsplit("/", 1)[1]
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Force mock mode, silence tracing, and isolate the shared timer store.

    `timers.connection()` caches one connection per process, reused by both
    the graph's `monitoring` node and the sweeper. Without clearing that
    cache between tests, the first test to reach `monitoring` would lock in
    a connection for the rest of the suite — potentially even the real
    `triage` database, if no earlier test had overridden its DSN.
    """
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    # Empty, not deleted: `observability.tracing_enabled()` calls load_dotenv(), which
    # fills in anything ABSENT from the environment. Deleting these let a developer's
    # .env switch tracing back on mid-suite and the run would block on localhost:3000.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    dsn = _throwaway_db()
    monkeypatch.setenv("TRIAGE_CHECKPOINT_DB", dsn)
    timers.connection.cache_clear()
    yield
    timers.connection.cache_clear()
    _drop_db(dsn)


@pytest.fixture
def conn():
    """The throwaway timer store for this test (used by test_timers.py,
    test_fire.py, test_sweeper.py — none of these read/write the checkpointer).
    Reuses `timers.connection()`, which `offline` already pointed at this
    test's throwaway database.
    """
    return timers.connection()


@pytest.fixture
def graph():
    """A compiled graph with a throwaway checkpoint database.

    A database of its own, separate from `conn`'s — the gate tests need a
    checkpoint that survives a rebuilt graph object, and nothing here needs
    the timer store and the checkpoint store to be the same database (they
    share one DSN in production, but that's a deployment choice, not a
    correctness requirement — the two are only ever joined by `case_id`, a
    plain string, never a real foreign key).
    """
    dsn = _throwaway_db()
    try:
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            yield build_graph(checkpointer=saver)
    finally:
        _drop_db(dsn)


@pytest.fixture
def run(graph):
    """Invoke the graph on a case and return (final_values, pending_gate)."""

    def _run(case: dict[str, Any], thread: str | None = None):
        thread = thread or f"t-{uuid.uuid4().hex[:8]}"
        result = graph.invoke(
            {
                "case_id": case["case_id"],
                "raw_payload": dict(case),
                "nurse_proposed_acuity": case.get("nurse_proposed_acuity"),
            },
            {"configurable": {"thread_id": thread}},
        )
        pending = result.get("__interrupt__")
        # Hydrate through the same helper the runner uses: LangGraph returns only
        # the channels a run touched, so an untouched field is absent rather than
        # holding its declared default. Tests assert on what a caller really sees.
        return hydrate(result), (dict(pending[0].value) if pending else None), thread

    return _run


@pytest.fixture
def checkpoint_db(monkeypatch):
    """A throwaway checkpoint database, wired into both `runner.graph` (where
    cases are written) and `runner.DSN` (which `all_case_summaries` and
    `case_lock` read). They must be the same database, or a test would be
    reading/locking a different store than the one it just wrote to.

    Unlike this file's own `graph` fixture, this one goes through
    `app.runner` itself — needed for any test that calls `runner.start_case`,
    `runner.resume_case`, `runner.all_case_summaries`, or `runner.case_lock`
    directly, since those all read the `runner.DSN` / `runner.graph` module
    attributes rather than taking a graph object as a parameter.
    """
    from app import runner

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


def arrows(state: dict[str, Any]) -> list[str]:
    """Arrow labels from a run's audit trail, in order."""
    return [r["arrow"] for r in state.get("audit_log", []) if r.get("arrow")]
