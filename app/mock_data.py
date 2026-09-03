"""Mock intake case and canned agent outputs.

Demo case 1 ("clean submission") from the Document intake & demo data table in
docs/SPECIFICATION.md. No LLM is called; these are the outputs the agents would
produce, used to exercise the wiring and the Langfuse trace shape.
"""

DEMO_CASE_1 = {
    "case_id": "case-0001",
    "channel": "website",
    "stable_patient_id": "300000001",
    "nurse_proposed_acuity": 3,
    "chief_complaint": "chest tightness for 2 hours",
    "vitals": {"hr": 104, "bp": "148/92", "spo2": 95, "temp_c": 37.1},
    "free_text": "Patient reports pressure in the chest, worse on exertion.",
}

# Canned parse_intake outcomes, keyed by the intake-channel's submission_type
# (clean / missing / failed / injection — the four demo cases in
# docs/SPECIFICATION.md § Document intake & demo data). Consumed by
# intake-channel's POST /submit; parsed_fields is left empty (rather than
# echoing the built payload) since the real submitted case is what should be
# shown back to the caller, not a canned duplicate of it.
MOCK_PARSE_RESULTS = {
    "clean": {
        "outcome": "DATA_PARSED",
        "parsed_fields": {},
        "missing_fields": [],
        "reason": None,
    },
    "missing": {
        "outcome": "MISSING_FIELDS_DETECTED",
        "parsed_fields": {},
        "missing_fields": ["nurse_proposed_acuity", "vitals"],
        "reason": None,
    },
    "failed": {
        "outcome": "SUBMISSION_FAILED",
        "parsed_fields": {},
        "missing_fields": [],
        "reason": "submission incomplete — nothing usable received",
    },
    "injection": {
        "outcome": "INVALID_INPUT_DETECTED",
        "parsed_fields": {},
        "missing_fields": [],
        "reason": "injection",
    },
}

# Keyed by task name in crew.jsonc. Used by app/main.py's own mock loop
# (unrelated to intake-channel, which uses MOCK_PARSE_RESULTS above for
# parse_intake specifically).
MOCK_OUTPUTS = {
    "parse_intake": {
        "outcome": "DATA_PARSED",
        "parsed_fields": DEMO_CASE_1,
        "missing_fields": [],
        "reason": None,
    },
    "classify_acuity": {
        "system_proposed_acuity": 2,
        "confidence": 0.81,
        "acuity_source": "system",
        "rationale": "Chest tightness with tachycardia and elevated BP warrants an emergent band.",
    },
    "validate_safety": {
        "verdict": "pass",
        "reasons": ["acuity within valid ESI band", "no identifiers present in model payload"],
    },
}
