"""ESI v5 decision point D: the age-banded danger-zone vital signs.

The table is Figure 2-2 of the handbook (docs/Emergency_Severity_Index_Handbook.pdf,
p. 12): seven age bands, a heart-rate and respiratory-rate ceiling for each, and
SpO2 < 92% for all of them. This module computes exactly that and nothing more.

It annotates. It never changes an acuity. The handbook treats D as a judgment
applied in context — its own examples keep a heart rate of 102 at level 3 and
uptriage an SpO2 of 91% for reasons no threshold holds — so the breaches are
attached to the case for the humans and the classifier to weigh
(SPECIFICATION.md § Safety invariants, the note on danger-zone vitals).

The age band is derived from the CRM's date of birth at identity resolution and
is the only age fact the case carries: a seven-way bucket identifies nobody,
where a birth date is identifier-class data (I11).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

# (band, heart rate above, respiratory rate above). SpO2 < 92% breaches in every band.
# Figure 2-2, ESI v5 — the seven rows in the handbook's order.
DANGER_ZONE_LIMITS: tuple[tuple[str, int, int], ...] = (
    ("under_1_month", 190, 60),
    ("1_to_12_months", 180, 55),
    ("1_to_3_years", 140, 40),
    ("3_to_5_years", 120, 35),
    ("5_to_12_years", 120, 30),
    ("12_to_18_years", 100, 20),
    ("over_18_years", 100, 20),
)
SPO2_FLOOR = 92

AGE_BANDS: tuple[str, ...] = tuple(band for band, _, _ in DANGER_ZONE_LIMITS)


def age_band(date_of_birth: str, today: date | None = None) -> str:
    """The ESI age band for a birth date, at `today`.

    Month boundaries follow the handbook's rows literally: under one month, one
    to twelve months, then years. A malformed date raises — the caller decides
    whether a patient with an unreadable record continues without a band.
    """
    born = date.fromisoformat(date_of_birth[:10])
    now = today or datetime.now(timezone.utc).date()
    months = (now.year - born.year) * 12 + (now.month - born.month) - (now.day < born.day)
    if months < 1:
        return "under_1_month"
    if months < 12:
        return "1_to_12_months"
    years = months // 12
    if years < 3:
        return "1_to_3_years"
    if years < 5:
        return "3_to_5_years"
    if years < 12:
        return "5_to_12_years"
    if years < 18:
        return "12_to_18_years"
    return "over_18_years"


def danger_zone(vitals: dict[str, Any] | None, band: str | None) -> list[str]:
    """Which of `vitals` sit outside the band's limits, as "hr>100"-style labels.

    Without a band nothing can be judged — a new patient with no CRM record has
    no date of birth — so the answer is empty, and the caller records that the
    check did not run rather than pretending the vitals were normal.
    """
    if not vitals or band not in AGE_BANDS:
        return []
    hr_limit, rr_limit = next((hr, rr) for b, hr, rr in DANGER_ZONE_LIMITS if b == band)
    breaches: list[str] = []
    checks = (("hr", vitals.get("hr"), lambda v: v > hr_limit, f"hr>{hr_limit}"),
              ("rr", vitals.get("rr"), lambda v: v > rr_limit, f"rr>{rr_limit}"),
              ("spo2", vitals.get("spo2"), lambda v: v < SPO2_FLOOR, f"spo2<{SPO2_FLOOR}"))
    for _, value, breached, label in checks:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and breached(value):
            breaches.append(label)
    return breaches
