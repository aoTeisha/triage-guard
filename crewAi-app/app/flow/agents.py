"""Agent / actor invocations, one function per actor in the spec's table.

This is where the neuro-symbolic split is made concrete. The Actors / Agents
table in docs/SPECIFICATION.md marks each participant as deterministic or LLM;
this module honours that:

  LLM (the ONLY generative model in the decision path)
    - invoke_acuity_classifier  → runs a deterministic red-flag pre-check
                                  FIRST, then the LLM. Mock LLM for now.

  Deterministic (never an LLM — enforced here by using plain code)
    - invoke_intake_parser      → schema/field-presence validation (guards/)
    - invoke_safety_validation  → Prolog/Datalog/Z3/OPA verdict (mock pass)
    - build_model_payload       → schema-drop of identifiers + BERT urgency
    - fetch_patient_data        → CRM SQLite stub (crm_client)
    - Output Verification       → deterministic.verify_output
    - Waiting Room Monitor      → timer (not modelled in this happy-path slice)
    - Audit                     → deterministic.emit_event_log

Humans (Triage/Charge Nurse, Technician) and the Human Escalation bridge are
represented by mock inputs; wire a real UI/queue behind invoke_human_escalation.

Every function here returns MOCK data you can edit. The one place to replace
with a real crewAI Agent/Crew call is invoke_acuity_classifier — load the
JSON-first crew from app/agents/acuity_classifier.jsonc via crewai.project
.load_crew, exactly as the old app/main.py did.
"""

from __future__ import annotations

import re
from typing import Any

from app.crm_client import fetch_patient
from app.flow.state import TriageState, UrgencyScores

# ---- Acuity Classifier: the single LLM, with a deterministic pre-check ------

# Red-flag pre-check (SPECIFICATION.md § Neuro-Symbolic Architecture). This is
# DETERMINISTIC by design — a hard clinical trigger forces emergent regardless
# of what the LLM would say. Advisory (overridable at the gate), logged as
# acuity_source = rule_forced.
_RED_FLAG_PATTERNS = (
    re.compile(r"\bchest (pain|tightness|pressure)\b", re.I),
    re.compile(r"\b(stroke|fainting|unconscious|not breathing|seizure)\b", re.I),
    re.compile(r"\bspo2\b.*\b([0-8]\d|9[0-1])\b", re.I),   # SpO2 <= 91
)


def _red_flag(payload: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(payload.get(k, "")) for k in ("chief_complaint", "free_text", "vitals")
    )
    return any(p.search(haystack) for p in _RED_FLAG_PATTERNS)


def invoke_acuity_classifier(redacted_payload: dict[str, Any]) -> dict[str, Any]:
    """Propose {system_proposed_acuity, confidence, acuity_source}.

    Step 1 (deterministic): red-flag pre-check. A match returns emergent (2)
    with acuity_source = rule_forced, and the LLM is not consulted for the level.
    Step 2 (LLM): MOCK. Replace this branch with a real crew.kickoff() call.
    """
    if _red_flag(redacted_payload):
        return {
            "system_proposed_acuity": 2,
            "confidence": 0.99,
            "acuity_source": "rule_forced",
            "rationale": "Deterministic red-flag matched; forced to emergent.",
        }

    # ---- MOCK LLM OUTPUT — edit freely, or swap for a real crew call --------
    return {
        "system_proposed_acuity": 2,
        "confidence": 0.81,
        "acuity_source": "system",
        "rationale": "MOCK: elevated HR/BP with chest complaint suggests emergent band.",
    }


# ---- Intake Parser: DETERMINISTIC schema validation ------------------------

def invoke_intake_parser(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """MOCK ParseResult. The real deterministic classification already lives in
    flow/steps.py via guards.classify_intake — this returns a canned success so
    the skeleton runs clean. Replace with the guard-driven result once you want
    the four-branch behaviour under the Flow.
    """
    return {
        "outcome": "DATA_PARSED",
        "parsed_fields": dict(raw_payload),
        "missing_fields": [],
        "reason": None,
    }


# ---- Input Normalizer + PII schema-drop + BERT: DETERMINISTIC drop ----------

_IDENTIFIER_KEYS = ("name", "stable_patient_id", "date_of_birth", "dob", "phone")


def build_model_payload(
    parsed_fields: dict[str, Any], history: dict[str, Any] | None
) -> tuple[dict[str, Any], UrgencyScores]:
    """Drop identifiers, key by case_id, merge history, score urgency.

    The identifier drop is deterministic schema-drop (critical, fail-closed).
    Urgency is a MOCK score standing in for the BERT scorer.
    """
    payload = {k: v for k, v in parsed_fields.items() if k not in _IDENTIFIER_KEYS}
    if history:
        payload["history"] = {
            k: v for k, v in history.items() if k not in _IDENTIFIER_KEYS
        }
    # MOCK urgency scores — replace with the BERT/NER scorer output.
    scores = UrgencyScores(sentiment=0.2, distress=0.4, pain=0.5)
    return payload, scores


# ---- CRM / Patient DB: DETERMINISTIC data store (SQLite stub) ---------------

def fetch_patient_data(stable_patient_id: str) -> dict[str, Any]:
    """Thin wrapper over the CRM stub client. Falls back to a MOCK not_found
    when the stub isn't running, so the skeleton never hangs on the network.
    """
    try:
        result = fetch_patient(stable_patient_id, timeout=1.0)
        return {"status": result.status, "record": result.record}
    except Exception:  # stub not up in the skeleton — treat as db_error/degrade
        return {"status": "db_error", "record": None}


# ---- Safety Validation: DETERMINISTIC verdict (Prolog/Datalog/Z3/OPA) -------

def invoke_safety_validation(state: TriageState) -> dict[str, Any]:
    """MOCK verdict (always pass). Real deployment: the symbolic engines.
    Edit the return to exercise the fail → human-gate branch (10·fail).
    """
    return {
        "verdict": "pass",
        "reasons": ["MOCK: acuity within valid ESI band", "no identifiers in payload"],
    }


# ---- Human Escalation bridge: mock human response ---------------------------

def invoke_human_escalation(state: TriageState, reason: str) -> dict[str, Any]:
    """MOCK charge-nurse response. Wire a real UI/queue here.

    For an acuity discrepancy it returns a resolution choice; for a safety fail
    it returns a correction. Both require a charge role in the real gate.
    """
    if reason == "discrepancy":
        return {"decision": "use_system_acuity", "resolver_role": "charge_nurse"}
    return {"decision": "corrected", "resolver_role": "charge_nurse"}
