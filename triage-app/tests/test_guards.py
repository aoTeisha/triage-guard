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
