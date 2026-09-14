"""Position rules: a card keeps its place, and the clock never re-sorts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.states import ClinicalStatus
from app.views import card_from_state
from board.ordering import positions, sort_cards

from .conftest import make_state

T0 = "2026-01-01T08:00:00+00:00"
T1 = "2026-01-01T09:00:00+00:00"
T2 = "2026-01-01T10:00:00+00:00"


def cards(states, now=None):
    return [card_from_state(s, now) for s in states]


def test_a_case_in_human_review_keeps_its_place():
    """Going to the gate and coming back must not move a patient in line."""
    queue = [make_state("a", 3, T0), make_state("b", 3, T1), make_state("c", 3, T2)]
    before = positions(cards(queue))
    assert before["b"] == 2

    queue[1]["clinical_status"] = ClinicalStatus.HUMAN_REVIEW.value
    during = positions(cards(queue))

    queue[1]["clinical_status"] = ClinicalStatus.WAITING.value
    after = positions(cards(queue))

    assert during == before == after


def test_leaving_the_line_does_not_renumber_the_rest():
    """A patient who starts treatment holds no position, and the two behind them
    keep the numbers they had — position tracks the order_key, not the column.
    """
    queue = [make_state("a", 3, T0), make_state("b", 3, T1), make_state("c", 3, T2)]
    queue[0]["clinical_status"] = ClinicalStatus.TREATMENT_STARTED.value

    place = positions(cards(queue))
    assert "a" not in place
    assert place == {"b": 1, "c": 2}


def test_the_clock_does_not_reorder():
    """A long-waiting card may turn red; it does not move up."""
    queue = [make_state("a", 3, T0), make_state("b", 1, T2)]
    early = datetime(2026, 1, 1, 11, tzinfo=timezone.utc)
    later = early + timedelta(hours=6)

    order_early = [c.case_id for c in sort_cards(cards(queue, early))]
    order_later = [c.case_id for c in sort_cards(cards(queue, later))]

    assert order_early == order_later == ["b", "a"]
    assert cards(queue, later)[0].waited_min > cards(queue, early)[0].waited_min
