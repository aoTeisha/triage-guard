// intake-channel — patient lookup, intake submission, and the human gate.
//
// Lookup and submit stay two separate steps: lookup only enriches what is
// displayed and never decides the submission's identity — the patient ID field
// is what gets sent.
//
// Submit now drives the real graph, so a case can come back *paused* at the
// acuity gate rather than settled. That is what the gate panel below is for:
// the charge nurse's answer goes to /resume/{case_id} and the case continues.

const patientIdInput = document.getElementById("patient-id");
const lookupBtn = document.getElementById("lookup-btn");
const lookupResultEl = document.getElementById("lookup-result");
const submitBtn = document.getElementById("submit-btn");
const submitResultEl = document.getElementById("submit-result");

// Only a missing/empty ID blocks submission — not_found and db_error are both
// valid outcomes to continue from (SPECIFICATION.md's fail-open rule).
function updateSubmitEnabled() {
  submitBtn.disabled = patientIdInput.value.trim().length === 0;
}

patientIdInput.addEventListener("input", updateSubmitEnabled);

lookupBtn.addEventListener("click", async () => {
  const id = patientIdInput.value.trim();
  if (!id) return;

  lookupResultEl.textContent = "Looking up…";
  lookupResultEl.className = "";

  try {
    const res = await fetch(`/lookup/${encodeURIComponent(id)}`);
    renderLookupResult(await res.json());
  } catch {
    lookupResultEl.textContent = "⚠ Could not reach intake-channel.";
    lookupResultEl.className = "status-db_error";
  }
  updateSubmitEnabled();
});

function renderLookupResult({ status, record }) {
  lookupResultEl.className = `status-${status}`;
  if (status === "found") {
    lookupResultEl.textContent = `✓ ${record.name} · ${record.date_of_birth}`;
  } else if (status === "not_found") {
    lookupResultEl.textContent = "⚠ New patient — no record found. Continuing is fine.";
  } else {
    lookupResultEl.textContent = "⚠ CRM unavailable — continuing without history.";
  }
}

submitBtn.addEventListener("click", async () => {
  const id = patientIdInput.value.trim();
  const submissionType = document.querySelector(
    'input[name="submission_type"]:checked'
  ).value;

  submitResultEl.innerHTML = "Submitting…";

  try {
    const res = await fetch("/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stable_patient_id: id, submission_type: submissionType }),
    });
    render(await res.json());
  } catch {
    submitResultEl.textContent = "⚠ Submit failed — is intake-channel running?";
  }
});

async function resolveGate(caseId, decision, resolverRole) {
  submitResultEl.innerHTML = "Resolving…";
  try {
    const res = await fetch(`/resume/${encodeURIComponent(caseId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, resolver_role: resolverRole }),
    });
    render(await res.json());
  } catch {
    submitResultEl.textContent = "⚠ Resume failed.";
  }
}

function render(body) {
  submitResultEl.innerHTML = "";

  submitResultEl.appendChild(summary(body));
  if (body.gate) submitResultEl.appendChild(gatePanel(body));
  if (body.outcome === "MISSING_FIELDS_DETECTED") {
    submitResultEl.appendChild(
      note(`Missing: ${body.missing_fields.join(", ")}. A completion form for these
            arrives with the missing-fields return path (arrow 1b.x).`)
    );
  }
  submitResultEl.appendChild(trail(body.audit_log || []));
}

function summary(body) {
  const el = document.createElement("div");
  el.className = "summary";
  const rows = [
    ["case", body.case_id],
    ["control state", body.control_state],
    ["intake outcome", body.outcome],
    ["acuity", body.acuity === null ? "—" : `${body.acuity} (${body.acuity_source || "—"})`],
    ["safety passed", String(body.safety_passed)],
    ["degraded", (body.degraded || []).join(", ") || "—"],
  ];
  el.innerHTML = rows
    .map(([k, v]) => `<div><span class="k">${k}</span><span class="v">${v ?? "—"}</span></div>`)
    .join("");
  return el;
}

function gatePanel({ case_id, gate }) {
  const el = document.createElement("div");
  el.className = "gate";

  const heading = gate.gate === "discrepancy"
    ? `Acuity discrepancy — nurse proposed ${gate.nurse_proposed_acuity},
       system proposed ${gate.system_proposed_acuity} (gap ${gate.acuity_gap}).`
    : "Safety validation failed — a charge nurse must correct and revalidate.";

  el.innerHTML = `
    <strong>Paused — awaiting ${gate.required_role}</strong>
    <p>${heading}</p>
    <label>Resolver role
      <select class="role">
        <option value="charge_nurse">charge_nurse</option>
        <option value="shift_lead">shift_lead</option>
        <option value="nurse">nurse (not authorized — will be refused)</option>
      </select>
    </label>
    <div class="options"></div>`;

  const role = el.querySelector(".role");
  const options = el.querySelector(".options");
  gate.options.forEach((option) => {
    const btn = document.createElement("button");
    btn.textContent = option;
    btn.addEventListener("click", () => resolveGate(case_id, option, role.value));
    options.appendChild(btn);
  });

  return el;
}

function note(text) {
  const el = document.createElement("div");
  el.id = "missing-fields-note";
  el.textContent = text;
  return el;
}

function trail(rows) {
  const el = document.createElement("details");
  el.className = "trail";
  el.innerHTML =
    `<summary>Audit trail — ${rows.length} records</summary>` +
    `<table>${rows
      .map(
        (r) =>
          `<tr><td class="arrow">${r.arrow || "—"}</td>` +
          `<td class="action">${r.action}</td>` +
          `<td>${r.explanation}</td></tr>`
      )
      .join("")}</table>`;
  return el;
}
