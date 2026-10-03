"""Guards are the deterministic half of the machine — every one of these must
hold without an LLM, a network call, or a clock.
"""

import pytest

from app import guards

CLEAN = {
    "case_id": "case-1",
    "channel": "website",
    "national_id": "300000005",
    "nurse_proposed_acuity": 3,
    "chief_complaint": "chest_pain",
    "vitals": {"hr": 104, "bp": "148/92", "spo2": 95, "temp_c": 37.1},
    "free_text": "Patient reports pressure in the chest, worse on exertion.",
}

MISSING = {k: v for k, v in CLEAN.items() if k not in ("nurse_proposed_acuity", "vitals")}

FAILED = {k: v for k, v in CLEAN.items() if k in ("case_id", "channel")}

def test_missing_fields_lists_exactly_the_absent_ones():
    assert guards.missing_fields(MISSING) == ["nurse_proposed_acuity", "vitals"]


def test_missing_fields_empty_on_a_complete_payload():
    assert guards.missing_fields(CLEAN) == []


def test_nothing_usable_only_when_no_clinical_field_survives():
    assert guards.nothing_usable(FAILED) is True
    assert guards.nothing_usable(MISSING) is False


def test_required_fields_complete_and_valid_passes_on_clean():
    passed, _ = guards.required_fields_complete_and_valid(CLEAN)
    assert passed is True


def test_required_fields_complete_and_valid_fails_on_missing():
    passed, why = guards.required_fields_complete_and_valid(MISSING)
    assert passed is False
    assert "nurse_proposed_acuity" in why


def test_a_payload_without_a_string_case_id_is_invalid_schema():
    flagged, reason = guards.not_input_is_valid({**CLEAN, "case_id": None})
    assert flagged is True
    assert reason == "invalid_schema"


def test_parse_intake_rejects_an_invalid_schema_with_its_reason():
    from app.actors.intake import parse_intake
    from app.events import IntakeOutcome

    result = parse_intake({**CLEAN, "case_id": None})
    assert result.outcome == IntakeOutcome.INVALID_INPUT_DETECTED
    assert result.reason == "invalid_schema"


def test_a_well_formed_payload_is_valid():
    assert guards.not_input_is_valid(CLEAN) == (False, "input is valid")


# ---- acuity range (I4 / I13): present but unusable ---------------------------


@pytest.mark.parametrize("value", [0, 6, 7, -1, True, False, 3.0, "3", None])
def test_an_acuity_outside_the_esi_levels_is_unusable(value):
    """`True` is in the list on purpose: bool subclasses int in Python, so
    `True in range(1, 6)` is true and a checkbox could pass for ESI 1.
    `None` is absent, not unusable — `missing_fields` reports that one.
    """
    payload = {**CLEAN, "nurse_proposed_acuity": value}
    assert guards.unusable_fields(payload) == ([] if value is None else ["nurse_proposed_acuity"])


@pytest.mark.parametrize("value", [1, 2, 3, 4, 5])
def test_every_real_esi_level_is_usable(value):
    assert guards.unusable_fields({**CLEAN, "nurse_proposed_acuity": value}) == []


@pytest.mark.parametrize("code", guards.CHIEF_COMPLAINTS)
def test_every_complaint_code_is_usable(code):
    assert guards.unusable_fields({**CLEAN, "chief_complaint": code}) == []


def test_a_complaint_that_is_not_a_code_is_unusable():
    """Free text typed here would be refused by the privacy policy three nodes
    later (I12); refusing it at the door sends it back to the nurse instead."""
    assert guards.unusable_fields({**CLEAN, "chief_complaint": "chest tightness for 2 hours"}) \
        == ["chief_complaint"]


@pytest.mark.parametrize("vitals", [
    {"pain_score": 7},                       # not a vital sign the model may see
    {"hr": "ninety"},                        # text where a number belongs
    {"hr": "104"},
    {"spo2": True},                          # bool counts as int in Python, not to the policy
    {"temp_c": float("nan")},
    {"bp": "high"},
    {"bp": 120},
    {"bp": "120/80\n"},                      # Python's `$` would pass this; RE2's does not
    "hr 104, bp 148/92",                     # not a dict at all
    [104],
])
def test_vitals_the_privacy_policy_would_refuse_are_unusable(vitals):
    """Refused here, the nurse fixes them at the intake pause. Let through, the
    privacy policy refuses them three nodes later and the case halts (I12)."""
    assert guards.unusable_fields({**CLEAN, "vitals": vitals}) == ["vitals"]


