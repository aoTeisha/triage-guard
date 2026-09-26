# What may reach the acuity classifier (I11, I12), evaluated by the real OPA
# engine over the payload `build_model_payload` produced, before it is stored.
#
# An allow-list, not a deny-list. The old check refused six named keys and let
# everything else through, so a field nobody had thought of reached the model
# unexamined. Here a field reaches the model only because this file names it,
# and its value only because it has the declared shape: numbers, dates, codes
# from a fixed vocabulary, and short labels. No prose. Anything else is a deny
# with a reason naming the path.
#
# What this cannot do: read an identifier typed *inside* an allowed value. That
# stays with the regex scan in app/guards/identifiers.py, whose patterns need
# look-arounds that RE2 (Rego's regex) does not have.
package triage.privacy

import rego.v1

default allow := false

allow if {
	count(deny_reasons) == 0
	input.payload.case_id
}

# ---- what the model may see ------------------------------------------------
approved_fields := {"case_id", "chief_complaint", "vitals", "age_band", "history"}
vital_fields := {"hr", "rr", "bp", "spo2", "temp_c"}
history_fields := {"known_conditions", "prior_visits"}
visit_fields := {"date", "acuity"}

# Kept in sync by hand with `CHIEF_COMPLAINTS` in app/guards/fields.py and
# `AGE_BANDS` in app/esi.py; tests/symbolic/test_opa_privacy.py catches drift.
chief_complaints := {
	"chest_pain", "shortness_of_breath", "abdominal_pain", "head_injury", "fever",
	"laceration", "limb_injury", "dizziness", "vomiting", "back_pain", "other",
}
age_bands := {
	"under_1_month", "1_to_12_months", "1_to_3_years", "3_to_5_years",
	"5_to_12_years", "12_to_18_years", "over_18_years",
}

# Identifier-class keys, refused at any depth. Belt to the allow-list's braces:
# an approved container (history, vitals) must not smuggle one in either.
identifier_keys := {"name", "national_id", "stable_patient_id", "date_of_birth", "dob", "phone", "email"}

# A label is a short, plain string: a condition name, not a paragraph.
label_pattern := `^[a-z0-9][a-z0-9 ()/-]{0,39}$`
date_pattern := `^[0-9]{4}-[0-9]{2}-[0-9]{2}$`
bp_pattern := `^[0-9]{2,3}/[0-9]{2,3}$`

# ---- the allow-list ---------------------------------------------------------
deny_reasons contains sprintf("field %q is not approved for the model", [f]) if {
	some f, _ in input.payload
	not f in approved_fields
}

deny_reasons contains sprintf("identifier key %q at %s", [key, concat(".", [sprintf("%v", [p]) | some p in path])]) if {
	some [path, _] in walk(input.payload)
	key := path[count(path) - 1]
	key in identifier_keys
}

# ---- closed values, field by field ------------------------------------------
deny_reasons contains sprintf("chief_complaint %q is not a code from the fixed set", [input.payload.chief_complaint]) if {
	not input.payload.chief_complaint in chief_complaints
}

deny_reasons contains sprintf("age_band %q is not an ESI age band", [input.payload.age_band]) if {
	input.payload.age_band
	not input.payload.age_band in age_bands
}

deny_reasons contains sprintf("vitals.%s is not a vital sign the model may see", [k]) if {
	some k, _ in input.payload.vitals
	not k in vital_fields
}

deny_reasons contains sprintf("vitals.%s must be a number", [k]) if {
	some k, v in input.payload.vitals
	k in {"hr", "rr", "spo2", "temp_c"}
	v != null
	not is_number(v)
}

deny_reasons contains "vitals.bp must read like 120/80" if {
	v := input.payload.vitals.bp
	v != null
	not regex.match(bp_pattern, v)
}

deny_reasons contains sprintf("history.%s is not part of the history the model may see", [k]) if {
	some k, _ in input.payload.history
	not k in history_fields
}

deny_reasons contains sprintf("history.known_conditions[%d] is not a short label", [i]) if {
	some i, c in input.payload.history.known_conditions
	not regex.match(label_pattern, c)
}

deny_reasons contains sprintf("history.prior_visits[%d].%s is not part of a visit the model may see", [i, k]) if {
	some i, visit in input.payload.history.prior_visits
	some k, _ in visit
	not k in visit_fields
}

deny_reasons contains sprintf("history.prior_visits[%d].date is not an ISO date", [i]) if {
	some i, visit in input.payload.history.prior_visits
	not regex.match(date_pattern, visit.date)
}

deny_reasons contains sprintf("history.prior_visits[%d].acuity is not an ESI level", [i]) if {
	some i, visit in input.payload.history.prior_visits
	not visit.acuity in {1, 2, 3, 4, 5}
}

decision := {"allow": allow, "deny_reasons": deny_reasons}
