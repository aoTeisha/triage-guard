"""Behavioral orchestration (BPpy) for the fire state machine: which event
one claimed timer gets.

The `proposer` asks for the naive thing — dispatch a reassessment, send a
reminder. Every safety rule is its own b-thread that, when its condition
holds, blocks that naive event and requests the safe one instead. The
arbiter picks a requested, unblocked event with the highest priority;
`one_decision` then blocks everything, so a run yields exactly one event.

Requirements don't check the decision after the fact here — they *are* the
decision. The same facts also go to Prolog (`app.symbolic.prolog`), and
`fire.handle` refuses to act if the two layers disagree.
"""

from __future__ import annotations

from typing import Any

import bppy as bp

DISPATCH = bp.BEvent("DISPATCH")
RECONCILE = bp.BEvent("RECONCILE")
NOTIFY = bp.BEvent("NOTIFY")
CANCEL = bp.BEvent("CANCEL")
FAIL_BUDGET = bp.BEvent("FAIL_BUDGET")
# The CRM write-back (I17): a released case's visit data, retried until the
# CRM takes it. Touches no case state, so no pause and no acknowledgment.
WRITEBACK = bp.BEvent("WRITEBACK")

# Every notify-only kind. Mirrors `fire._REMINDER_PAUSES`.
REMINDER_KINDS = {"gate_reminder", "reassessment_reminder", "senior_reminder"}
# A fire whose acknowledgment was lost (or whose worker died mid-call).
UNACKNOWLEDGED = {"UNKNOWN", "DISPATCHING"}


def _proposed(ctx: dict[str, Any]) -> bp.BEvent:
    if ctx["kind"] == "reassessment":
        return DISPATCH
    if ctx["kind"] in REMINDER_KINDS:
        return NOTIFY
    if ctx["kind"] == "crm_writeback":
        return WRITEBACK
    return CANCEL  # unrecognized kind: no pause to check, never delivered blind


@bp.thread
def proposer(ctx):
    yield bp.sync(request=_proposed(ctx))


@bp.thread
def no_blind_redispatch(ctx):
    """UNKNOWN never jumps straight back to DISPATCHING: reconcile first."""
    if ctx["kind"] == "reassessment" and ctx["fire_state"] in UNACKNOWLEDGED:
        yield bp.sync(request=RECONCILE, block=DISPATCH, priority=1)
        while True:
            yield bp.sync(block=DISPATCH)


@bp.thread
def stale_reminder(ctx):
    """A reminder about a pause that's already resolved is cancelled, not sent.
    Highest priority: stale beats over-budget, since sending is off the table either way."""
    if ctx["kind"] in REMINDER_KINDS and not ctx["pause_active"]:
        yield bp.sync(request=CANCEL, block=NOTIFY, priority=2)
        while True:
            yield bp.sync(block=NOTIFY)


@bp.thread
def notify_budget(ctx):
    """Hitting the per-recipient cap is a recorded FAILED, never a silent drop."""
    if ctx["kind"] in REMINDER_KINDS and ctx["notify_count"] >= ctx["notify_budget"]:
        yield bp.sync(request=FAIL_BUDGET, block=NOTIFY, priority=1)
        while True:
            yield bp.sync(block=NOTIFY)


@bp.thread
def one_decision(ctx):
    """After the first event, block everything: one claimed timer, one decision."""
    yield bp.sync(waitFor=bp.All())
    while True:
        yield bp.sync(block=bp.All())


class _Selected(bp.BProgramRunnerListener):
    """Collects selected events. bppy's listener base is abstract on every
    hook, so the no-ops below are required, not decorative."""

    def __init__(self):
        self.events: list[str] = []

    def event_selected(self, b_program, event):
        self.events.append(event.name)

    def starting(self, *a, **k): pass
    def started(self, *a, **k): pass
    def super_step_done(self, *a, **k): pass
    def ended(self, *a, **k): pass
    def assertion_failed(self, *a, **k): pass
    def b_thread_added(self, *a, **k): pass
    def b_thread_removed(self, *a, **k): pass
    def b_thread_done(self, *a, **k): pass
    def halted(self, *a, **k): pass


def select_action(ctx: dict[str, Any]) -> tuple[str, str]:
    """`(selected, proposed)` for one timer. Equal means no rule intervened;
    different means `selected` is what a guard substituted for `proposed`.
    """
    listener = _Selected()
    bp.BProgram(
        bthreads=[proposer(ctx), no_blind_redispatch(ctx), stale_reminder(ctx),
                  notify_budget(ctx), one_decision(ctx)],
        event_selection_strategy=bp.PriorityBasedEventSelectionStrategy(default_priority=0),
        listener=listener,
    ).run()
    assert len(listener.events) == 1, listener.events  # one_decision guarantees this
    return listener.events[0], _proposed(ctx).name
