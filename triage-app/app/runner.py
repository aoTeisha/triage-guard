"""Driving the graph: start a case, inspect a pause, resume it.

One module so the CLI and the intake UI use the *same* code path. A gate that
behaves differently under the demo runner than under the browser would not be a
gate worth testing.

`thread_id` is the `case_id`: one case is one conversation with the graph, and
resuming means re-entering that thread.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from typing import Any

import psycopg
from langgraph.types import Command

from app.actors import human_bridge
from app.graph import TriageState, build_graph
from app.observability import langfuse_callbacks
from app.states import State

# Shared Postgres checkpoint store — same DSN the timers module writes to
# (see app/monitor/timers.py), so a timer row and the case state it refers
# to can be written in one transaction.
DSN = os.environ.get(
    "TRIAGE_CHECKPOINT_DB", "postgresql://triage:triage@localhost:5434/triage"
)


_saver_cm = None  # kept alive for the process's lifetime; see `graph()` below.


class CaseClosedError(RuntimeError):
    """Raised by `start_case` when `thread_id` already belongs to a closed
    case (I20) — resubmitting that case_id would re-enter its LangGraph
    thread from START and mutate fields that are supposed to be final.
    """


class CaseLockTimeout(RuntimeError):
    """Raised by `case_lock` when another writer still holds the case's lock
    past `timeout_seconds` (I22) — refuse rather than let two writers
    interleave against the same LangGraph thread.
    """


@contextmanager
def case_lock(case_id: str, timeout_seconds: float = 5.0):
    """Postgres advisory lock keyed by `case_id` (I22): serializes concurrent
    staff actions against the same case across every process that can write
    one — `intake-channel` (via `start_case`/`resume_case`) and `board` (via
    its own resume endpoints).

    `DSN` is read here, not captured at import — tests monkeypatch
    `runner.DSN` directly (see `tests/conftest.py`'s `checkpoint_db`
    fixture), so this must look it up at call time, not close over the
    module-import-time value.

    Uses `pg_try_advisory_lock` polled in a loop, not the blocking
    `pg_advisory_lock`: a stuck holder must time out into a clear refusal,
    not hang the caller indefinitely.

    # ponytail: 50ms poll, busy-loop. Fine for a ward's request volume;
    # LISTEN/NOTIFY or a condition variable if contention ever shows up in
    # a profile.
    """
    conn = psycopg.connect(DSN, autocommit=True)
    key = f"case_lock:{case_id}"
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while True:
            acquired = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))", (key,)
            ).fetchone()[0]
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise CaseLockTimeout(f"case {case_id} is locked by another writer")
            time.sleep(0.05)
        yield
    finally:
        if acquired:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        conn.close()


@lru_cache(maxsize=1)
def graph():
    """The compiled graph, with persistence. Cached — compiling is not free and
    the topology never changes at runtime.
    """
    global _saver_cm
    from langgraph.checkpoint.postgres import PostgresSaver

    # `from_conn_string` is a context manager backed by a generator; entering
    # it without keeping a reference lets Python garbage-collect the
    # generator once this function returns, which closes the connection
    # under it. Stashing it at module level keeps it alive for as long as
    # the cached graph is used.
    cm = PostgresSaver.from_conn_string(DSN)
    saver = cm.__enter__()
    try:
        saver.setup()
        compiled = build_graph(checkpointer=saver)
    except BaseException:
        # `lru_cache` doesn't cache a raising call, so a later retry would
        # enter a second context manager and orphan this one's connection.
        cm.__exit__(*sys.exc_info())
        raise
    _saver_cm = cm
    return compiled


def config_for(case_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": case_id},
        "callbacks": langfuse_callbacks(),
    }


def hydrate(result: Any) -> dict[str, Any]:
    """Fill in fields no node happened to write, using their declared
    defaults from `TriageState`.

    LangGraph only returns the fields a run actually touched, so a case that
    stopped early at `missing_fields_requested` comes back with no
    `safety_passed` key at all — a caller reading `.get("safety_passed")`
    would see `None`, even though the field's declared default is `False`.
    Round-tripping the result through the `TriageState` model restores every
    missing default, and validates what was actually written.
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

    Also takes a lock keyed by `stable_patient_id` (I19), held for the whole
    invoke — not just `resolving_identity`'s own duplicate-case check.
    `all_case_summaries` only sees a case once its first checkpoint has
    committed, so two `start_case` calls for the same patient, neither of
    which has written a checkpoint yet, would otherwise both pass the check
    before either becomes visible to the other. Serializing case *creation*
    per patient closes that window entirely: by the time the second call's
    own `resolving_identity` runs, the first call's whole run — including
    whatever it decided about a duplicate — has already committed.
    """
    thread = thread_id or case["case_id"]
    stable_patient_id = case.get("stable_patient_id")
    with ExitStack() as stack:
        if stable_patient_id:
            stack.enter_context(case_lock(f"patient:{stable_patient_id}"))
        stack.enter_context(case_lock(thread))
        existing = graph().get_state(config_for(thread)).values
        if existing.get("control_state") == State.CASE_CLOSED.value:
            raise CaseClosedError(f"case {thread} is closed; its fields cannot change")
        result = graph().invoke(
            {
                "case_id": case["case_id"],
                "raw_payload": dict(case),
                "nurse_proposed_acuity": case.get("nurse_proposed_acuity"),
            },
            config_for(thread),
        )
    return hydrate(result), _pending(result)


def resume_case(
    case_id: str, decision: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Answer a gate. `decision` needs at least `decision` and `resolver_role`."""
    with case_lock(case_id):
        result = graph().invoke(Command(resume=decision), config_for(case_id))
    return hydrate(result), _pending(result)


