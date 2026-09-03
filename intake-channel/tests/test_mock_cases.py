"""Tests for channel.mock_cases.build_case — the four demo payloads."""

import pytest

from channel.mock_cases import build_case
from channel.patient_lookup import PatientLookupResult

FOUND = PatientLookupResult(
    status="found", record={"stable_patient_id": "P-1005", "name": "David Friedman"}
)
NOT_FOUND = PatientLookupResult(status="not_found")
DB_ERROR = PatientLookupResult(status="db_error")


def test_clean_has_all_required_fields():
    case = build_case(FOUND, "P-1005", "clean")

    assert case["stable_patient_id"] == "P-1005"
    assert case["nurse_proposed_acuity"] == 3
    assert "vitals" in case
    assert case["chief_complaint"]


def test_missing_omits_acuity_and_vitals():
    case = build_case(FOUND, "P-1005", "missing")

    assert "nurse_proposed_acuity" not in case
    assert "vitals" not in case
    # The system never invents an acuity value in its place.
    assert "acuity" not in str(case.keys())


def test_failed_has_no_clinical_fields():
    case = build_case(FOUND, "P-1005", "failed")

    assert "chief_complaint" not in case
    assert "vitals" not in case
    assert "nurse_proposed_acuity" not in case
    assert case["case_id"]


def test_injection_free_text_contains_attack():
    case = build_case(FOUND, "P-1005", "injection")

    assert "ignore previous instructions" in case["free_text"].lower()


def test_stable_patient_id_always_from_typed_id_not_lookup():
    """The lookup result must never override the id the nurse typed —
    even a found record for a different id shouldn't leak in."""
    mismatched_lookup = PatientLookupResult(
        status="found", record={"stable_patient_id": "P-9999", "name": "Someone Else"}
    )

    case = build_case(mismatched_lookup, "P-1005", "clean")

    assert case["stable_patient_id"] == "P-1005"


@pytest.mark.parametrize("lookup", [FOUND, NOT_FOUND, DB_ERROR])
@pytest.mark.parametrize("submission_type", ["clean", "missing", "failed", "injection"])
def test_all_lookup_outcomes_produce_a_valid_case(lookup, submission_type):
    """A case is built the same way regardless of whether the lookup found
    a record, found nothing, or the CRM was unreachable — all three continue
    per SPECIFICATION.md's fail-open rule."""
    case = build_case(lookup, "P-1005", submission_type)

    assert case["case_id"]
    assert case["channel"] == "website"


def test_case_id_is_unique_per_call():
    case_a = build_case(FOUND, "P-1005", "clean")
    case_b = build_case(FOUND, "P-1005", "clean")

    assert case_a["case_id"] != case_b["case_id"]


def test_unknown_submission_type_raises():
    with pytest.raises(ValueError):
        build_case(FOUND, "P-1005", "not-a-real-type")  # type: ignore[arg-type]
