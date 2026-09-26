"""I1 at the level of the real function. `app/symbolic/z3_proofs.py` proves the
general claim over every acuity pair and every arrival instant; these run the
actual `assign_order_key` on concrete ones.

The earlier version of this file compared only pairs with `a < b`, always giving
the more acute patient the later arrival. That leaves the tie-break — the only
thing the second half of the key is for — completely untested.
"""

from itertools import product

import pytest

from app.deterministic import assign_order_key

EARLY = "2026-09-19T10:00:00+00:00"
LATE = "2026-09-19T10:05:00+00:00"


@pytest.mark.parametrize("a,b", [(a, b) for a, b in product(range(1, 6), repeat=2) if a < b])
def test_more_acute_patient_is_always_ahead(a, b):
    """Even when the more acute patient arrived last."""
    assert assign_order_key(a, LATE) < assign_order_key(b, EARLY)


@pytest.mark.parametrize("acuity", range(1, 6))
def test_within_one_level_the_earlier_patient_is_ahead(acuity):
    assert assign_order_key(acuity, EARLY) < assign_order_key(acuity, LATE)


def test_the_key_does_not_order_by_text():
    """A +03:00 timestamp reads later than a +00:00 one while being an hour
    earlier. The key is epoch seconds, so the patient keeps their real place.
    """
    earlier_moment = "2026-09-19T10:00:00+03:00"   # 07:00 UTC
    later_moment = "2026-09-19T09:00:00+00:00"
    assert earlier_moment > later_moment                                  # as text
    assert assign_order_key(3, earlier_moment) < assign_order_key(3, later_moment)


def test_a_missing_arrival_time_is_refused_not_replaced():
    with pytest.raises(ValueError):
        assign_order_key(3, None)


def test_a_naive_arrival_time_is_refused():
    """No zone means no shared clock, so the instant is unknowable."""
    with pytest.raises(ValueError):
        assign_order_key(3, "2026-09-19T10:00:00")
