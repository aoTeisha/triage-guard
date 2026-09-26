# The monitor's runtime gate, evaluated by the real OPA engine
# (app/symbolic/opa.py) immediately before a side effect happens.
# Whitelist: default deny; every deny carries a reason.
package triage.monitor

import rego.v1

default allow := false

# --- dispatch: resume a paused case with REASSESSMENT_TIMEOUT --------------
allow if {
	input.action == "dispatch"
	input.case.control_state == "monitoring"
	not input.timer.fire_state in {"UNKNOWN", "DISPATCHING"}
}

deny_reasons contains "case_not_at_monitoring_pause" if {
	input.action == "dispatch"
	input.case.control_state != "monitoring"
}

deny_reasons contains "blind_redispatch_from_unknown" if {
	input.action == "dispatch"
	input.timer.fire_state in {"UNKNOWN", "DISPATCHING"}
}

# --- notify: gate / re-filing / senior reminder -----------------------------
allow if {
	input.action == "notify"
	input.pause_active == true
	input.notify_count < input.notify_budget
}

deny_reasons contains "reminder_pause_resolved" if {
	input.action == "notify"
	input.pause_active != true
}

deny_reasons contains "notification_budget_exhausted" if {
	input.action == "notify"
	input.notify_count >= input.notify_budget
}

# --- move: waiting room -> treatment ----------------------------------------
move_roles := {"nurse", "charge_nurse", "shift_lead"}

allow if {
	input.action == "move"
	input.case.safety_passed == true
	input.case.approved == true
	input.actor_role in move_roles
}

deny_reasons contains "move refused: safety not passed" if {
	input.action == "move"
	input.case.safety_passed != true
}

deny_reasons contains "move refused: not approved" if {
	input.action == "move"
	input.case.approved != true
}

deny_reasons contains sprintf("move refused: role %q not authorized", [input.actor_role]) if {
	input.action == "move"
	not input.actor_role in move_roles
}

# --- release: sign the patient out, from any pause ---------------------------
release_reasons := {"discharge", "ama", "transfer", "admit"}

# Must be kept in sync by hand with `charge_role/1` in
# app/symbolic/rules/monitor.pl — OPA can't call into Prolog, so this is an
# independent restatement of the same role set, not a delegation. See
# `tests/symbolic/test_opa.py` (or wherever this file's cross-engine
# consistency test lives) for the check that catches drift.
charge_roles := {"charge_nurse", "shift_lead"}

allow if {
	input.action == "release"
	input.reason in release_reasons
	input.actor_role in charge_roles
}

deny_reasons contains sprintf("release refused: invalid reason %q", [input.reason]) if {
	input.action == "release"
	not input.reason in release_reasons
}

deny_reasons contains sprintf("release refused: role %q is not a charge role", [input.actor_role]) if {
	input.action == "release"
	not input.actor_role in charge_roles
}

# --- writeback: a released case's visit reaches the CRM (I17) ---------------
# Only a closed case has a visit to write: the acuity is settled and the
# release signed. Anything earlier would write a story that is still changing.
allow if {
	input.action == "writeback"
	input.case.control_state == "case_closed"
	input.case.stable_patient_id
}

deny_reasons contains "writeback refused: case is not closed" if {
	input.action == "writeback"
	input.case.control_state != "case_closed"
}

deny_reasons contains "writeback refused: no CRM record for this patient" if {
	input.action == "writeback"
	not input.case.stable_patient_id
}

# --- anything else ----------------------------------------------------------
deny_reasons contains "unknown_action" if {
	not input.action in {"dispatch", "notify", "move", "release", "writeback"}
}

decision := {"allow": allow, "deny_reasons": deny_reasons}