def snapshot(case_id: str) -> dict[str, Any]:
    """Current persisted state for a case, for a board or a status endpoint."""
    return graph().get_state(config_for(case_id)).values


def history(case_id: str) -> list[dict[str, Any]]:
    """Every checkpoint for a case, oldest first — the full history of state
    snapshots, read straight from the checkpointer LangGraph already uses to
    persist the run.
    """
    snapshots = list(graph().get_state_history(config_for(case_id)))
    return [s.values for s in reversed(snapshots)]


def all_case_ids() -> list[str]:
    """Every case_id (LangGraph thread_id) that has at least one checkpoint.

    Shared by `all_case_summaries` below and `board.repo.CheckpointRepo.case_ids`
    (a board refresh's own enumeration) — one query, not two copies of it.

    `DSN` is read per call, not captured at import: tests point it at a
    throwaway database (see `checkpoint_db`), and a captured DSN would
    ignore them. Read-only session: neither caller should be able to write
    the checkpoint store even by accident, and Postgres enforces that here
    rather than trusting every future edit to stay a SELECT.
    `OperationalError` as well as `UndefinedTable`: before the first case
    the checkpointer's tables don't exist, and a caller may also start
    before Postgres is up. Both mean "no cases yet," not an error.
    """
    try:
        with psycopg.connect(DSN, options="-c default_transaction_read_only=on") as conn:
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.OperationalError):
        return []
    return [r[0] for r in rows]


def all_case_summaries(exclude_case_id: str | None = None) -> list[dict[str, Any]]:
    """One row per case ever started, as `{case_id, control_state,
    stable_patient_id}`. Backs the I19 duplicate-active-case check.

    # ponytail: one graph.get_state() per case — same trade-off
    # board/repo.py's CheckpointRepo already makes for a board refresh. Fine
    # at current volume; past a few hundred cases, write a `cases` summary
    # row (case_id, control_state, stable_patient_id) alongside each
    # checkpoint and read that table instead of scanning every thread here.
    """
    summaries = []
    for case_id in all_case_ids():
        if case_id == exclude_case_id:
            continue
        values = graph().get_state(config_for(case_id)).values
        summaries.append({
            "case_id": case_id,
            "control_state": values.get("control_state"),
            "stable_patient_id": values.get("stable_patient_id"),
        })
    return summaries


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

    # The waiting-room pause (`awaiting_reassessment`) is not a gate — nobody
    # answers it directly, a reassessment timer firing does. So an unattended
    # run just stops here, the same way it used to simply reach the graph's
    # END before this pause node existed.
    while pending and "gate" in pending and len(gates) < max_gates:
        gates.append(pending)
        reply = human_bridge.mock_resume_for(pending["gate"])
        state, pending = resume_case(thread, reply)

    return state, gates
