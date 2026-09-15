"""The waiting-room monitor: the background process that fires reassessment
timers for waiting patients and reminds staff about unanswered approval
requests.

Three modules, one job split by layer:
    timers.py    the shared store: schema, scheduling, claim/lease, heartbeat
    fire.py      the fire state machine: dispatch, ack, reconcile, notify
    sweeper.py   the worker loop (`uv run sweeper`) that drives the two above

The graph nodes that pause and resume (`app.graph.nodes.terminal.monitoring` /
`awaiting_reassessment`, `app.graph.nodes.gate.awaiting_human_approval`) import
`timers` from here directly; they never import `fire` or `sweeper` — the monitor
proposes a moment into the graph, it does not decide what the graph does with it.
"""

from __future__ import annotations
