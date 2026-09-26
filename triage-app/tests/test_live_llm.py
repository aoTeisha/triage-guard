"""The one test that actually calls OpenRouter. Excluded from the fast suite.

Run: uv run pytest tests/test_live_llm.py -m integration -v
Needs OPENROUTER_API_KEY and MODEL in .env (see .env.example).
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from app.actors import acuity_classifier
from app.schemas import AcuityProposal

# The shape the classifier really sees: what policy/privacy.rego allows and
# nothing else — a complaint code, numbers, an age band, history as labels and
# (date, acuity). No prose, no identifier, and not the nurse's own level.
CASE = {
    "case_id": "case-live-0001",
    "chief_complaint": "chest_pain",
    "vitals": {"hr": 104, "bp": "148/92", "spo2": 95, "temp_c": 37.1},
    "age_band": "over_18_years",
    "history": {"known_conditions": ["hypertension", "type 2 diabetes"],
                "prior_visits": [{"date": "2026-01-15", "acuity": 2}]},
}


@pytest.mark.integration
def test_classify_reaches_a_real_model(monkeypatch):
    load_dotenv()
    if not os.environ.get("OPENROUTER_API_KEY", "").startswith("sk-or-"):
        pytest.skip("no OPENROUTER_API_KEY in .env")
    monkeypatch.setenv("TRIAGE_LLM", "live")
    acuity_classifier._model.cache_clear()

    proposal = acuity_classifier.classify(CASE)

    assert isinstance(proposal, AcuityProposal)
    assert proposal.acuity_source == "system"
    assert proposal.rationale.strip()
    # Structured output already bounds the ranges; this asserts the model read the
    # case rather than defaulting — exertional chest pressure is not a 4 or a 5.
    assert proposal.system_proposed_acuity <= 3
