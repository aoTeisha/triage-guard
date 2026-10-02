"""The LangGraph workaround in `app/graph/_langgraph_fixes.py`.

The bug it covers needs Python to reuse a freed exception's id for a later
`GraphInterrupt`, which no test can arrange on demand; before the fix it cost
`tests/test_component_down.py` about one run in ten. What can be checked
exactly is the guarantee the fix gives: an exception the runner committed stays
alive as long as the runner does, so its id cannot be handed to a new object
while the runner still looks ids up.
"""

from __future__ import annotations

import gc
import weakref
from types import SimpleNamespace

from langgraph.pregel._runner import PregelRunner

import app.graph.build  # noqa: F401 — installs the fix, as building the graph does
from app.graph._langgraph_fixes import keep_failed_exceptions_alive


class Boom(Exception):
    pass


def _runner() -> PregelRunner:
    ignore = lambda *a, **k: None  # noqa: E731
    return PregelRunner(submit=lambda: ignore, put_writes=lambda: ignore,
                        node_error_handler_map={"crashes": "handler"})


def _failed_task() -> SimpleNamespace:
    return SimpleNamespace(id="t1", name="crashes", writes=[], config=None)


def test_building_the_graph_installs_the_fix_once():
    assert getattr(PregelRunner.commit, "_keeps_failures_alive", False)
    patched = PregelRunner.commit
    keep_failed_exceptions_alive()
    assert PregelRunner.commit is patched          # not wrapped a second time


def test_a_committed_exception_lives_as_long_as_its_runner():
    runner = _runner()
    task = _failed_task()
    error = Boom("validator down")
    alive = weakref.ref(error)

    runner.commit(task, error)
    del error, task
    gc.collect()
    assert alive() is not None                     # its id cannot be reused yet

    del runner
    gc.collect()
    assert alive() is None                         # and it goes with the runner
