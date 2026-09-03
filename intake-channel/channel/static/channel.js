// intake-channel — patient lookup + mock intake submission.
//
// Two separate steps, deliberately not merged into one action (see
// STAGE1_INTAKE_CHANNEL_PLAN.md § 4.3): lookup only enriches what's
// displayed and never decides the submission's identity — the patient ID
// field is what's sent on submit.

const patientIdInput = document.getElementById("patient-id");
const lookupBtn = document.getElementById("lookup-btn");
const lookupResultEl = document.getElementById("lookup-result");
const submitBtn = document.getElementById("submit-btn");
const submitResultEl = document.getElementById("submit-result");

// Only a missing/empty ID blocks submission — not_found and db_error are
// both valid outcomes to continue from (SPECIFICATION.md's fail-open rule).
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
    const body = await res.json();
    renderLookupResult(body);
  } catch (err) {
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
    const body = await res.json();
    renderSubmitResult(body);
  } catch (err) {
    submitResultEl.textContent = "⚠ Submit failed — is intake-channel running?";
  }
});

function renderSubmitResult(body) {
  let html = `<pre>${JSON.stringify(body, null, 2)}</pre>`;
  html += "<div>↳ sent to Langfuse ✓</div>";

  if (body.outcome === "MISSING_FIELDS_DETECTED") {
    // The completion screen for these fields needs case-level state
    // (missing_fields_requested) that doesn't exist yet in this stage —
    // see STAGE1_INTAKE_CHANNEL_PLAN.md § 11 for what's deferred and why.
    html +=
      '<div id="missing-fields-note">' +
      "A real completion form for the fields above (" +
      body.missing_fields.join(", ") +
      ") arrives once the state machine stage lands.</div>";
  }

  submitResultEl.innerHTML = html;
}
