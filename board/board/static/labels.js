// Constants and the label maps that turn the spec's vocabulary into words.

const REFRESH_MS = 5000;
// Mirrors _OPTIONS in app/actors/human_bridge.py — the resume payload's valid
// decisions per escalation reason. Duplicated here the same way the origin
// above is: one hardcoded dev-deployment constant, not a shared source.
const GATE_OPTIONS = {
  discrepancy: ["use_nurse_acuity", "use_system_acuity"],
  low_confidence: ["use_nurse_acuity", "use_system_acuity"],
  safety_fail: ["corrected", "escalate_further"],
};
const GATE_HEADINGS = {
  discrepancy: "Acuity discrepancy",
  low_confidence: "Low-confidence acuity — needs confirmation",
  safety_fail: "Safety validation failed — correct and revalidate",
};
const COLUMN_LABELS = {
  waiting: "Waiting",
  human_review: "Human review",
  reassessment_required: "Reassessment required",
  treatment_started: "Treatment started",
  formal_validation: "Treated — awaiting discharge",
  patient_released: "Released",
};
// A column with no writer shows why it is empty instead of hiding. Every
// column has a writer since 2026-09-26 ("Treatment complete" fills the last
// one), so this is empty; it stays for the next status that arrives on
// paper before it arrives in code.
const NOT_YET_WRITTEN = {};

// The state fields say why a case is unusual; these say it in words.
const DEGRADED_LABELS = { crm: "patient history unavailable" };
const FLAG_LABELS = {
  crm_down_intake_only: "no history — intake details only",
  "cross-check off": "second opinion unavailable",
};
const DRAWER_COLUMN = "patient_released";
// `acuity_source` is the spec's vocabulary; a board is not the place to make a
// nurse learn it.
const SOURCE_LABELS = {
  system: "set by system",
  auto_resolved: "auto-resolved (1-level gap)",
  human_confirmed: "confirmed by nurse",
};

// Everything below translates the spec's vocabulary into something a person can
// read at a glance. The spec names are not thrown away — they go in the `title`
// of the element, so traceability against docs/SPECIFICATION.md is one hover
// away and nobody has to learn transition names to use the board.
const ACTION_LABELS = {
  route_channel: "case arrived",
  invoke_intake_parser: "reading the submission",
  emit_event_log: "",                       // the explanation already says it
  notify_user: "patient notified",
  fetch_patient_data: "looking up patient history",
  build_model_payload: "identifiers removed for the classifier",
  invoke_acuity_classifier: "triage level requested",
  assign_order_key: "place in queue assigned",
  invoke_human_escalation: "sent to the charge nurse",
  apply_human_acuity: "charge nurse set the level",
  apply_correction: "correction applied",
  explain_denial: "action refused",
  alert_technician: "technician alerted",
  discard_output: "result rejected, retrying",
  start_reassessment_timer: "reassessment timer started",
};

const STAGE_LABELS = {
  intake_received: "submission received",
  parsing: "reading the submission",
  data_parsed: "submission read",
  missing_fields_requested: "waiting for missing details",
  submission_failed: "submission unusable",
  input_rejected: "submission rejected",
  resolving_identity: "looking up patient history",
  redacting_routing: "removing identifiers",
  classifying: "deciding triage level",
  acuity_proposed: "triage level proposed",
  safety_validating: "safety check running",
  verdict_proposed: "safety check returned",
  awaiting_human_approval: "waiting for the charge nurse",
  monitoring: "in the waiting room",
  reassessment_required: "due for reassessment",
  case_closed: "closed",
  agent_failed: "a component failed",
  action_denied: "last action refused",
};

const OUTCOME_LABELS = {
  DATA_PARSED: "read successfully",
  MISSING_FIELDS_DETECTED: "details missing",
  SUBMISSION_FAILED: "nothing usable in the submission",
  INVALID_INPUT_DETECTED: "rejected as unsafe input",
};
const RELEASE_REASON_LABELS = {
  discharge: "Discharge", ama: "AMA", transfer: "Transfer", admit: "Admit",
};

// What a notification is about, in words. The raw transition name stays in the tooltip.
const NOTICE_LABELS = {
  missing_fields: "details missing",
  submission_unusable: "submission unusable",
  invalid_input: "unsafe input blocked",
  approval_requested: "approval requested",
  escalation_recorded: "charge nurse answered",
  reassessment_due: "reassessment due",
  move_confirmed: "move to treatment confirmed",
  blk: "action refused",
};

// Who a reminder went to, in words. The monitor widens the audience with each
// rung, so these read as an escalation ladder.
const REMINDER_LABELS = {
  assigned_nurse: "assigned nurse reminded",
  any_charge_nurse: "charge nurse reminded",
  any_shift_lead: "shift lead reminded",
};
// What the monitor found on its own, looking across every case at once.
const ESCALATION_LABELS = {
  unwatched_case: "no timer watching this case",
  orphan_timer: "timer with no case",
  reassessment_overdue: "reassessment overdue",
  store_unreachable: "case store unreachable",
};

const plain = (map, key, fallback) => map[key] || fallback || key || "—";

const nudgeLabel = (n) =>
  n.source === "escalation"
    ? plain(ESCALATION_LABELS, n.kind)
    : plain(REMINDER_LABELS, n.recipient_class);
