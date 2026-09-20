"""The waiting-room monitor: fires reassessment timers and reminds staff about
unanswered approval requests.

    timers.py    store: schema, schedule, claim/lease, heartbeat
    fire.py      state machine: dispatch, reconcile, notify
    sweeper.py   worker loop (`uv run sweeper`) that drives the two above

Graph nodes only *create* timer rows via `timers.schedule`. The sweeper fires,
retries, and reconciles them; the graph never sees that work.
"""

from __future__ import annotations
