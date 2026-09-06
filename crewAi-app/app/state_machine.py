"""The intake slice of the control plane (docs/SPECIFICATION.md § Transitions).

Six states, seven events, nine transitions. Scope stops at the intake branches
on purpose — everything past `data_parsed` needs agents and formal layers that
do not exist yet.

transition() decides; it does not act. Actions come back by name for the
caller to run, which keeps the entire table testable with no Langfuse client
and no notify_user stub.

There is no State.BLOCKED. A denied transition returns accepted=False with
next_state set to the state the case was already in — the BLK row in the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app import guards

# Every guard has the same shape, so the table can hold any of them.
Guard = Callable[[dict], tuple[bool, str]]


class State(str, Enum):
    """Control-plane states reachable in the intake slice."""

    INTAKE_RECEIVED = "intake_received"
    PARSING = "parsing"
    DATA_PARSED = "data_parsed"
    MISSING_FIELDS_REQUESTED = "missing_fields_requested"
    SUBMISSION_FAILED = "submission_failed"
    INPUT_REJECTED = "input_rejected"


class Event(str, Enum):
    """Events that move the intake slice."""

    CASE_SUBMITTED = "CASE_SUBMITTED"
    MESSAGE_NORMALIZED = "MESSAGE_NORMALIZED"
    DATA_PARSED = "DATA_PARSED"
    MISSING_FIELDS_DETECTED = "MISSING_FIELDS_DETECTED"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    INVALID_INPUT_DETECTED = "INVALID_INPUT_DETECTED"
    FIELDS_SUBMITTED = "FIELDS_SUBMITTED"


@dataclass(frozen=True)
class TransitionRule:
    """One row of the Transitions table."""

    next_state: State
    explanation: str
    arrow: str
    guard: Guard | None = None
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitionResult:
    """What transition() decided. `accepted=False` is the BLK row: the case
    stays where it was and the refusal is explained, never a new state.
    """

    accepted: bool
    next_state: State | None
    explanation: str
    arrow: str = ""
    actions: tuple[str, ...] = ()


TRANSITIONS: dict[tuple[State | None, Event], TransitionRule] = {
    (None, Event.CASE_SUBMITTED): TransitionRule(
        next_state=State.INTAKE_RECEIVED,
        explanation="new case entered",
        arrow="1a",
        actions=("route_channel", "assign_order_key"),
    ),
    (State.INTAKE_RECEIVED, Event.MESSAGE_NORMALIZED): TransitionRule(
        next_state=State.PARSING,
        explanation="input normalized",
        arrow="2",
        actions=("emit_event_log",),
    ),
    (State.PARSING, Event.DATA_PARSED): TransitionRule(
        next_state=State.DATA_PARSED,
        explanation="submission valid",
        arrow="4",
        guard=guards.required_fields_complete_and_valid,
        actions=("emit_event_log",),
    ),
    (State.PARSING, Event.MISSING_FIELDS_DETECTED): TransitionRule(
        next_state=State.MISSING_FIELDS_REQUESTED,
        explanation="required fields missing",
        arrow="16",
        guard=guards.not_required_fields_complete,
        actions=("notify_user:request fields",),
    ),
    (State.PARSING, Event.SUBMISSION_FAILED): TransitionRule(
        next_state=State.SUBMISSION_FAILED,
        explanation="submission unusable",
        arrow="17",
        actions=("notify_user:resubmit or manual",),
    ),
    (State.PARSING, Event.INVALID_INPUT_DETECTED): TransitionRule(
        next_state=State.INPUT_REJECTED,
        explanation="invalid schema or injection",
        arrow="18",
        guard=guards.not_input_is_valid,
        actions=("notify_user:invalid input",),
    ),
    (State.MISSING_FIELDS_REQUESTED, Event.FIELDS_SUBMITTED): TransitionRule(
        next_state=State.PARSING,
        explanation="nurse supplied fields",
        arrow="1b.x",
    ),
}

# The two event-less recovery edges: both error states return the case to the
# front door, they just tell the nurse a different thing on the way.
AUTO_ADVANCE: dict[State, TransitionRule] = {
    State.SUBMISSION_FAILED: TransitionRule(
        next_state=State.INTAKE_RECEIVED,
        explanation="resubmit",
        arrow="1a·resubmit",
        actions=("notify_user:resubmit",),
    ),
    State.INPUT_REJECTED: TransitionRule(
        next_state=State.INTAKE_RECEIVED,
        explanation="rejected, await new input",
        arrow="1a·rejected",
        actions=("notify_user:invalid input",),
    ),
}


def classify_intake(payload: dict) -> Event:
    """Which of the four intake outcomes this payload produces.

    The deterministic stand-in for the Intake Parser's ParseResult.outcome.
    The payload decides, never the caller — that is what makes the guards load
    bearing instead of decorative.

    Order is the priority order: injection, then nothing-usable, then gaps.
    """
    if guards.not_input_is_valid(payload)[0]:
        return Event.INVALID_INPUT_DETECTED
    if guards.nothing_usable(payload):
        return Event.SUBMISSION_FAILED
    if guards.missing_fields(payload):
        return Event.MISSING_FIELDS_DETECTED
    return Event.DATA_PARSED


def transition(current: State | None, event: Event, payload: dict) -> TransitionResult:
    """Decide one transition. Never raises; a bad pair is a denial, not an error."""
    # A hostile payload outranks whatever event the caller claimed: a case that
    # is both incomplete and injected is rejected, never sent back to be
    # completed and resubmitted.
    if current is State.PARSING and event is not Event.INVALID_INPUT_DETECTED:
        if guards.not_input_is_valid(payload)[0]:
            event = Event.INVALID_INPUT_DETECTED

    rule = TRANSITIONS.get((current, event))
    if rule is None:
        return TransitionResult(
            accepted=False,
            next_state=current,
            explanation=f"forbidden transition: no rule for {current} + {event.value}",
        )

    passed, why = rule.guard(payload) if rule.guard else (True, rule.explanation)
    if not passed:
        return TransitionResult(False, current, why)
    return TransitionResult(True, rule.next_state, why, rule.arrow, rule.actions)


def advance_auto(current: State) -> TransitionResult:
    """The two event-less recovery edges. Denied from any other state."""
    rule = AUTO_ADVANCE.get(current)
    if rule is None:
        return TransitionResult(
            accepted=False,
            next_state=current,
            explanation=f"forbidden transition: no auto-advance from {current}",
        )
    return TransitionResult(
        accepted=True,
        next_state=rule.next_state,
        explanation=rule.explanation,
        arrow=rule.arrow,
        actions=rule.actions,
    )
