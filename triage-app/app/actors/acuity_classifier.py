"""Acuity Classifier — the ONLY generative model in the decision path.

docs/actors/… lists twelve actors; eleven are deterministic code, humans, or data
stores. This is the one exception, and the neuro-symbolic split is enforced
structurally: this is the only module in the package that may import an LLM client.

Two steps, in this order and never the other way round:

  1. Red-flag pre-check — DETERMINISTIC. A hard clinical trigger forces emergent
     regardless of what a model would say, with acuity_source = rule_forced. Runs
     in mock mode and live mode alike; a rule that only fires when the model is
     switched on is not a safety rule.
  2. The model — proposes an ESI level and a confidence.

It proposes. It never settles the acuity: the graph resolves this against the
nurse's proposal using the fixed 0 / 1 / 2+ bands.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

from app.actors import load_mock, load_persona
from app.schemas import AcuityProposal

# Red-flag patterns (§ Neuro-Symbolic Architecture). Deterministic and advisory:
# a match forces emergent, which the human gate can still override.
_RED_FLAGS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bchest (pain|tightness|pressure)\b", re.I),
    re.compile(r"\b(stroke|fainting|unconscious|not breathing|seizure)\b", re.I),
    re.compile(r"\bspo2\b\D{0,12}([0-8]\d|9[0-1])\b", re.I),   # SpO2 <= 91
)

_SCANNED_FIELDS = ("chief_complaint", "free_text", "vitals")


def red_flag(payload: dict[str, Any]) -> bool:
    """True when a hard clinical trigger is present in the redacted payload."""
    haystack = " ".join(str(payload.get(k, "")) for k in _SCANNED_FIELDS)
    return any(p.search(haystack) for p in _RED_FLAGS)


def _forced_emergent() -> AcuityProposal:
    return AcuityProposal(
        system_proposed_acuity=2,
        confidence=0.99,
        acuity_source="rule_forced",
        rationale="Deterministic red-flag matched; forced to emergent without consulting the model.",
    )


def live_mode() -> bool:
    """`TRIAGE_LLM=live` calls a real model. Default is mock, so the skeleton and
    the whole fast test suite run offline with no key.
    """
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

    Raises on transport failure so the graph's RetryPolicy can see it — swallowing
    the exception here would make the retry budget unreachable.
    """
    if red_flag(payload):
        return _forced_emergent()

    if not live_mode():
        # MOCK — edit mocks/acuity_classifier.json, or set TRIAGE_LLM=live.
        return AcuityProposal.model_validate(load_mock("acuity_classifier"))

    return _model().invoke(_prompt(payload))
