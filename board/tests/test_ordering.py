"""Queue ordering — the invariants from SPECIFICATION.md § Queue ordering rule."""

from __future__ import annotations

from app.views import card_from_state
from board.ordering import positions, sort_cards

from .conftest import make_state

EARLY = "2026-01-01T08:00:00+00:00"
LATE = "2026-01-01T18:00:00+00:00"


def cards(*states):
    return [card_from_state(s) for s in states]


def test_emergent_never_behind_queued():
    """G(¬(queued.order_key < emergent.order_key)) — stated as an invariant, so
    it is asserted over the ordering the board actually renders.
    """
    board = sort_cards(cards(
        make_state("early-queued", 4, EARLY),
        make_state("late-emergent", 1, LATE),
    ))

    assert [c.case_id for c in board] == ["late-emergent", "early-queued"]

    emergent = [c for c in board if c.bucket == "emergent"]
    queued = [c for c in board if c.bucket == "queued"]
    assert all(e.order_key < q.order_key for e in emergent for q in queued)


def test_arrival_breaks_ties_inside_a_bucket():
    board = sort_cards(cards(
        make_state("second", 3, LATE),
        make_state("first", 5, EARLY),
    ))
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
    place = positions(cards(
        make_state("c", 5, LATE),
        make_state("a", 1, EARLY),
        make_state("b", 3, EARLY),
    ))
    assert place == {"a": 1, "b": 2, "c": 3}
