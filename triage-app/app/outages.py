"""The outage switch: take one component offline for every patient, from the
board, until it is switched back. A demo of how the system keeps working without
it, not a production control.

The flags live in Postgres (`component_outages`), so the board and the sweeper,
two separate processes, see the same outage. Each component checks its flag at the
lowest call into it, and a flagged component raises the same kind of error a real
outage would, so the system's own error handling reacts exactly as it would then.

Nothing here reads the database unless `DEMO_OUTAGES=1` is set: with it unset,
every component is up and `check` costs one environment lookup.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from app.monitor import timers

# The board and the sweeper each import this, and neither loads `.env` earlier.
load_dotenv()

# "monitor" is the sweeper: it has no call to hook, so it skips its own tick.
COMPONENTS = ("llm", "opa", "prolog", "datalog", "monitor")


def enabled() -> bool:
    return os.environ.get("DEMO_OUTAGES") == "1"


def _down() -> frozenset[str]:
    rows = timers.connection().execute("SELECT component FROM component_outages").fetchall()
    return frozenset(r[0] for r in rows)


def is_down(component: str) -> bool:
    return enabled() and component in _down()


def check(component: str) -> None:
    """Raise if `component` is switched off. Called at the lowest call into it."""
    if is_down(component):
        raise RuntimeError(f"simulated outage: {component} unavailable (outage switch)")


def down_list() -> list[str]:
    """Switched-off components, in `COMPONENTS` order."""
    if not enabled():
        return []
    down = _down()
    return [c for c in COMPONENTS if c in down]


def set_down(component: str, down: bool) -> None:
    if down:
        timers.connection().execute("INSERT INTO component_outages (component) VALUES (%s) "
                                    "ON CONFLICT (component) DO NOTHING", (component,))
    else:
        timers.connection().execute("DELETE FROM component_outages WHERE component = %s", (component,))
