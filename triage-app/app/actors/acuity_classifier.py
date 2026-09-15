"""Acuity Classifier - the ONLY generative model in the decision path.

docs/actors/... lists twelve actors; eleven are deterministic code, humans, or data
stores. This is the one exception, and the neuro-symbolic split is enforced
structurally: this is the only module in the package that may import an LLM client.

The model works the ESI v5 algorithm. Decision points A (life-saving intervention),
B (high-risk / altered mental status / severe distress) and C (resource count) all
need clinical reading, and ESI defines B by judgment with worked examples rather than
a closed list - so all three are the model's, grounded by the criteria in the persona
file. Decision point D (danger-zone vitals) is deterministic and is added here too,
as an annotation that never changes the level (see `app/esi.py`, not yet built).

It proposes. It never settles the acuity: the graph resolves this against the
nurse's proposal using the fixed 0 / 1 / 2+ bands.

The hand-written red-flag regex this file used to carry was retired on 2026-09-13 -
it matched "chest tightness" but not "pressure in the chest", and recovered SpO2 by
string-matching a stringified dict. See
docs/plans/2026-09-13-acuity-classifier-design.md.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv

from app.actors import load_mock, load_persona
from app.schemas import AcuityProposal


def live_mode() -> bool:
    """`TRIAGE_LLM=live` calls a real model. Default is mock, so the skeleton and
    the whole fast test suite run offline with no key.

    load_dotenv() here, not only in main.kickoff: the board and the tests import
    classify() without going through the CLI entrypoint.
    """
    load_dotenv()
    return os.environ.get("TRIAGE_LLM", "mock").strip().lower() == "live"


@lru_cache(maxsize=1)
def _model():
    """Structured-output LLM. Built lazily so mock mode never needs a key.

    OpenRouter is OpenAI-compatible, hence ChatOpenAI with a base_url. The model id
    comes from the environment and nowhere else — the persona file deliberately
    holds no model name, so there is one place to change it.
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=os.environ["MODEL"],
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=os.environ["OPENROUTER_API_KEY"],
        temperature=0,
    ).with_structured_output(AcuityProposal)


def _prompt(payload: dict[str, Any]) -> list[tuple[str, str]]:
    persona = load_persona("acuity_classifier")
    system = f"{persona['role']}\n\nGoal: {persona['goal']}\n\n{persona['backstory']}"
    return [("system", system), ("human", f"Redacted case payload:\n{payload}")]


def classify(payload: dict[str, Any]) -> AcuityProposal:
    """Propose an acuity for one redacted payload.

    Raises on transport failure so the graph's RetryPolicy can see it - swallowing the
    exception here would make the retry budget unreachable.
    """
    if not live_mode():
        # MOCK - edit mocks/acuity_classifier.json, or set TRIAGE_LLM=live.
        return AcuityProposal.model_validate(load_mock("acuity_classifier"))

    return _model().invoke(_prompt(payload))
