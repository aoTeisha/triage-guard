"""Work around a LangGraph bug that can drop a pause after a node crash.

LangGraph's `PregelRunner` remembers the errors it has routed to a node's
`error_handler` by `id(exception)`, in `_handled_exception_ids`, a set that lives
as long as the run. Before it raises a node's `GraphInterrupt` it checks that the
interrupt's id is *not* in that set. Once a handled exception is garbage
collected, Python may give its id to a new object — and when that object is the
`GraphInterrupt` of a later pause in the same run, LangGraph takes the interrupt
for an error it already handled and swallows it. The run then ends with the case
paused nowhere: no gate, no waiting room.

In this graph that is a safety-validator, classifier or CRM crash (each has an
error handler) followed by any pause in the same `invoke`. Seen as a flake in
`tests/test_component_down.py`, about one run in ten, and possible in production.
Present in langgraph 1.2.11 and 1.2.12 (`langgraph/pregel/_runner.py`).

The fix keeps every exception the runner commits alive for as long as the runner
itself, so no id in its set can be reused while the set is consulted. The runner
calls `commit(task, exception)` for every failed task before it records the id.
Remove this module once LangGraph tracks handled exceptions by object, not by id.
"""

from __future__ import annotations

from typing import Any

from langgraph.pregel._runner import PregelRunner

_MARKER = "_keeps_failures_alive"


def keep_failed_exceptions_alive() -> None:
    """Patch `PregelRunner.commit` once per process. Safe to call again."""
    original = PregelRunner.commit
    if getattr(original, _MARKER, False):
        return

    def commit(self: PregelRunner, task: Any, exception: BaseException | None) -> None:
        if exception is not None:
            self.__dict__.setdefault("_failures_kept_alive", []).append(exception)
        return original(self, task, exception)

    setattr(commit, _MARKER, True)
    commit.__doc__ = original.__doc__
    PregelRunner.commit = commit  # type: ignore[method-assign]
