from app.deterministic import assign_order_key

from itertools import product

EARLY = "2026-09-19T10:00:00+00:00"
LATE = "2026-09-19T10:05:00+00:00"


def test_more_acute_patient_is_always_ahead():
    for a, b in product(range(1, 6), repeat=2):
        if a < b:
            assert assign_order_key(a, LATE) < assign_order_key(b, EARLY), (a, b)


def test_a_missing_arrival_time_is_refused_not_replaced():
    import pytest

    with pytest.raises(ValueError):
        assign_order_key(3, None)
