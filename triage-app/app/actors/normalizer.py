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

import re
from typing import Any

from app.guards.fields import CHIEF_COMPLAINTS, is_esi_level, vitals_usable
from app.guards.identifiers import redact_identifiers

# The model's view of a prior visit: when, how acute, and that visit's complaint
# code and vitals, named as in the current payload. The notes are prose. Kept in
# sync by hand with `visit_fields` in app/symbolic/policy/privacy.rego;
# tests/symbolic/test_opa_privacy.py catches drift.
VISIT_FIELDS = ("date", "acuity", "chief_complaint", "vitals")

# A condition label is short and plain: a name, not a paragraph. Kept in sync by
# hand with `label_pattern` in app/symbolic/policy/privacy.rego;
# tests/symbolic/test_opa_privacy.py catches drift.
LABEL_PATTERN = r"^[a-z0-9][a-z0-9 ()/-]{0,39}$"


def _model_visit(visit: dict[str, Any]) -> dict[str, Any]:
    """One prior visit as the model may see it. The complaint and the vitals go
    in only if they pass the checks this visit's own would (`unusable_fields`);
    a visit recorded before either was written, or with a bad value, keeps its
    date and level without them."""
    seen = {"date": visit["date"], "acuity": visit["acuity"]}
    if visit.get("chief_complaint") in CHIEF_COMPLAINTS:
        seen["chief_complaint"] = visit["chief_complaint"]
    vitals = visit.get("vitals")
    if vitals and vitals_usable(vitals):
        seen["vitals"] = vitals
    return seen


def model_history(history: dict[str, Any] | None) -> dict[str, Any] | None:
    """The CRM history reduced to what the policy allows: short condition labels,
    and prior visits as date, acuity, complaint code and vitals. Name and date of
    birth were already stripped at identity resolution; this drops the prose."""
    if not history:
        return None
    # Only visits with a date and an ESI level, and of those only the fields that
    # pass: a malformed row in the CRM must not halt every later case for the
    # patient at the privacy policy. The row stays in the CRM for humans; it just
    # is not part of the model's view.
    visits = [_model_visit(v) for v in history.get("prior_visits") or []
              if isinstance(v, dict) and v.get("date") and is_esi_level(v.get("acuity"))]
    # The same for condition labels: one the policy would refuse ("type 2
    # diabetes, on metformin") is left out of the model's view, not sent to halt
    # the case. `fullmatch`: Python's `$` also matches before a trailing newline.
    conditions = [c.lower() for c in history.get("known_conditions") or []
                  if isinstance(c, str) and re.fullmatch(LABEL_PATTERN, c.lower())]
    return {
        "known_conditions": conditions,
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
