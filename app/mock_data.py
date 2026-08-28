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

# Keyed by task name in crew.jsonc.
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
