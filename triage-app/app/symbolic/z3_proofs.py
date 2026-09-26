"""Design-time proofs over the acuity rules (I4), by the real Z3 solver.

The other three engines in this package answer questions about *one case* at
runtime. Z3 is the odd one out: it runs before any patient exists, over every
input at once, and answers a different question: not "is this case allowed?"
but "does an input exist that breaks the rule?".

The method throughout is refutation. To prove a claim holds everywhere, assert
its negation and ask for `unsat`: nothing satisfies the negation, so nothing
violates the claim. A `sat` answer is not a failure message, it is the
counterexample: the exact acuity pair that breaks the rule.

Both acuity levels are bounded to `ACUITY_LEVELS`, because the code now enforces
that: `guards.unusable_fields` sends any other value back to the nurse before it
reaches a queue key. A premise is only legitimate here when something guarantees
it — and this one buys readable counterexamples, `nurse = 4, system = 5` rather
than `nurse = 0, system = -1`.

The gap itself is still *derived*, the way `compute_acuity_gap` derives it, never
assumed non-negative.
"""

from __future__ import annotations

from z3 import And, ArithRef, BoolRef, If, Implies, Int, Not, Or, Solver, unsat

from app.deterministic import AGREE_GAP, MINOR_GAP
from app.guards import ACUITY_LEVELS

# The three outcomes of resolve_acuity, as distinct numbers Z3 can compare.
# Names, not levels: ACUITY_AGREE / ACUITY_GAP_MINOR / ACUITY_GAP_MAJOR.
AGREE, MINOR, MAJOR = 0, 1, 2


LOW, HIGH = min(ACUITY_LEVELS), max(ACUITY_LEVELS)


def _levels() -> tuple[ArithRef, ArithRef, ArithRef, BoolRef]:
    """The two proposed levels, the gap between them as the code computes it, and
    the premise that both are real ESI levels.
    """
    nurse, system = Int("nurse"), Int("system")
    diff = nurse - system
    gap = If(diff >= 0, diff, -diff)                   # Z3 Int has no Abs
    return nurse, system, gap, And(LOW <= nurse, nurse <= HIGH, LOW <= system, system <= HIGH)


def _refute(claim: BoolRef) -> tuple[bool, str]:
    """(claim holds for every value, counterexample) — proof by refutation."""
    solver = Solver()
    solver.add(Not(claim))
    if solver.check() == unsat:
        return True, ""
    return False, str(solver.model())


def bands_partition_every_gap() -> tuple[bool, str]:
    """I4, first half: every gap lands in exactly one band.

    The three bands as the spec states them — unordered claims about a gap, not
    an if-chain. Proving them total and exclusive is what makes "exactly one
    outcome applies" true of the *rule*, independent of any code that reads it.
    """
    _, _, gap, esi = _levels()
    keep, nurse_wins, at_gate = gap == AGREE_GAP, gap == MINOR_GAP, gap > MINOR_GAP
    return _refute(Implies(esi, Or(
        And(keep, Not(nurse_wins), Not(at_gate)),
        And(Not(keep), nurse_wins, Not(at_gate)),
        And(Not(keep), Not(nurse_wins), at_gate),
    )))


def code_obeys_the_bands(
    agree_at: int = AGREE_GAP, minor_at: int = MINOR_GAP
) -> tuple[bool, str]:
    """I4, second half: `resolve_acuity`'s if-chain satisfies all three bands.

    The chain is ordered and the bands are not, so agreement is a real claim:
    a chain that tested `gap <= 1` first, or ordered its arms differently, would
    still return exactly one answer while contradicting a band. The thresholds
    are parameters so a test can mutate the chain and see the proof fail.
    """
    _, _, gap, esi = _levels()
    chain = If(gap == agree_at, AGREE, If(gap == minor_at, MINOR, MAJOR))
    return _refute(Implies(esi, And(
        Implies(gap == AGREE_GAP, chain == AGREE),
        Implies(gap == MINOR_GAP, chain == MINOR),
        Implies(gap > MINOR_GAP, chain == MAJOR),
    )))


def settled_acuity_stays_in_range() -> tuple[bool, str]:
    """I13's range half, where I4 settles a level without a human.

    Both auto-settling bands return the nurse's number unchanged, so a settled
    acuity is an ESI level whenever the inputs were. `guards.unusable_fields`
    makes that premise true at the front door.
    """
    nurse, _, gap, esi = _levels()
    settled = nurse                                   # both auto-settling bands return the nurse's level
    return _refute(Implies(And(esi, gap <= MINOR_GAP), And(LOW <= settled, settled <= HIGH)))


PROOFS = {
    "I4 bands partition every gap": bands_partition_every_gap,
    "I4 resolve_acuity obeys the bands": code_obeys_the_bands,
    "I13 a settled acuity stays in 1-5": settled_acuity_stays_in_range,
}


def main() -> int:
    """Run every proof and print its verdict. Exit 1 if any counterexample exists."""
    failed = 0
    for name, proof in PROOFS.items():
        proved, counterexample = proof()
        print(f"{'PROVED ' if proved else 'BROKEN '} {name}"
              + (f"\n          counterexample: {counterexample}" if counterexample else ""))
        failed += not proved
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
