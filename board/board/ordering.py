"""Queue ordering — and the deliberate absence of any ordering logic.

The board has no sort rules of its own. `order_key` is `(bucket_rank,
arrival_time)`, written by `app.deterministic.assign_order_key` and re-keyed only
when acuity changes. Everything here is `sorted()` plus one derivation.

Three spec rules live in this file:

  * emergent is never behind queued — falls out of bucket_rank, not out of code here
  * position is display, not state — derived at render, never stored
  * a card keeps its place while it sits in another column — so position is
    computed over the *global* ordering, not per column
"""

from __future__ import annotations

from app.states import ClinicalStatus
from app.views import CaseCard

# Still in line. A patient who has started treatment, is being released, or has
# left is no longer waiting for one, so they hold no position — and removing
# them must not renumber anyone above them, which is why the set is explicit.
QUEUEING_STATUSES = frozenset({
    ClinicalStatus.WAITING.value,
    ClinicalStatus.HUMAN_REVIEW.value,
    ClinicalStatus.REASSESSMENT_REQUIRED.value,
})

# Sorts after every real key: (0, ...) emergent, (1, ...) queued, (2, "") unkeyed.
# A case parked at the acuity gate has no settled acuity, so it legitimately has
# no order_key yet — it shows at the bottom rather than pretending to a position.
_UNKEYED = (2, "")


def sort_cards(cards: list[CaseCard]) -> list[CaseCard]:
    """The whole sort. `sorted` is stable, so equal keys keep insertion order."""
    return sorted(cards, key=lambda c: c.order_key or _UNKEYED)


def positions(cards: list[CaseCard]) -> dict[str, int]:
    """case_id → queue position, 1-based, over the global ordering.

    Global and not per-column on purpose: going to `human_review` or
    `reassessment_required` must not move a patient in line, so a card in those
    columns shows the same number it had in `waiting`. Cards that have left the
    line get no entry at all rather than a position of their own.
    """
    in_line = sort_cards([c for c in cards if c.status in QUEUEING_STATUSES])
    return {c.case_id: i for i, c in enumerate(in_line, start=1)}
