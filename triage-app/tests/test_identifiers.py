"""Identifiers typed into free text: redacted out of the model payload, and a
halt if any survive. The key-name drop cannot see a national ID written inside
chief_complaint; these tests pin the value scan that can.
"""

from __future__ import annotations

import pytest

from app.actors.normalizer import build_model_payload
from app.deterministic import verify_no_identifiers
from app.guards.identifiers import find_identifiers, redact_identifiers
from app.mock_cases import DEMO_CASES


def test_a_national_id_in_text_is_redacted():
    assert redact_identifiers("patient 123456789 chest pain") == "patient [REDACTED_ID] chest pain"


@pytest.mark.parametrize("text", ["050-123-4567", "0501234567", "050 123 4567",
                                  "+972-50-123-4567", "+972501234567", "02-123-4567",
                                  "077-1234567", "0771234567", "050-123-45-67",
                                  "050 123 45 67", "972-50-1234567"])
def test_phone_variants_are_redacted(text):
    assert redact_identifiers(f"call {text} now") == "call [REDACTED_PHONE] now"


@pytest.mark.parametrize("text", ["12345678-9", "12-345678-9"])
def test_an_id_written_with_its_check_digit_split_off_is_redacted(text):
    assert redact_identifiers(f"id {text} here") == "id [REDACTED_ID] here"


def test_an_email_is_redacted():
    assert redact_identifiers("mail dana@example.com") == "mail [REDACTED_EMAIL]"


@pytest.mark.parametrize("text", ["bp 120/80", "pain 9/10", "spo2 95%", "temp 37.1",
                                  "since 2026-09-25", "age 67", "300 mg twice daily",
                                  "case-0001", "P-1017"])
def test_clinical_text_is_left_alone(text):
    assert redact_identifiers(text) == text
    assert find_identifiers(text) == []


def test_the_digits_of_a_decimal_are_not_an_id():
    assert redact_identifiers("weight 70.123456789 kg") == "weight 70.123456789 kg"


def test_an_id_after_an_abbreviation_dot_is_still_redacted():
    assert redact_identifiers("No.123456789") == "No.[REDACTED_ID]"


def test_a_country_code_with_the_trunk_zero_kept_is_redacted_whole():
    assert redact_identifiers("call +972 050 123 4567") == "call [REDACTED_PHONE]"


def test_a_longer_digit_run_is_not_an_id():
    assert redact_identifiers("order 123456789012") == "order 123456789012"


def test_nested_values_are_redacted_with_their_path():
    value = {"history": {"visits": [{"note": "reach at 0521234567"}]}, "hr": 104}
    assert find_identifiers(value) == [("history.visits[0].note", "phone")]
    redacted = redact_identifiers(value)
    assert redacted == {"history": {"visits": [{"note": "reach at [REDACTED_PHONE]"}]}, "hr": 104}
    assert value["history"]["visits"][0]["note"] == "reach at 0521234567", "input not mutated"


def test_the_verifier_halts_on_a_value_leak():
    ok, why = verify_no_identifiers({"case_id": "c1", "chief_complaint": "id 123456789"})
    assert not ok
    assert "national_id in chief_complaint" in why


def test_the_verifier_still_halts_on_an_identifier_key():
    ok, why = verify_no_identifiers({"case_id": "c1", "phone": "x"})
    assert not ok
    assert "phone" in why


def test_the_payload_builder_keeps_only_what_the_policy_names():
    """Built from the allow-list: the nurse's prose, her proposed level, the
    routing metadata and every identifier stay on the case record. The prior
    visits' notes, which are prose from the CRM, are dropped too.
    """
    fields = dict(DEMO_CASES["clean"], free_text="patient 123456789, call 0521234567")
    history = {"known_conditions": ["COPD"], "last_updated": "x",
               "prior_visits": [{"date": "2025-01-01", "acuity": 2, "notes": "dana@example.com"}]}
    payload = build_model_payload("c1", fields, history, "over_18_years")

    assert sorted(payload) == ["age_band", "case_id", "chief_complaint", "history", "vitals"]
    assert payload["history"] == {"known_conditions": ["copd"],
                                  "prior_visits": [{"date": "2025-01-01", "acuity": 2}]}
    assert verify_no_identifiers(payload)[0]


def test_an_id_typed_into_a_history_label_is_redacted_before_verification():
    payload = build_model_payload("c1", DEMO_CASES["clean"],
                                  {"known_conditions": ["copd, id 123456789"], "prior_visits": []})
    assert payload["history"]["known_conditions"] == ["copd, id [REDACTED_ID]"]


def test_an_id_typed_into_the_complaint_never_reaches_the_model(run):
    """The complaint is a code now (I12). Prose with an id in it is an unusable
    value: the case waits for the nurse at the intake fix pause, and no model
    payload is ever built from it.
    """
    case = dict(DEMO_CASES["clean"], case_id="case-id-in-text",
                chief_complaint="chest tightness, patient id 123456789")
    state, pending, _ = run(case)
    assert pending["intake_fix_pending"] is True
    assert "chief_complaint" in state["missing_fields"]
    assert not state.get("redacted_payload")
