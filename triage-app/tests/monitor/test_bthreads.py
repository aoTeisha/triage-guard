"""The BPpy layer (`app.monitor.bthreads`): for one claimed timer, which
event the b-threads let through. Pure Python, no database, no graph.
"""

from __future__ import annotations

import pytest

from app.monitor import bthreads


def _ctx(**over):
    base = {"kind": "reassessment", "fire_state": "DUE", "pause_active": True,
            "notify_count": 0, "notify_budget": 5}
    return base | over


@pytest.mark.parametrize("ctx, selected, proposed", [
    (_ctx(), "DISPATCH", "DISPATCH"),
    (_ctx(fire_state="FAILED"), "DISPATCH", "DISPATCH"),
    (_ctx(fire_state="UNKNOWN"), "RECONCILE", "DISPATCH"),
    (_ctx(fire_state="DISPATCHING"), "RECONCILE", "DISPATCH"),
    (_ctx(kind="gate_reminder"), "NOTIFY", "NOTIFY"),
    (_ctx(kind="reassessment_reminder"), "NOTIFY", "NOTIFY"),
    (_ctx(kind="senior_reminder"), "NOTIFY", "NOTIFY"),
    (_ctx(kind="gate_reminder", pause_active=False), "CANCEL", "NOTIFY"),
    (_ctx(kind="senior_reminder", notify_count=5), "FAIL_BUDGET", "NOTIFY"),
    # A stale reminder is pointless whether or not the budget is spent: CANCEL wins.
    (_ctx(kind="gate_reminder", pause_active=False, notify_count=5), "CANCEL", "NOTIFY"),
    (_ctx(kind="safety_park"), "CANCEL", "CANCEL"),
])
def test_exactly_one_event_and_it_is_deterministic(ctx, selected, proposed):
    for _ in range(10):  # the arbiter is random among equal priorities; ours must never tie
        assert bthreads.select_action(ctx) == (selected, proposed)


def test_unknown_never_jumps_straight_back_to_dispatching():
    """The rule that matters most (app/monitor/README.md), and I16's teeth."""
    for state in ("UNKNOWN", "DISPATCHING"):
        assert bthreads.select_action(_ctx(fire_state=state))[0] != "DISPATCH"
