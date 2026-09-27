// ---- new case ---------------------------------------------------------------
//
// The intake form needs no result renderer of its own: on success this hands
// off to the board's own detail panel, which already shows the intake
// outcome, the acuity block, the gate form when one is needed, the trace
// check and the full audit trail. That holds for a refused or incomplete
// submission too, which projects to no card at all — `openPanel` renders
// those as "off the board".

function toggleIntakePanel() {
  const panel = document.getElementById("intake-panel");
  const opening = panel.hidden || !panel.classList.contains("open");
  panel.hidden = false;
  panel.classList.toggle("open", opening);
  if (opening) {
    // Built on open, not at load: the complaint list arrives with the first
    // board refresh.
    buildRealInputs();
    document.getElementById("patient-id").focus();
  }
  else closeIntakePanel();
}

function closeIntakePanel() {
  const panel = document.getElementById("intake-panel");
  panel.classList.remove("open");
  panel.hidden = true;
  resetIntakeForm();
}

// Closing (X, Escape, or a successful submit) always clears the form — a
// nurse reopening it for the next patient must never see the last one's
// national ID, lookup result, or submission-type choice.
function resetIntakeForm() {
  document.getElementById("patient-id").value = "";
  const lookupResult = document.getElementById("lookup-result");
  lookupResult.className = "";
  lookupResult.textContent = "";
  document.querySelector('input[name="submission_type"][value="clean"]').checked = true;
  document.querySelector('input[name="case_mode"][value="real"]').checked = true;
  document.getElementById("violation").selectedIndex = 0;
  buildRealInputs();
  updateModeFields();
  const submitResult = document.getElementById("submit-result");
  submitResult.className = "msg";
  submitResult.textContent = "";
  updateSubmitEnabled();
}

// The typed clinical fields of a real case, rebuilt empty on every open.
let realInputs = null;

function buildRealInputs() {
  realInputs = clinicalInputs();
  document.getElementById("real-inputs").replaceChildren(...realInputs.nodes);
}

// The rule picker's options, in words, keyed by the checker's rule names.
document.getElementById("violation").replaceChildren(
  ...Object.entries(TRACE_RULE_LABELS).map(([rule, label]) => {
    const opt = el("option", null, label);
    opt.value = rule;
    return opt;
  }));

const caseMode = () => document.querySelector('input[name="case_mode"]:checked').value;
const submissionType = () => document.querySelector('input[name="submission_type"]:checked').value;

// Real shows the typed fields; Demo shows the fixed submissions, and the rule
// picker only for the trace-violation demo.
function updateModeFields() {
  const demo = caseMode() === "demo";
  document.getElementById("real-fields").hidden = demo;
  document.getElementById("demo-fields").hidden = !demo;
  document.getElementById("violation-fields").hidden = submissionType() !== "trace_violation";
  if (demo) renderPayloadPreview();
}

// What each demo scenario sends, from the server's own builder, fetched once.
let demoCases = null;

// Every intake field, in form order, with the fields a scenario leaves out
// marked "not sent" — the absence is the point of several scenarios.
const PREVIEW_ROWS = [
  ["channel", (c) => c.channel],
  ["national ID", (c) => ("national_id" in c
    ? document.getElementById("patient-id").value.trim() || "(the ID typed above)" : undefined)],
  ["nurse acuity", (c) => c.nurse_proposed_acuity && `ESI ${c.nurse_proposed_acuity}`],
  ["chief complaint", (c) => c.chief_complaint && c.chief_complaint.replace(/_/g, " ")],
  ["HR", (c) => c.vitals && c.vitals.hr],
  ["BP", (c) => c.vitals && c.vitals.bp],
  ["SpO2", (c) => c.vitals && c.vitals.spo2 && `${c.vitals.spo2}%`],
  ["temp", (c) => c.vitals && c.vitals.temp_c && `${c.vitals.temp_c} °C`],
  ["free text", (c) => c.free_text && `“${c.free_text}”`],
];

async function renderPayloadPreview() {
  const box = document.getElementById("payload-preview");
  if (!demoCases) {
    try {
      demoCases = await (await fetch("/api/demo-cases")).json();
    } catch {
      box.replaceChildren(el("div", "v not-sent", "could not load the scenarios"));
      return;
    }
  }
  const kind = submissionType();
  const rows = [];
  PREVIEW_ROWS.forEach(([label, pick]) => {
    const value = pick(demoCases.cases[kind]);
    const sent = value !== undefined && value !== null && value !== "";
    rows.push(el("div", "k", label), el("div", sent ? "v" : "v not-sent", sent ? String(value) : "not sent"));
  });
  if (kind === "trace_violation") {
    const rule = document.getElementById("violation").value;
    rows.push(el("div", "k", "fake log entries added after"),
              el("div", "v planted", demoCases.planted[rule].join(" → ").replace(/_/g, " ")
                + " — written straight into the log, bypassing every guard"));
  }
  box.replaceChildren(...rows);
}

// The body `/api/submit` expects: typed fields for a real case, a demo type
// (and its rule) for a demo case.
function submitBody(nationalId) {
  if (caseMode() === "real") {
    return { national_id: nationalId, fields: realInputs.read() };
  }
  const body = { national_id: nationalId, submission_type: submissionType() };
  if (body.submission_type === "trace_violation") body.violation = document.getElementById("violation").value;
  return body;
}

// Only an empty ID blocks submission. not_found and db_error are both valid
// outcomes to continue from: a new patient and an unreachable CRM each mean
// "carry on with intake-only data", they are not failures to stop for.
function updateSubmitEnabled() {
  const id = document.getElementById("patient-id").value.trim();
  document.getElementById("submit-btn").disabled = id.length === 0;
}

async function runLookup() {
  const id = document.getElementById("patient-id").value.trim();
  const out = document.getElementById("lookup-result");
  if (!id) return;

  out.className = "";
  out.textContent = "Looking up…";

  try {
    const res = await fetch(`/api/lookup/${encodeURIComponent(id)}`);
    const { status, record } = await res.json();
    out.className = `status-${status}`;
    if (status === "found") {
      // The internal id is shown because the nurse will see it on the board;
      // the national id they typed is never stored on the case — patient
      // identifiers live only in the CRM.
      out.textContent = `✓ ${record.name} · ${record.date_of_birth} · ${record.stable_patient_id}`;
    } else if (status === "not_found") {
      out.textContent = "⚠ New patient — no record found. Continuing is fine.";
    } else {
      out.textContent = "⚠ CRM unavailable — continuing without history.";
    }
  } catch {
    out.className = "status-db_error";
    out.textContent = "⚠ Could not reach the server.";
  }
  updateSubmitEnabled();
}

async function submitNewCase() {
  const btn = document.getElementById("submit-btn");
  const msg = document.getElementById("submit-result");
  const id = document.getElementById("patient-id").value.trim();

  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = "Submitting…";

  let res;
  try {
    res = await fetch("/api/submit", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(submitBody(id)),
    });
  } catch {
    msg.className = "msg err";
    msg.textContent = "network error";
    btn.disabled = false;
    return;
  }

  const data = await res.json().catch(() => null);
  if (!res.ok || !data || !data.case_id) {
    msg.className = "msg err";
    // A validation refusal carries a list of problems, a guard's refusal a sentence.
    const detail = data && data.detail;
    msg.textContent = (Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : detail)
      || `failed (${res.status})`;
    btn.disabled = false;
    return;
  }

  msg.className = "msg ok";
  msg.textContent = "submitted";
  btn.disabled = false;
  closeIntakePanel();
  await refresh();
  openPanel(data.case_id);
}
