"""The Z3 proofs (`app.symbolic.z3_proofs`): I4's bands, and I13's range half.

Per-gap tests sample; these hold for every integer pair. The last two tests are
the point of the file: a deliberately wrong if-chain must come back with a
counterexample, or the proofs would be checks that cannot fail.
"""

from __future__ import annotations

import pytest

from app.deterministic import assign_order_key, resolve_acuity
from app.labels import Transition
from app.symbolic import z3_proofs


@pytest.mark.parametrize("name", list(z3_proofs.PROOFS))
def test_every_proof_holds(name):
    proved, counterexample = z3_proofs.PROOFS[name]()
    assert proved, f"{name} broken by {counterexample}"


def test_a_chain_that_never_takes_the_nurses_level_is_caught():
    """Both auto-settling arms collapsed onto gap 0: a gap of 1 would go to the
    charge nurse, contradicting the second band.
    """
    proved, counterexample = z3_proofs.code_obeys_the_bands(agree_at=0, minor_at=0)
    assert not proved
    assert counterexample                      # names the pair, e.g. nurse = 2, system = 1


def test_a_chain_with_its_thresholds_swapped_is_caught():
    proved, counterexample = z3_proofs.code_obeys_the_bands(agree_at=1, minor_at=0)
    assert not proved
    assert counterexample


def test_the_proven_thresholds_are_the_ones_the_code_uses():
    """The proofs above model `resolve_acuity`'s chain rather than calling it.
    This is the tie: the modeled thresholds are 0 and 1, and so are the real ones.
    """
    assert resolve_acuity(3, 3)[2] is Transition.ACUITY_AGREE
    assert resolve_acuity(3, 4)[2] is Transition.ACUITY_GAP_MINOR
    assert resolve_acuity(3, 5)[2] is Transition.ACUITY_GAP_MAJOR


# ---- I1: the queue order ----------------------------------------------------


def test_sorting_by_arrival_before_acuity_is_caught():
    """The fairest-looking bug in triage: first come first served, full stop.
    A more acute patient who arrived a minute later ends up behind.
    """
    proved, counterexample = z3_proofs.more_acute_is_never_behind(time_first=True)
    assert not proved
    assert counterexample


def test_a_reversed_tie_break_is_caught():
    proved, counterexample = z3_proofs.at_the_same_level_the_earlier_patient_is_ahead(
        newest_first=True)
    assert not proved
    assert counterexample


def test_a_reversed_tie_break_hides_from_the_acuity_property():
    """Why I1 needs both halves proved. Reversing the tie-break leaves "a more
    acute patient is never behind" perfectly true — the damage is entirely
    inside one level, where that property says nothing.
    """
    assert z3_proofs.more_acute_is_never_behind(newest_first=True)[0] is True


def test_the_real_key_orders_the_way_the_proof_assumes():
    """The tie between model and code: the proofs reason about a pair built from
    (acuity, arrival), and this is `assign_order_key` actually doing that.
    """
    early, late = "2026-09-19T10:00:00+00:00", "2026-09-19T10:05:00+00:00"
    assert assign_order_key(2, late) < assign_order_key(3, early)      # acuity wins
    assert assign_order_key(3, early) < assign_order_key(3, late)      # then arrival
    assert isinstance(assign_order_key(3, early)[1], float)           # numeric, not text
