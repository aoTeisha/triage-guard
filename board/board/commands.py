"""The one place a staff action re-enters a paused case.

Every write that resumes a case goes through here: it takes the case lock,
validates the pause, invokes the graph with `Command(resume=...)` into the
case's own thread, and records the outcome on a trace span. Centralizing that
sequence in one function is what stops two call sites from drifting into two
slightly different versions of it.

What it deliberately does *not* do is decide what an endpoint replies. The two
halves of the UI answer differently today — the board's buttons get a status
word, the intake forms get a case view — and the branch in the page that reads a
refusal out of an HTTP 200 is the thing standing between a wrong-role click and a
false "released" confirmation. So this returns an `Outcome` and each endpoint
formats its own body, byte-for-byte as before. Unifying those two shapes is a
later change with its own review; `Outcome` already carries both facts for
whoever takes it.

Pauses in this graph report themselves two different ways and the caller says
which to expect, because guessing wrong drops patients. A bare `interrupt()`
appears only as a pending task on the checkpoint and leaves `control_state`
holding whatever the previous node set, so `control_state` cannot be trusted
there. A pause built as a commit node followed by a pause node keeps
`control_state` equal to the pause's own name for exactly as long as the pause
lasts. `paused_at` tests the first kind, `in_control_state` the second, and
`open_pause` accepts any pause of a case that is not already closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException
from langgraph.types import Command

from app import runner
from app.labels import Transition
from app.observability import case_trace, record_outcome
from app.states import State
from app.views import case_view

# Raises HTTPException(409) if the snapshot is not the pause this answer belongs
# to. Takes a LangGraph StateSnapshot, which carries both `.values` (the
# committed state) and `.next` (the pending tasks).
Check = Callable[[Any], None]


@dataclass
class Outcome:
    """What happened, without saying how to report it.

    `accepted` is False when a guard inside the graph refused the action, which
    it records as a BLK audit row. That is a normal outcome, not an HTTP error:
    the request was valid and was processed, and the graph said no.
    """

    result: dict[str, Any]      # the raw return of graph.invoke
    accepted: bool
    refusal: str | None         # the guard's own explanation when refused

    def view(self) -> dict[str, Any]:
        """The hydrated case view — what the intake endpoints have always
        returned, built the same way `runner.resume_case` built it.

        A method rather than a field because the board's four endpoints reply
        with a status word and never read this. Making them hydrate would spend
        work they do not need and hand them a new way to fail, on a path whose
        behavior this change is supposed to leave alone.

        `runner.pending` reads `__interrupt__` off the raw result, so it must be
        called before `hydrate`, which strips every dunder key.
        """
        gate = runner.pending(self.result)
        return case_view(runner.hydrate(self.result), gate)


def paused_at(node: str, detail: str | None = None) -> Check:
    """The named node must be among the checkpoint's pending tasks.

    `detail` overrides the generic wording for a caller whose refusal text is
    part of the preserved contract — `/resume`'s "case is not awaiting human
    approval" is the one case, kept distinct from the generic template so its
    reply doesn't change.
    """

    def check(snapshot: Any) -> None:
        if node not in [getattr(n, "value", n) for n in snapshot.next]:
            raise HTTPException(status_code=409, detail=detail or f"case is not paused at {node}")

    return check


def in_control_state(state: str, detail: str) -> Check:
    """`control_state` must equal `state`. `detail` is the refusal a nurse reads,
    so each pause supplies its own wording rather than sharing a generic one.
    Those strings are part of the preserved contract — they are already the
    `detail` of a 409 the page renders.
    """

    def check(snapshot: Any) -> None:
        if snapshot.values.get("control_state") != state:
            raise HTTPException(status_code=409, detail=detail)

    return check


def in_control_states(states: frozenset[str], detail: str) -> Check:
    """`in_control_state` for an answer more than one pause accepts."""

    def check(snapshot: Any) -> None:
        if snapshot.values.get("control_state") not in states:
            raise HTTPException(status_code=409, detail=detail)

    return check


def open_pause() -> Check:
    """Any pause of a case that is not already closed. A release is valid from
    any such pause, not just one named pause.
    """

    def check(snapshot: Any) -> None:
        if snapshot.values.get("control_state") == State.CASE_CLOSED.value or not snapshot.next:
            raise HTTPException(status_code=409, detail="case is closed or not paused")

    return check


def answer_pause(
    case_id: str,
    resume: dict[str, Any],
    check: Check,
    *,
    require_applied: bool,
) -> Outcome:
    """Re-enter `case_id`'s paused run with `Command(resume=resume)`.

    I22: the pause-validity snapshot is taken *after* acquiring the case lock,
    never before. Two near-simultaneous requests for one case — a double-click,
    or two nurses — would otherwise both pass `check` while the case still looked
    paused to both, then race into `invoke`. The loser's resume can land on a
    thread the winner already closed, and LangGraph's `Command(resume=...)` on an
    ended thread with no pending task just returns the current fully-updated
    state rather than erroring, so the loser would see the winner's audit-log
    growth and report the winner's outcome as its own. Locking first and
    snapshotting second means a second request only ever sees the truth: either
    the pause is genuinely still open, or `check` refuses it with a 409 before
    `invoke` is ever called.

    `require_applied` confirms the audit log actually grew, and 409s if it did
    not. It is True for four endpoints and False for the other four, so each
    endpoint's caller sees exactly the refusal behavior its contract documents
    rather than a new one grafted on. Turning it on everywhere looks right and
    is the obvious follow-up, with its own testing.

    The outcome is read from `invoke`'s own return value, not from a second
    `get_state` afterwards — a second read could pick up whichever concurrent
    request's audit row landed last.
    """
    with runner.case_lock(case_id):
        graph = runner.graph()
        config = runner.config_for(case_id)
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise HTTPException(status_code=404, detail=f"no case {case_id}")
        check(snapshot)
        before = len(snapshot.values.get("audit_log") or [])
        with case_trace(case_id, "case-resume", snapshot.values) as span:
            result = graph.invoke(Command(resume=resume), config)
            record_outcome(span, snapshot.values, result)

    log = result.get("audit_log") or []
    if require_applied and len(log) <= before:
        raise HTTPException(status_code=409, detail="case already left the pause")

    refused = bool(log) and log[-1].get("transition") == Transition.BLK.value
    return Outcome(
        result=result,
        accepted=not refused,
        refusal=log[-1].get("explanation") if refused else None,
    )