@pytest.mark.parametrize("vitals", [
    CLEAN["vitals"],
    {},
    {"hr": 104, "rr": 18, "bp": "90/60", "spo2": 98.5, "temp_c": 36},
    {"hr": None, "bp": None},                # an absent reading, as it is to the policy
])
def test_vitals_of_the_allowed_shape_are_usable(vitals):
    assert guards.unusable_fields({**CLEAN, "vitals": vitals}) == []


# ---- believable vitals --------------------------------------------------------------


@pytest.mark.parametrize(("sign", "low", "high"), [
    ("hr", 20, 300),
    ("rr", 2, 120),
    ("spo2", 30, 100),
    ("temp_c", 20, 46),
])
def test_each_vital_is_bounded_inclusively(sign, low, high):
    """The edge values are believable; one step past either edge is not."""
    assert guards.VITAL_BOUNDS[sign] == (low, high)
    for inside in (low, high, low + 0.5, high - 0.5):
        assert guards.unusable_fields({**CLEAN, "vitals": {sign: inside}}) == [], inside
    for outside in (low - 0.1, high + 0.1, 0, -1):
        assert guards.unusable_fields({**CLEAN, "vitals": {sign: outside}}) == ["vitals"], outside


@pytest.mark.parametrize("bp", ["40/39", "300/200", "300/10", "41/10", "120/80"])
def test_blood_pressure_at_the_bounds_is_usable(bp):
    assert guards.unusable_fields({**CLEAN, "vitals": {"bp": bp}}) == []


@pytest.mark.parametrize("bp", [
    "39/20",      # systolic below 40
    "301/80",     # systolic above 300
    "120/09",     # diastolic below 10
    "250/201",    # diastolic above 200
    "80/120",     # the two numbers swapped
    "90/90",      # systolic must be above diastolic
])
def test_blood_pressure_outside_the_bounds_is_unusable(bp):
    assert guards.unusable_fields({**CLEAN, "vitals": {"bp": bp}}) == ["vitals"]


def test_a_phone_number_typed_as_heart_rate_is_unusable():
    """A number of the right type, so the shape check and the privacy policy both
    let it through, and `redact_identifiers` walks only strings. The bound is the
    one check that stops it reaching the model as a heart rate."""
    assert guards.unusable_fields({**CLEAN, "vitals": {"hr": 501234567}}) == ["vitals"]


@pytest.mark.parametrize("vitals", [
    # Septic shock: tachycardic, tachypnoeic, hypotensive, hypoxic, febrile.
    {"hr": 160, "rr": 40, "bp": "70/30", "spo2": 82, "temp_c": 40.5},
    # Complete heart block and hypothermia.
    {"hr": 30, "rr": 8, "bp": "60/20", "spo2": 88, "temp_c": 28},
    # Neonate in respiratory distress, well past every danger-zone limit for the band.
    {"hr": 220, "rr": 90, "spo2": 60, "temp_c": 35},
    # Heatstroke and hypertensive emergency.
    {"hr": 180, "rr": 36, "bp": "260/150", "spo2": 94, "temp_c": 41},
])
def test_a_critically_ill_patients_real_vitals_are_usable(vitals):
    """Bounds of believability, not of normality: the sickest real patient passes."""
    assert guards.unusable_fields({**CLEAN, "vitals": vitals}) == []


def test_every_danger_zone_limit_is_believable():
    """A reading just past a danger-zone limit is the one D exists to flag, so it
    must reach the classifier rather than be refused at intake."""
    from app import esi

    for _band, hr_limit, rr_limit in esi.DANGER_ZONE_LIMITS:
        vitals = {"hr": hr_limit + 1, "rr": rr_limit + 1, "spo2": esi.SPO2_FLOOR - 1}
        assert guards.unusable_fields({**CLEAN, "vitals": vitals}) == []


def test_fahrenheit_typed_as_celsius_is_unusable():
    assert guards.unusable_fields({**CLEAN, "vitals": {"temp_c": 98.6}}) == ["vitals"]


def test_an_unusable_acuity_goes_back_to_the_nurse_not_to_rejection():
    """A typo is not an attack. It routes like a missing field, so the case keeps
    its arrival time and the patient keeps their place (I10).
    """
    assert guards.classify_intake_payload({**CLEAN, "nurse_proposed_acuity": 7}) \
        == "MISSING_FIELDS_DETECTED"


def test_free_text_is_optional_so_a_structured_form_is_complete_without_it():
    """The real-case form has no free-text box; the model never reads free text (I12)."""
    payload = {k: v for k, v in CLEAN.items() if k != "free_text"}
    assert "free_text" not in guards.missing_fields(payload)
    assert "free_text" not in guards.NURSE_SUPPLIED_FIELDS
