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

# A pass, or a shift lead's clearance while the safety check could not run.
safety_settled if input.case.safety_passed == true

safety_settled if input.case.safety_waived == true

allow if {
	input.action == "move"
	safety_settled
	input.case.approved == true
	input.actor_role in move_roles
	order_settled
}

deny_reasons contains "move refused: safety not passed" if {
	input.action == "move"
	not safety_settled
}

deny_reasons contains "move refused: not approved" if {
	input.action == "move"
	input.case.approved != true
}

deny_reasons contains sprintf("move refused: role %q not authorized", [input.actor_role]) if {
	input.action == "move"
	not input.actor_role in move_roles
}

# Out of order: someone still in line is ahead of this patient. Allowed, but
# only with a reason from this fixed set: never free text, which could carry
# patient details into the audit log. Same set as `SKIP_REASONS` in
# app/deterministic.py; `tests/test_out_of_order_move.py` fails if they drift.
# `skipped` is the queue positions ahead, computed by the board's server from
# the global queue, never taken from the browser.
move_skip_reasons := {"different_care_area", "patient_ahead_unavailable", "clinical_judgment"}

skipped := object.get(input, ["queue", "skipped"], [])

order_settled if count(skipped) == 0

order_settled if input.queue.skip_reason in move_skip_reasons

# The reason itself is not echoed: a value outside the set is exactly the
# free text that must not reach the log.
deny_reasons contains sprintf("move refused: out of order, %d patient(s) still ahead in line and no reason from the allowed set", [count(skipped)]) if {
	input.action == "move"
	not order_settled
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
