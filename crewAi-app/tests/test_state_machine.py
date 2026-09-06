"""The two tests that matter most here are the forbidden-pair one and the
injection-vs-missing-fields priority one: they demonstrate the invariant, not
the happy path.
"""

from app.state_machine import Event, State, advance_auto, classify_intake, transition

from tests.test_guards import CLEAN, FAILED, INJECTION, MISSING


def test_case_submitted_from_nothing_enters_intake_received():
    result = transition(None, Event.CASE_SUBMITTED, CLEAN)
    assert result.accepted
    assert result.next_state is State.INTAKE_RECEIVED
    assert "assign_order_key" in result.actions
    assert result.arrow == "1a"


def test_normalized_message_moves_to_parsing_on_arrow_2():
    result = transition(State.INTAKE_RECEIVED, Event.MESSAGE_NORMALIZED, CLEAN)
    assert result.accepted
    assert result.next_state is State.PARSING
    assert result.arrow == "2"


def test_forbidden_pair_is_denied_and_the_case_does_not_move():
    """The BLK row: the attempt is refused, the case stays put, and no new
    state is invented.
    """
    result = transition(State.INPUT_REJECTED, Event.DATA_PARSED, CLEAN)
    assert not result.accepted
    assert result.next_state is State.INPUT_REJECTED
    assert "forbidden transition" in result.explanation
    assert result.actions == ()


def test_denied_transition_never_returns_a_state_outside_the_enum():
    for state in State:
        result = transition(state, Event.CASE_SUBMITTED, CLEAN)
        if not result.accepted:
            assert result.next_state is state


def test_clean_payload_reaches_data_parsed():
    result = transition(State.PARSING, Event.DATA_PARSED, CLEAN)
    assert result.accepted
    assert result.next_state is State.DATA_PARSED


def test_missing_payload_reaches_missing_fields_requested():
    result = transition(State.PARSING, Event.MISSING_FIELDS_DETECTED, MISSING)
    assert result.accepted
    assert result.next_state is State.MISSING_FIELDS_REQUESTED
    assert "nurse_proposed_acuity" in result.explanation


def test_injection_beats_missing_fields():
    """A payload that is BOTH incomplete and hostile must be rejected, not
    sent back to the nurse for completion — inviting a resubmission of
    malicious input is never correct.
    """
    hostile_and_incomplete = {
        k: v for k, v in INJECTION.items() if k != "nurse_proposed_acuity"
    }
    result = transition(State.PARSING, Event.MISSING_FIELDS_DETECTED, hostile_and_incomplete)
    assert result.accepted
    assert result.next_state is State.INPUT_REJECTED
    assert result.next_state is not State.MISSING_FIELDS_REQUESTED
    assert result.explanation.startswith("injection")


def test_guard_failure_keeps_the_case_where_it_was():
    result = transition(State.PARSING, Event.DATA_PARSED, MISSING)
    assert not result.accepted
    assert result.next_state is State.PARSING


def test_fields_submitted_returns_to_parsing():
    result = transition(State.MISSING_FIELDS_REQUESTED, Event.FIELDS_SUBMITTED, CLEAN)
    assert result.accepted
    assert result.next_state is State.PARSING
    assert result.arrow == "1b.x"


def test_auto_advance_from_submission_failed_goes_to_intake_received():
    result = advance_auto(State.SUBMISSION_FAILED)
    assert result.accepted
    assert result.next_state is State.INTAKE_RECEIVED
    assert result.arrow == "1a·resubmit"


def test_auto_advance_is_denied_from_a_state_that_has_none():
    result = advance_auto(State.PARSING)
    assert not result.accepted
    assert result.next_state is State.PARSING


def test_classify_intake_reproduces_all_four_demo_outcomes():
    assert classify_intake(CLEAN) is Event.DATA_PARSED
    assert classify_intake(MISSING) is Event.MISSING_FIELDS_DETECTED
    assert classify_intake(FAILED) is Event.SUBMISSION_FAILED
    assert classify_intake(INJECTION) is Event.INVALID_INPUT_DETECTED
