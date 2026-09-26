"""The privacy policy (`policy/privacy.rego`), evaluated by the real `opa`
binary: what the model may see (I11, I12). An allow-list — a field reaches the
model because the policy names it, a value because it has the declared shape.
Every deny names the path. A missing engine is a deny.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from app import esi
from app.deterministic import verify_no_identifiers
from app.guards import CHIEF_COMPLAINTS
from app.symbolic import opa

CLEAN = {
    "case_id": "case-1",
    "chief_complaint": "chest_pain",
    "vitals": {"hr": 104, "bp": "148/92", "spo2": 95, "temp_c": 37.1},
    "age_band": "over_18_years",
    "history": {"known_conditions": ["hypertension", "type 2 diabetes"],
                "prior_visits": [{"date": "2025-11-02", "acuity": 3}]},
}


def decide(payload):
    return opa.evaluate({"payload": payload}, policy=opa.PRIVACY_POLICY, query=opa.PRIVACY_QUERY)


def refused(payload) -> list[str]:
    decision = decide(payload)
    assert decision["allow"] is False, decision
    assert decision["deny_reasons"], "a deny must carry a reason"
    return decision["deny_reasons"]


def test_the_clean_payload_is_allowed():
    assert decide(CLEAN) == {"allow": True, "deny_reasons": []}


def test_a_payload_with_only_the_required_fields_is_allowed():
    """No history and no band: a new patient the CRM has never seen."""
    assert decide({"case_id": "c", "chief_complaint": "fever", "vitals": {"temp_c": 39.2}})["allow"]


# ---- the allow-list -------------------------------------------------------------


@pytest.mark.parametrize("field", ["free_text", "nurse_proposed_acuity", "channel", "mothers_maiden_name"])
def test_a_field_the_policy_does_not_name_is_refused(field):
    """The old check refused six named keys and passed everything else. This is
    the difference: a field nobody thought of is refused too.
    """
    reasons = refused({**CLEAN, field: "x"})
    assert reasons == [f'field "{field}" is not approved for the model']


@pytest.mark.parametrize("key", ["name", "national_id", "stable_patient_id", "date_of_birth", "dob", "phone", "email"])
def test_an_identifier_key_is_refused_inside_an_approved_container(key):
    reasons = refused({**CLEAN, "history": {**CLEAN["history"], key: "x"}})
    assert any(key in r for r in reasons)


# ---- closed values ---------------------------------------------------------------


@pytest.mark.parametrize("code", CHIEF_COMPLAINTS)
def test_every_complaint_code_is_allowed(code):
    assert decide({**CLEAN, "chief_complaint": code})["allow"]


def test_prose_in_the_complaint_is_refused():
    assert refused({**CLEAN, "chief_complaint": "chest tightness for 2 hours"}) == [
        'chief_complaint "chest tightness for 2 hours" is not a code from the fixed set']


def test_an_unknown_age_band_is_refused_and_a_missing_one_is_not():
    assert refused({**CLEAN, "age_band": "adult"}) == ['age_band "adult" is not an ESI age band']
    assert decide({k: v for k, v in CLEAN.items() if k != "age_band"})["allow"]


@pytest.mark.parametrize("vitals,reason", [
    ({"hr": "104"}, "vitals.hr must be a number"),
    ({"bp": "high"}, "vitals.bp must read like 120/80"),
    ({"weight_kg": 80}, "vitals.weight_kg is not a vital sign the model may see"),
])
def test_a_vital_of_the_wrong_shape_is_refused(vitals, reason):
    assert refused({**CLEAN, "vitals": vitals}) == [reason]


def test_a_null_vital_is_an_absent_reading_not_a_refusal():
    assert decide({**CLEAN, "vitals": {"hr": None, "bp": None}})["allow"]


@pytest.mark.parametrize("history,reason", [
    ({"prior_visits": [{"date": "2025-11-02", "acuity": 3, "notes": "call 0521234567"}]},
     "history.prior_visits[0].notes is not part of a visit the model may see"),
    ({"prior_visits": [{"date": "November", "acuity": 3}]},
     "history.prior_visits[0].date is not an ISO date"),
    ({"prior_visits": [{"date": "2025-11-02", "acuity": 7}]},
     "history.prior_visits[0].acuity is not an ESI level"),
    ({"known_conditions": ["Patient reports chest pressure worse on exertion, please call the ward"]},
     "history.known_conditions[0] is not a short label"),
    ({"allergies": []}, "history.allergies is not part of the history the model may see"),
])
def test_history_is_dates_levels_and_labels_only(history, reason):
    assert refused({**CLEAN, "history": history}) == [reason]


def test_no_case_id_means_no_allow():
    decision = decide({k: v for k, v in CLEAN.items() if k != "case_id"})
    assert decision["allow"] is False


# ---- the verifier over the policy ------------------------------------------------


def test_the_verifier_refuses_with_the_policys_reason():
    ok, why = verify_no_identifiers({**CLEAN, "free_text": "prose"})
    assert not ok
    assert 'field "free_text" is not approved' in why


def test_the_verifier_still_scans_inside_allowed_values():
    """What Rego cannot do: the patterns need look-arounds RE2 lacks, so the
    regex scan stays, and it runs after the policy."""
    ok, why = verify_no_identifiers({**CLEAN, "history": {"known_conditions": ["id 123456789"], "prior_visits": []}})
    assert not ok
    assert "national_id in history.known_conditions[0]" in why


def test_a_missing_engine_is_a_refusal(monkeypatch):
    monkeypatch.delenv("OPA_URL", raising=False)      # no sidecar either: nothing can answer
    monkeypatch.setenv("OPA_BIN", "/nonexistent/opa")
    ok, why = verify_no_identifiers(CLEAN)
    assert not ok
    assert "engine_unavailable:opa" in why


# ---- one vocabulary, not two --------------------------------------------------------


def _rego_set(name: str) -> set[str]:
    completed = subprocess.run(
        [os.environ.get("OPA_BIN", "opa"), "eval", "-d", str(opa.PRIVACY_POLICY), "--format=raw",
         f"data.triage.privacy.{name}"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    return set(json.loads(completed.stdout))


def test_the_complaint_codes_match_python():
    """`chief_complaints` in privacy.rego and `CHIEF_COMPLAINTS` in guards/fields.py
    restate one set. This is the drift check the sync comments point at."""
    assert _rego_set("chief_complaints") == set(CHIEF_COMPLAINTS)


def test_the_age_bands_match_python():
    assert _rego_set("age_bands") == set(esi.AGE_BANDS)
