"""card_from_state: waited_min basis per status.

- waiting: counts from waiting_started_at (when cleared to the queue)
- treatment_started: counts from treatment_started_at
- patient_released: counts from released_at
- everything else: counts from arrival_time (unchanged, pre-existing behavior)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.views import card_from_state

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_waiting_case_counts_from_waiting_started_at():
    state = {
        "case_id": "c1",
        "clinical_status": "waiting",
        "arrival_time": _iso(90),
        "waiting_started_at": _iso(80),
    }
    card = card_from_state(state, now=NOW)
    assert card.waited_min == 80


def test_treatment_started_counts_from_treatment_started_at():
    state = {
        "case_id": "c1",
        "clinical_status": "treatment_started",
        "arrival_time": _iso(90),
        "waiting_started_at": _iso(80),
        "treatment_started_at": _iso(5),
    }
    card = card_from_state(state, now=NOW)
    assert card.waited_min == 5


def test_human_review_still_counts_from_arrival():
    state = {
        "case_id": "c1",
        "clinical_status": "human_review",
        "arrival_time": _iso(90),
    }
    card = card_from_state(state, now=NOW)
    assert card.waited_min == 90


def test_patient_released_counts_from_released_at():
    state = {
        "case_id": "c1",
        "clinical_status": "patient_released",
        "arrival_time": _iso(90),
        "released_at": _iso(3),
    }
    card = card_from_state(state, now=NOW)
    assert card.waited_min == 3
