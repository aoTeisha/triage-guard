"""The classifier's rationale: kept on the case, redacted, shown on the case
view for the gate, and never carried from one triage into the next.
"""

from __future__ import annotations

from langgraph.types import Command

from app import observability as obs
from app.actors import acuity_classifier
from app.graph import TriageState
from app.mock_cases import DEMO_CASES
from app.runner import config_for, hydrate
from app.schemas import AcuityProposal
from app.views import case_view

REFILE = {
    "nurse_proposed_acuity": 1,
    "chief_complaint": "chest_pain",
    "vitals": {"hr": 140, "bp": "90/60", "spo2": 88, "temp_c": 38.2},
}


def _proposing(monkeypatch, rationale: str, confidence: float = 0.73) -> None:
    monkeypatch.setattr(
        acuity_classifier, "classify",
        lambda payload: AcuityProposal(
            system_proposed_acuity=2, confidence=confidence, acuity_source="system",
            rationale=rationale,
        ),
    )


def _fire_the_timer(graph, thread: str) -> None:
    graph.invoke(Command(resume={"event": "REASSESSMENT_TIMEOUT", "fire_id": "f1"}),
                 config_for(thread))


def test_the_rationale_is_kept_after_classifying(run, monkeypatch):
    _proposing(monkeypatch, "Chest pressure with tachycardia.")

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["classifier_rationale"] == "Chest pressure with tachycardia."
    assert state["confidence"] == 0.73


def test_identifiers_in_the_rationale_are_redacted_before_it_is_stored(run, monkeypatch):
    _proposing(monkeypatch, "Patient 300000011 (call 052-123-4567) reports chest pain.")

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["classifier_rationale"] == (
        "Patient [REDACTED_ID] (call [REDACTED_PHONE]) reports chest pain.")


def test_a_classifier_fallback_keeps_no_rationale(run, monkeypatch):
    def unusable(payload):
        raise RuntimeError("classifier unreachable")

    monkeypatch.setattr(acuity_classifier, "classify", unusable)

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["system_proposed_acuity"] is None
    assert state["classifier_rationale"] is None


def test_a_refile_replaces_the_last_triages_rationale(graph, run, monkeypatch):
    _proposing(monkeypatch, "first triage's reason")
    _, _, thread = run(DEMO_CASES["clean"])
    _fire_the_timer(graph, thread)

    _proposing(monkeypatch, "second triage's reason")
    result = hydrate(graph.invoke(Command(resume=REFILE), config_for(thread)))

    assert result["classifier_rationale"] == "second triage's reason"


def test_a_refile_that_falls_back_shows_no_old_rationale(graph, run, monkeypatch):
    _proposing(monkeypatch, "first triage's reason")
    _, _, thread = run(DEMO_CASES["clean"])
    _fire_the_timer(graph, thread)

    def unusable(payload):
        raise RuntimeError("classifier unreachable")

    monkeypatch.setattr(acuity_classifier, "classify", unusable)
    graph.invoke(Command(resume=REFILE), config_for(thread))

    result = hydrate(graph.get_state(config_for(thread)).values)
    assert result["classifier_rationale"] is None
    assert "first triage's reason" not in repr(case_view(result, None))


def test_the_case_view_carries_the_rationale_and_confidence():
    view = case_view({"case_id": "c1", "confidence": 0.73,
                      "classifier_rationale": "Chest pressure with tachycardia."}, None)

    assert view["confidence"] == 0.73
    assert view["classifier_rationale"] == "Chest pressure with tachycardia."


def test_the_langfuse_mask_covers_the_rationale():
    """Node spans record the whole state, rationale included. It is redacted
    before it is stored; the mask is the second net if anything slipped in.
    """
    state = TriageState(case_id="c1", classifier_rationale="call 0521234567 or 300000011")

    masked = repr(obs._mask(data={"output": state}))

    assert "0521234567" not in masked and "300000011" not in masked
    assert "[REDACTED_PHONE]" in masked
