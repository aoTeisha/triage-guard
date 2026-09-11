"""Which cases are on the board, and in which column."""

from __future__ import annotations

from app.states import ClinicalStatus, State
from app.views import BOARD_COLUMNS, card_from_state

from .conftest import make_state

T0 = "2026-01-01T08:00:00+00:00"


def test_every_clinical_status_is_a_column_and_case_closed_is_not():
    assert BOARD_COLUMNS == [s.value for s in ClinicalStatus]
    assert State.CASE_CLOSED.value not in BOARD_COLUMNS
    assert len(set(BOARD_COLUMNS)) == len(BOARD_COLUMNS)


def test_each_status_maps_to_exactly_one_column():
    for status in ClinicalStatus:
        card = card_from_state(make_state("c", 3, T0, status=status))
        assert [card.status] == [c for c in BOARD_COLUMNS if c == card.status]


def test_a_closed_case_leaves_the_board():
    closed = make_state("c", 3, T0)
    closed["clinical_status"] = State.CASE_CLOSED.value
    assert card_from_state(closed) is None


def test_a_case_that_never_reached_the_world_plane_is_not_a_card():
    """Refused, failed, or still in intake — no clinical_status, no card."""
    refused = make_state("c", 3, T0)
    refused["clinical_status"] = None
    refused["control_state"] = State.ACTION_DENIED.value
    assert card_from_state(refused) is None


def test_a_released_card_is_still_returned():
    """AMA consequence: an unsigned-but-departed case is still open, so the card
    must remain reachable rather than being filtered out with the closed ones.
    """
    released = make_state("c", 3, T0, status=ClinicalStatus.PATIENT_RELEASED)
    card = card_from_state(released)
    assert card is not None
    assert card.status == ClinicalStatus.PATIENT_RELEASED.value


def test_gate_pending_is_derived_from_the_control_state():
    at_gate = make_state("c", 3, T0, status=ClinicalStatus.HUMAN_REVIEW)
    at_gate["control_state"] = State.AWAITING_HUMAN_APPROVAL.value
    assert card_from_state(at_gate).gate_pending is True
    assert card_from_state(make_state("d", 3, T0)).gate_pending is False
