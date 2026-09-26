"""Input Normalizer + PII filter — builds the one payload the model may see.

An allow-list, built from the fields the privacy policy names (I12), not the
old subtraction of six banned keys from whatever arrived. The nurse's prose
(`free_text`), her proposed level (`nurse_proposed_acuity`) and the routing
metadata never get in: the first is not a closed value, the second would anchor
the model on the answer it is supposed to cross-check (I4), the third says
nothing clinical. All of it stays on the case record for humans.

Then the values that remain are scanned for identifiers typed inside them and
redacted. `app/symbolic/policy/privacy.rego` checks the result before it is
stored; this file constructs, that file decides.
"""

from __future__ import annotations

from typing import Any

from app.guards.fields import is_esi_level
from app.guards.identifiers import redact_identifiers

# The model's view of a prior visit: when, and how acute. The notes are prose.
VISIT_FIELDS = ("date", "acuity")


def model_history(history: dict[str, Any] | None) -> dict[str, Any] | None:
    """The CRM history reduced to what the policy allows: condition labels, and
    prior visits as (date, acuity). Name and date of birth were already stripped
    at identity resolution; this drops the prose."""
    if not history:
        return None
    # Only visits with a date and an ESI level: a malformed row in the CRM must
    # not halt every later case for the patient at the privacy policy. The row
    # stays in the CRM for humans; it just is not part of the model's view.
    visits = [{k: v.get(k) for k in VISIT_FIELDS} for v in history.get("prior_visits") or []
              if isinstance(v, dict) and v.get("date") and is_esi_level(v.get("acuity"))]
    return {
        "known_conditions": [str(c).lower() for c in history.get("known_conditions") or []],
        "prior_visits": visits,
    }


def build_model_payload(
    case_id: str,
    parsed_fields: dict[str, Any],
    history: dict[str, Any] | None,
    age_band: str | None = None,
) -> dict[str, Any]:
    """Identifier-free, case_id-keyed payload of closed values only (I11, I12)."""
    payload: dict[str, Any] = {
        "case_id": case_id,
        "chief_complaint": parsed_fields.get("chief_complaint"),
        "vitals": parsed_fields.get("vitals"),
    }
    if age_band:
        payload["age_band"] = age_band
    reduced = model_history(history)
    if reduced:
        payload["history"] = reduced
    return redact_identifiers(payload)
