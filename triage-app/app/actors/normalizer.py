"""Input Normalizer + PII filter (docs/actors/input_normalizer.jsonc).

Deterministic identifier removal. CRITICAL, fail-closed, retry N=0 — a fault here
halts the line rather than risk a leak.

The BERT/NER urgency scorer that used to live here was removed on 2026-09-13. It
produced three scores (sentiment, distress, pain) that nothing downstream ever read,
and intake carries no free text for it to score. See
docs/plans/2026-09-13-acuity-classifier-design.md.
"""

from __future__ import annotations

from typing import Any

from app.guards.identifiers import redact_identifiers

# Structured identifier fields. These never enter the model-facing payload; they
# stay on the case record, where the nurse and the graph can still read them.
IDENTIFIER_KEYS = frozenset(
    {"name", "stable_patient_id", "date_of_birth", "dob", "phone"}
)


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
) -> dict[str, Any]:
    """Identifier-free, case_id-keyed payload, with history merged in.

    Two passes: identifier *fields* are dropped by name, then identifiers typed
    inside the remaining text are redacted. Only this model-facing copy is
    redacted; the case record keeps what the nurse wrote.
    """
    payload = redact_identifiers(drop_identifiers(parsed_fields))
    if history:
        payload["history"] = redact_identifiers(drop_identifiers(history))
    payload["case_id"] = case_id
    return payload
