"""Actors: one module per participant in docs/SPECIFICATION.md § Actors / Agents.

Each module exposes plain functions that take values and return proposals. Actors
**propose**; they never write state. The graph node calls the actor, verifies the
proposal, and writes the result — that separation is what keeps the write path
auditable and the actors unit-testable without a graph.

Eleven of the twelve actors are deterministic code, humans, or data stores. Exactly
one is generative: `acuity_classifier`. The spec card for each actor lives in
`docs/actors/<actor>.jsonc`; only the classifier has a persona file here
(`acuity_classifier.json`), because only it is fed to a model.

Canned outputs live in `mocks/<actor>.json` — edit the JSON to change what a mocked
actor returns, never the Python.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_MOCKS = _HERE / "mocks"


@lru_cache(maxsize=None)
def load_mock(actor: str) -> dict[str, Any]:
    """Canned output for a mocked actor. Cached: these files do not change at runtime."""
    return json.loads((_MOCKS / f"{actor}.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def load_persona(actor: str) -> dict[str, Any]:
    """role / goal / backstory for the one actor that is a real LLM.

    Plain `.json`, not `.jsonc`: `json.loads` rejects `//` comments, and a persona
    file that only parses through a custom stripper is a trap for the next reader.
    """
    return json.loads((_HERE / f"{actor}.json").read_text(encoding="utf-8"))
