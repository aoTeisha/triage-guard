"""ESI v5 decision point D (`app/esi.py`): the age bands and the danger-zone
table, pinned to the handbook, Figure 2-2 (PDF p. 12).

Nothing here asserts an acuity. The table annotates; the spec's note under
Safety invariants says why a breach never changes a level.
"""

from __future__ import annotations

from datetime import date

import pytest

from app import esi

TODAY = date(2026, 9, 26)


# ---- the table is the handbook's --------------------------------------------


def test_the_table_is_figure_2_2():
    """Seven rows, in the handbook's order, with its numbers. If this changes,
    the PDF page must have changed first.
    """
    assert esi.DANGER_ZONE_LIMITS == (
        ("under_1_month", 190, 60),
        ("1_to_12_months", 180, 55),
        ("1_to_3_years", 140, 40),
        ("3_to_5_years", 120, 35),
        ("5_to_12_years", 120, 30),
        ("12_to_18_years", 100, 20),
        ("over_18_years", 100, 20),
    )
    assert esi.SPO2_FLOOR == 92


# ---- age bands ---------------------------------------------------------------


@pytest.mark.parametrize("born,band", [
    (date(2026, 9, 10), "under_1_month"),
    (date(2026, 8, 26), "1_to_12_months"),    # one month today: no longer "under"
    (date(2026, 3, 1), "1_to_12_months"),
    (date(2025, 9, 26), "1_to_3_years"),      # first birthday today
    (date(2024, 1, 1), "1_to_3_years"),
    (date(2023, 9, 26), "3_to_5_years"),      # third birthday today
    (date(2021, 9, 27), "3_to_5_years"),      # fifth birthday tomorrow
    (date(2021, 9, 26), "5_to_12_years"),
    (date(2015, 1, 1), "5_to_12_years"),
    (date(2014, 9, 26), "12_to_18_years"),
    (date(2008, 9, 27), "12_to_18_years"),    # eighteenth birthday tomorrow
    (date(2008, 9, 26), "over_18_years"),
    (date(1949, 9, 18), "over_18_years"),
])
def test_each_birth_date_lands_in_its_band(born, band):
    assert esi.age_band(born.isoformat(), today=TODAY) == band


def test_a_timestamp_is_read_as_its_date():
    assert esi.age_band("1949-09-18T00:00:00+00:00", today=TODAY) == "over_18_years"


def test_an_unreadable_date_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        esi.age_band("UNKNOWN", today=TODAY)


# ---- danger zone ---------------------------------------------------------------


@pytest.mark.parametrize("band,hr_limit,rr_limit", [(b, hr, rr) for b, hr, rr in esi.DANGER_ZONE_LIMITS])
def test_a_vital_at_the_limit_is_not_a_breach_and_one_above_is(band, hr_limit, rr_limit):
    assert esi.danger_zone({"hr": hr_limit, "rr": rr_limit, "spo2": 92}, band) == []
    assert esi.danger_zone({"hr": hr_limit + 1, "rr": rr_limit + 1, "spo2": 91}, band) == [
        f"hr>{hr_limit}", f"rr>{rr_limit}", "spo2<92"]


def test_the_handbooks_own_example_is_a_breach_and_nothing_more():
    """Chapter 6, Example Four: heart rate 102 against a limit of 100, all else
    normal. A breach — and the handbook keeps the patient at level 3.
    """
    assert esi.danger_zone({"hr": 102, "bp": "130/80", "spo2": 97, "temp_c": 36.9},
                           "over_18_years") == ["hr>100"]


def test_a_missing_vital_is_not_judged():
    assert esi.danger_zone({"bp": "120/80"}, "over_18_years") == []


def test_no_band_means_no_check():
    """A new patient has no CRM record, so no date of birth and no band. The
    answer is "not checked", never "normal" — the classify node writes that.
    """
    assert esi.danger_zone({"hr": 180, "spo2": 80}, None) == []
    assert esi.danger_zone({"hr": 180}, "not_a_band") == []


def test_a_boolean_is_not_a_reading():
    assert esi.danger_zone({"hr": True, "spo2": False}, "over_18_years") == []
