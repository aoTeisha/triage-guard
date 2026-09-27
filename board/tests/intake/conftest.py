"""Fixtures for case creation and the pauses answered from the intake side.

Every test runs its cases in a throwaway Postgres database, never the shared
`triage` one — `/submit` goes through the real runner, and a case left open
there for a real CRM patient would make the triage-app suite reject its own
clean demo case: a patient with an active, un-released case cannot have a
second one opened for them via intake.

Scoped to this directory rather than shared with the board tests next door: both
sets are autouse and both build throwaway databases, and running both over one
test would create two or three of them with `runner.graph` patched by one
fixture and `runner.DSN` by another.
"""

from __future__ import annotations

import pytest

from app.monitor import timers

from ..conftest import _drop_db, _throwaway_db, _wired_graph_db


@pytest.fixture(autouse=True)
def isolated_case_store(monkeypatch):
    """One throwaway database per test for the runner's graph, `runner.DSN` (the
    duplicate check and the case lock) and the timer store — the three places a
    case leaves a trace. Nothing here reaches the shared database.
    """
    from app import crm_client

    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    # A release writes the visit's clinical data back to the CRM; tests must not.
    monkeypatch.setattr(crm_client, "patch_patient", lambda *a, **k: "ok")

    dsn = _throwaway_db()
    monkeypatch.setenv("TRIAGE_CHECKPOINT_DB", dsn)
    timers.connection.cache_clear()
    try:
        with _wired_graph_db(monkeypatch, dsn):
            yield
    finally:
        timers.connection.cache_clear()
        _drop_db(dsn)
