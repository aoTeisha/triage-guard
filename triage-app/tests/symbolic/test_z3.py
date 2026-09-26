"""The Z3 proofs (`app.symbolic.z3_proofs`): I4's bands, and I13's range half.

Per-gap tests sample; these hold for every integer pair. The last two tests are
the point of the file: a deliberately wrong if-chain must come back with a
counterexample, or the proofs would be checks that cannot fail.
"""

from __future__ import annotations

import pytest

from app.deterministic import resolve_acuity
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
