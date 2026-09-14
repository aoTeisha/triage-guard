"""The card carries no field the browser shouldn't see.

The board never ships raw `TriageState` to a page. This is the test that keeps
that true when someone adds a field to the state model later.
"""

from __future__ import annotations

from app.views import PATIENT_LABELS, CaseCard, card_from_state

from .conftest import make_state

T0 = "2026-01-01T08:00:00+00:00"

ALLOWED = {
    "case_id", "patient_id", "patient_label", "complaint", "status", "acuity", "bucket",
    "acuity_source", "nurse_proposed_acuity", "system_proposed_acuity",
    "arrival_time", "waited_min", "order_key", "flags", "degraded",
    "gate_pending", "red_flag_fired",
}

# Identifier-class or model-facing fields. None of these may ever appear on a card.
FORBIDDEN = {
    "name", "date_of_birth", "dob", "phone", "patient_history",
    "raw_payload", "redacted_payload", "parsed_fields", "safety_verdict",
}


def test_card_fields_are_exactly_the_allow_list():
    assert set(CaseCard.model_fields) == ALLOWED


def test_no_identifier_or_payload_field_reaches_the_card():
    state = make_state("c", 3, T0)
    state["patient_history"] = {"name": "Ada L.", "date_of_birth": "1950-01-01"}
    state["raw_payload"] = {"free_text": "chest pain", "name": "Ada L."}
    state["redacted_payload"] = {"chief_complaint": "chest pain"}

    dumped = card_from_state(state).model_dump()
    assert FORBIDDEN.isdisjoint(dumped)
    assert "Ada L." not in str(dumped)


def test_the_complaint_headline_comes_from_the_redacted_payload():
    """The card leads with the complaint, and it must be the redacted copy — the
    one that already passed `verify_no_identifiers`.
    """
    state = make_state("c", 3, T0)
    state["redacted_payload"] = {"chief_complaint": "chest tightness for 2 hours"}
    state["parsed_fields"] = {"chief_complaint": "UNREDACTED", "name": "Ada L."}

    assert card_from_state(state).complaint == "chest tightness for 2 hours"


def test_a_case_with_no_redacted_payload_has_no_complaint():
    assert card_from_state(make_state("c", 3, T0)).complaint == ""


def test_patient_label_is_derived_from_crm_status_not_fetched():
    """No CRM call, and no name: the label says whether a record existed at
    intake, which is all a card needs and all it is allowed to show.
    """
    for status, label in PATIENT_LABELS.items():
        state = make_state("c", 3, T0)
        state["crm_status"] = status
        assert card_from_state(state).patient_label == label


def test_an_unknown_crm_status_still_renders():
    state = make_state("c", 3, T0)
    state["crm_status"] = None
    assert card_from_state(state).patient_label == "Unknown"
