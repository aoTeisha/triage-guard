"""A past visit's complaint and vitals, from the write-back that records them to
the model that reads them as history (I12, I17).

A release writes the visit's complaint code and vitals under the names the
current payload uses; a later case for the same patient sends them on in
`history.prior_visits`. A value that would not pass as this visit's own is
left out of the model's view, never sent to halt the case at the privacy
policy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from langgraph.types import Command

from app import crm_client
from app.actors import acuity_classifier
from app.actors.normalizer import build_model_payload, model_history
from app.deterministic import verify_no_identifiers
from app.graph.nodes import identity
from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.states import State
from tests.test_release_everywhere import RELEASE

VISIT = {"date": "2026-01-15", "acuity": 2, "notes": "shortness of breath",
         "chief_complaint": "shortness_of_breath",
         "vitals": {"hr": 112, "rr": 26, "bp": "152/94", "spo2": 91, "temp_c": 37.4}}


def _history(*visits):
    return {"known_conditions": ["hypertension"], "prior_visits": list(visits)}


# ---- the model's view -------------------------------------------------------------


def test_a_visits_complaint_and_vitals_reach_the_model_without_its_notes():
    payload = build_model_payload("c1", DEMO_CASES["clean"], _history(VISIT), "over_18_years")

    assert payload["history"]["prior_visits"] == [
        {"date": "2026-01-15", "acuity": 2, "chief_complaint": "shortness_of_breath",
         "vitals": VISIT["vitals"]}]
    assert verify_no_identifiers(payload)[0]


def test_a_visit_from_before_the_new_fields_keeps_its_date_and_level():
    old = {"date": "2025-11-02", "acuity": 3, "notes": "chest tightness, discharged stable"}
    assert model_history(_history(old))["prior_visits"] == [{"date": "2025-11-02", "acuity": 3}]


@pytest.mark.parametrize("bad", [
    {"chief_complaint": "chest tightness for 2 hours"},      # prose, not a code
    {"chief_complaint": None, "vitals": None},                # written with no payload
    {"chief_complaint": 3},
    {"vitals": {"hr": "112"}},                                # a string, not a number
    {"vitals": {"bp": "high"}},
    {"vitals": {"weight_kg": 80}},                            # not a sign the model may see
    {"vitals": {"hr": 5210000}},                              # no patient has this heart rate
    {"vitals": {"bp": "80/120"}},                             # diastolic above systolic
    {"vitals": "hr 112, spo2 91"},
    {"vitals": {}},
])
def test_a_bad_complaint_or_vitals_is_left_out_not_sent_to_halt(bad):
    """The checks this visit's own fields get at intake (`unusable_fields`). The
    rest of the visit still goes, and the payload still passes the policy."""
    visit = {"date": "2026-01-15", "acuity": 2} | bad
    payload = build_model_payload("c1", DEMO_CASES["clean"], _history(visit))

    sent = payload["history"]["prior_visits"]
    assert sent == [{"date": "2026-01-15", "acuity": 2}]
    assert verify_no_identifiers(payload)[0]


def test_a_bad_vitals_field_does_not_take_a_good_complaint_with_it():
    visit = VISIT | {"vitals": {"hr": "fast"}}
    assert model_history(_history(visit))["prior_visits"] == [
        {"date": "2026-01-15", "acuity": 2, "chief_complaint": "shortness_of_breath"}]


def _seeded_visits() -> list[dict]:
    """Every prior visit the CRM stub seeds, read from the literal in its seed
    file — the crm-stub package is its own project, not importable here."""
    seed = Path(__file__).resolve().parents[2] / "crm-stub" / "crm" / "seed.py"
    for node in ast.parse(seed.read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "PATIENTS":
            return [v for row in ast.literal_eval(node.value) for v in row[5]]
    raise AssertionError("PATIENTS not found in crm-stub/crm/seed.py")


def test_every_seeded_complaint_and_vitals_reach_the_model():
    """The seed's examples are what the demo shows: none may be dropped."""
    visits = _seeded_visits()
    recorded = [v for v in visits if "chief_complaint" in v or "vitals" in v]
    assert recorded, "the seed should show a visit with its complaint and vitals"

    sent = build_model_payload("c1", DEMO_CASES["clean"], _history(*visits))["history"]["prior_visits"]
    assert [v for v in sent if "chief_complaint" in v or "vitals" in v] == [
        {k: v[k] for k in ("date", "acuity", "chief_complaint", "vitals")} for v in recorded]
    assert all("notes" not in v for v in sent)


# ---- end to end: one visit's write-back is the next case's history ----------------------


def _patient_with(history, monkeypatch):
    """The CRM's answer for the clean case's patient, with this history."""
    record = {"stable_patient_id": "P-1001", "name": "Alon Mizrahi", "date_of_birth": "1958-03-12",
              "national_id": "300000001", **history}
    lookup = lambda *a, **k: crm_client.PatientLookupResult(status="found", record=record)  # noqa: E731
    monkeypatch.setattr(identity, "fetch_patient_by_national_id", lookup)
    monkeypatch.setattr(identity, "fetch_patient", lookup)


def _classifier_sees(monkeypatch) -> list[dict]:
    seen: list[dict] = []
    real = acuity_classifier.classify
    monkeypatch.setattr(acuity_classifier, "classify", lambda payload: seen.append(payload) or real(payload))
    return seen


def test_a_released_visit_reaches_the_next_cases_model_as_history(graph, run, monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(crm_client, "patch_patient",
                        lambda pid, visit, **k: written.append(visit["new_visit"]) or "ok")

    # First visit: triaged and released, which writes it back.
    _patient_with(_history(), monkeypatch)
    _, _, first = run(DEMO_CASES["clean"])
    graph.invoke(Command(resume=RELEASE), config_for(first))
    assert len(written) == 1
    visit = written[0]
    assert visit["chief_complaint"] == DEMO_CASES["clean"]["chief_complaint"]
    assert visit["vitals"] == DEMO_CASES["clean"]["vitals"]

    # Second visit: the CRM now holds the first one, plus an old row from before
    # complaint and vitals were written, and a row a bad import left.
    old = {"date": "2025-11-02", "acuity": 3, "notes": "chest tightness, discharged stable"}
    broken = {"date": "2025-12-01", "acuity": 4, "notes": "x", "chief_complaint": "see notes",
              "vitals": {"hr": "?"}}
    _patient_with(_history(old, broken, visit), monkeypatch)
    seen = _classifier_sees(monkeypatch)
    state, _, _ = run(DEMO_CASES["clean"] | {"case_id": "case-0001-b"})

    assert state["control_state"] != State.AGENT_FAILED.value
    assert not state["payload_unverified"]
    expected = [{"date": "2025-11-02", "acuity": 3},
                {"date": "2025-12-01", "acuity": 4},
                {"date": visit["date"], "acuity": visit["acuity"],
                 "chief_complaint": visit["chief_complaint"], "vitals": visit["vitals"]}]
    assert state["redacted_payload"]["history"]["prior_visits"] == expected
    assert seen and seen[0]["history"]["prior_visits"] == expected
