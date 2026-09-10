"""Shared fixtures. Every test runs offline: no LLM, no CRM stub, no Langfuse."""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from app.graph import build_graph
from app.runner import hydrate


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Force mock mode and silence tracing for the whole suite."""
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)


@pytest.fixture
def graph(tmp_path):
    """A compiled graph with a throwaway checkpoint file.

    File-backed rather than in-memory because the gate tests need a checkpoint
    that survives a rebuilt graph object — that is the whole point of the pause.
    """
    conn = sqlite3.connect(str(tmp_path / "ckpt.db"), check_same_thread=False)
    yield build_graph(checkpointer=SqliteSaver(conn))
    conn.close()


@pytest.fixture
def run(graph):
    """Invoke the graph on a case and return (final_values, pending_gate)."""

    def _run(case: dict[str, Any], thread: str | None = None):
        thread = thread or f"t-{uuid4().hex[:8]}"
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


def arrows(state: dict[str, Any]) -> list[str]:
    """Arrow labels from a run's audit trail, in order."""
    return [r["arrow"] for r in state.get("audit_log", []) if r.get("arrow")]
