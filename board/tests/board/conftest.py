"""Fixtures for the board's own endpoints: the projection, the columns, the
ordering, and the four manual status changes.

Deliberately no crm-stub running: the board is required to keep rendering when
the CRM (the external patient-lookup service) is down, and the only reliable
way to prove that is to never start one.

These three fixtures are scoped to this directory rather than shared with the
intake tests next door. Both suites build throwaway Postgres databases, and two
autouse sets running over one test would create two or three of them per test,
with `runner.graph` patched by one and `runner.DSN` by another.
"""

from __future__ import annotations

import pytest

from app.monitor import timers

from ..conftest import _drop_db, _throwaway_db, _wired_graph_db


@pytest.fixture(autouse=True)
def no_crm(monkeypatch):
    """Forces every test to run as if the CRM (patient-lookup service) is
    unreachable. Pinned here explicitly rather than left to depend on whether a
    crm-stub process happens to be running on :8000 — that made the suite pass
    or fail by accident depending on what else was running locally.
    """
    from app import crm_client

    monkeypatch.setattr(crm_client, "CRM_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture(autouse=True)
def isolated_timers(monkeypatch):
    """`timers.connection()` caches one connection per process, shared by the
    graph's `monitoring` node and the background sweeper. Give every test its
    own throwaway database so they can't leak state through that shared
    connection — not just tests that ask for `checkpoint_db` directly:
    `/api/board` and `/api/heartbeat` open this same connection on every call
    (to report whether the sweeper looks alive), so any test hitting either
    endpoint needs its own isolated database, whether or not it creates a case.
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
    cases are written) and `runner.DSN` (which the board reads to list cases).
    They must be the same database, or the board would be reading a different
    store than the one the test just wrote to.
    """
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    dsn = _throwaway_db()
    try:
        with _wired_graph_db(monkeypatch, dsn):
            yield dsn
    finally:
        _drop_db(dsn)
