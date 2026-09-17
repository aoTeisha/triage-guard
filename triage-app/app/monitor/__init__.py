"""The waiting-room monitor: the background process that fires reassessment
timers for waiting patients and reminds staff about unanswered approval
requests.

Three modules, one job split by layer:
    timers.py    the shared store: schema, scheduling, claim/lease, heartbeat
    fire.py      the fire state machine: dispatch, ack, reconcile, notify
    sweeper.py   the worker loop (`uv run sweeper`) that drives the two above

Graph boundary: When a case reaches monitoring states (awaiting_reassessment,
awaiting_human_approval), those nodes create timer records via `timers`. That's
all — they never execute or retry them. The background sweeper process reads
those timers, fires them, handles retries and reconciliation. The graph can't
see any of that work.
"""

from __future__ import annotations
