"""Driving the graph: start a case, inspect a pause, resume it.

One module so the CLI and the intake UI use the *same* code path. A gate that
behaves differently under the demo runner than under the browser would not be a
gate worth testing.

`thread_id` is the `case_id`: one case is one conversation with the graph, and
resuming means re-entering that thread.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from langgraph.types import Command

from app.actors import human_bridge
from app.graph import TriageState, build_graph
from app.observability import langfuse_callbacks

# Local checkpoint store. Gitignored; a real deployment swaps this for Postgres
# without touching anything else in this file.
DB_PATH = Path(
    os.environ.get("TRIAGE_CHECKPOINT_DB", Path(__file__).resolve().parent.parent / ".triage_state.db")
)


@lru_cache(maxsize=1)
def graph():
    """The compiled graph, with persistence. Cached — compiling is not free and
    the topology never changes at runtime.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return build_graph(checkpointer=SqliteSaver(conn))


def config_for(case_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": case_id},
        "callbacks": langfuse_callbacks(),
    }


def hydrate(result: Any) -> dict[str, Any]:
    """Fill in fields no node happened to write.

    LangGraph returns only the channels a run actually touched, so a case that
    stopped at `missing_fields_requested` comes back with no `safety_passed` key
    at all — and a caller reading `.get("safety_passed")` would see None where the
    declared default is False. Round-tripping through the model restores every
    default and validates what was written.
    """
    values = {k: v for k, v in result.items() if not k.startswith("__")}
    return TriageState.model_validate(values).model_dump()


def _pending(result: dict[str, Any]) -> dict[str, Any] | None:
    """The interrupt payload if the run paused, else None."""
    interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if not interrupts:
        return None
    return dict(interrupts[0].value)


def start_case(
    case: dict[str, Any], thread_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Kick off a case. Returns (state, pending_gate_or_None).

    `thread_id` defaults to the case_id, which is what a real intake wants: one
    case is one thread, and re-submitting the same case_id continues that case
    rather than starting a parallel one. Demo runs pass an explicit unique id so
    repeated runs of the same fixture do not accumulate onto one another's
    checkpointed audit trail.
    """
    result = graph().invoke(
        {
            "case_id": case["case_id"],
            "raw_payload": dict(case),
            "nurse_proposed_acuity": case.get("nurse_proposed_acuity"),
        },
        config_for(thread_id or case["case_id"]),
    )
    return hydrate(result), _pending(result)


def resume_case(
    case_id: str, decision: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Answer a gate. `decision` needs at least `decision` and `resolver_role`."""
    result = graph().invoke(Command(resume=decision), config_for(case_id))
    return hydrate(result), _pending(result)


def snapshot(case_id: str) -> dict[str, Any]:
    """Current persisted state for a case, for a board or a status endpoint."""
    return graph().get_state(config_for(case_id)).values


def history(case_id: str) -> list[dict[str, Any]]:
    """Every checkpoint for a case, oldest first — the full state-transition trail,
    free from the checkpointer already in use.
    """
    snapshots = list(graph().get_state_history(config_for(case_id)))
    return [s.values for s in reversed(snapshots)]


def run_to_completion(
    case: dict[str, Any], thread_id: str | None = None, max_gates: int = 5
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run a case unattended, answering each gate from the mock resolver.

    The gate is NOT bypassed: the graph really interrupts and really checkpoints,
    and the canned reply is fed back through `resume_case` exactly as a charge
    nurse's would be. `max_gates` bounds the correction loop so a misconfigured
    mock cannot spin forever.
    """
    thread = thread_id or case["case_id"]
    state, pending = start_case(case, thread_id=thread)
    gates: list[dict[str, Any]] = []

    while pending and len(gates) < max_gates:
        gates.append(pending)
        reply = human_bridge.mock_resume_for(pending["gate"])
        state, pending = resume_case(thread, reply)

    return state, gates
