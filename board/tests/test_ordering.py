"""Queue ordering — the invariants from SPECIFICATION.md § Queue ordering rule."""

from __future__ import annotations

from app.views import card_from_state
from board.ordering import positions, sort_cards

from .conftest import make_state

EARLY = "2026-01-01T08:00:00+00:00"
LATE = "2026-01-01T18:00:00+00:00"


def cards(*states):
    return [card_from_state(s) for s in states]


def test_more_acute_never_behind_less_acute():
    """No patient is ordered ahead of a more acute one (SPECIFICATION.md:47),
    asserted over the ordering the board actually renders.
    """
    board = sort_cards(
        cards(
            make_state("early-esi2", 2, EARLY),
            make_state("late-esi1", 1, LATE),
            make_state("early-esi4", 4, EARLY),
        )
    )

    assert [c.case_id for c in board] == ["late-esi1", "early-esi2", "early-esi4"]
    assert all(a.acuity <= b.acuity for a, b in zip(board, board[1:]))


def test_arrival_breaks_ties_for_same_acuity():
    board = sort_cards(
        cards(
            make_state("second", 3, LATE),
            make_state("first", 3, EARLY),
        )
    )
    assert [c.case_id for c in board] == ["first", "second"]


def test_unkeyed_cards_sort_last():
    """A case parked at the gate before acuity settled has no order_key. It goes
    to the bottom rather than being handed a position it has not earned.
    """
    unsettled = make_state("no-acuity", 3, EARLY)
    unsettled["order_key"] = None
    unsettled["acuity"] = None

    board = sort_cards(cards(unsettled, make_state("settled", 5, LATE)))
    assert [c.case_id for c in board] == ["settled", "no-acuity"]


def test_positions_are_one_based_over_the_global_ordering():
    place = positions(
        cards(
            make_state("c", 5, LATE),
            make_state("a", 1, EARLY),
            make_state("b", 3, EARLY),
        )
    )
    assert place == {"a": 1, "b": 2, "c": 3}
