"""Input Normalizer + PII filter + urgency scorer (docs/actors/input_normalizer.jsonc).

Two components with different criticality, per § Per-agent failure model:

  schema-drop   deterministic identifier removal. CRITICAL, fail-closed, retry N=0.
                A fault here halts the line rather than risk a leak.
  BERT/NER      probabilistic urgency scoring over free text. Non-critical,
                fail-open: on failure drop the free-text fields and continue, so
                no unredacted prose reaches the model and the privacy invariant
                still holds.

Only the scorer is mocked. The identifier drop below is the real rule.
"""

from __future__ import annotations

from typing import Any

from app.actors import load_mock
from app.graph.state import UrgencyScores

# Structured identifier fields. These never enter the model-facing payload; they
# stay on the case record, where the nurse and the graph can still read them.
IDENTIFIER_KEYS = frozenset(
    {"name", "stable_patient_id", "date_of_birth", "dob", "phone"}
)

# The "patient's own words" fields the BERT/NER scorer guards. When the scorer is
# unavailable these are dropped wholesale rather than passed through unscored.
FREE_TEXT_KEYS = frozenset({"free_text"})


class ScorerUnavailable(RuntimeError):
    """Raised when the urgency scorer cannot be reached (drives AF·pii_bert_ner)."""


def drop_identifiers(fields: dict[str, Any]) -> dict[str, Any]:
    """Deterministic schema-drop. Critical path — allowlist by exclusion, and never
    an inference: a key is dropped because it is named, not because a model thought
    it looked like a name.
    """
    return {k: v for k, v in fields.items() if k not in IDENTIFIER_KEYS}


def build_model_payload(
    case_id: str,
    parsed_fields: dict[str, Any],
    history: dict[str, Any] | None,
    *,
    drop_free_text: bool = False,
) -> dict[str, Any]:
    """Identifier-free, case_id-keyed payload, with history merged in.

    `drop_free_text` is the BERT/NER degrade path, not an ordinary option: when the
    scorer is down the free-text fields leave with it.
    """
    payload = drop_identifiers(parsed_fields)
    if drop_free_text:
        payload = {k: v for k, v in payload.items() if k not in FREE_TEXT_KEYS}
    if history:
        payload["history"] = drop_identifiers(history)
    payload["case_id"] = case_id
    return payload


def score_urgency(payload: dict[str, Any]) -> UrgencyScores:
    """MOCK stand-in for the BERT distress/pain scorer.

    Edit `mocks/input_normalizer.json` to change the scores; replace this function
    body to wire the real scorer. Raise `ScorerUnavailable` to exercise the degrade
    path.
    """
    return UrgencyScores(**load_mock("input_normalizer"))
