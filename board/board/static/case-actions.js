// What a nurse can do from the panel: move, release, answer a gate, re-file.

// Shared by both buttons in `movesSection`. The endpoint returns HTTP 200
// even when the in-graph guard (`move_authorized`/`release_authorized`)
// refuses the action — `status: "denied"` in the body is how a refusal is
// told apart from success, so a wrong-role click can't show a false
// "moved"/"released" confirmation. On refusal the button re-enables (the
// case stays retriable, e.g. by a charge nurse instead of a nurse) rather
// than treating it as terminal.
async function postCaseAction(btn, msg, url, body, pendingText, okText, onOk) {
  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = pendingText;
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    msg.className = "msg err";
    msg.textContent = "network error";
    btn.disabled = false;
    return;
  }
  let data;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    msg.className = "msg err";
    msg.textContent = (data && data.detail) || `failed (${res.status})`;
    btn.disabled = false;
    return;
  }
  if (!data || typeof data.status !== "string") {
    // A malformed 200 body must not be read as silent success — the real
    // outcome of the action is unknown.
    msg.className = "msg err";
    msg.textContent = "unexpected response from server";
    btn.disabled = false;
    return;
  }
  if (data.status === "denied") {
    msg.className = "msg err";
    msg.textContent = data.detail || "denied";
    btn.disabled = false;
    refresh();
    return;
  }
  msg.className = "msg ok";
  msg.textContent = okText;
  refresh();
  onOk();
}

function movesSection(caseId, card) {
  const box = el("div", "section moves");
  box.append(el("h3", null, "Manual status change"));

  const controls = el("div", "controls");
  const msg = el("div", "msg");

  const moveBtn = el("button", "move-btn", "Start treatment");
  moveBtn.disabled = card ? card.status !== "waiting" : true;
  moveBtn.title = moveBtn.disabled
    ? "only a patient currently waiting can be moved into treatment"
    : "";
  moveBtn.onclick = () => postCaseAction(
    moveBtn, msg, `/api/case/${encodeURIComponent(caseId)}/move-to-treatment`,
    { actor_role: "nurse" }, "moving…", "moved to treatment",
    () => setTimeout(() => openPanel(caseId), 300),
  );

  // Treatment done: the case moves to the sign-off column (spec arrow FV).
  const completeBtn = el("button", "move-btn", "Treatment complete");
  completeBtn.disabled = card ? card.status !== "treatment_started" : true;
  completeBtn.title = completeBtn.disabled ? "only a patient in treatment can be signed off" : "";
  completeBtn.onclick = () => postCaseAction(
    completeBtn, msg, `/api/case/${encodeURIComponent(caseId)}/treatment-complete`,
    { actor_role: "nurse" }, "marking treated…", "treated — awaiting discharge",
    () => setTimeout(() => openPanel(caseId), 300),
  );

  const canRelease = card && ["waiting", "treatment_started", "formal_validation"].includes(card.status);

  const reasonSelect = el("select", "reason-select");
  [["", "release reason"], ["discharge", "Discharge"], ["ama", "AMA"],
   ["transfer", "Transfer"], ["admit", "Admit"]].forEach(([value, label]) => {
    const opt = el("option", null, label);
    opt.value = value;
    reasonSelect.append(opt);
  });
  reasonSelect.disabled = !canRelease;

  const releaseBtn = el("button", "release-btn", "Release patient");
  releaseBtn.disabled = !canRelease;
  releaseBtn.title = canRelease
    ? "" : "release is only wired up from waiting / treatment started in this version";
  releaseBtn.onclick = () => {
    if (!reasonSelect.value) {
      msg.className = "msg err";
      msg.textContent = "pick a release reason first";
      return;
    }
    postCaseAction(
      releaseBtn, msg, `/api/case/${encodeURIComponent(caseId)}/release`,
      { reason: reasonSelect.value, actor_role: "charge_nurse" }, "releasing…", "released",
      closePanel,
    );
  };

  const releaseGroup = el("div", "release-group");
  releaseGroup.append(reasonSelect, releaseBtn);

  const btnRow = el("div", "btn-row");
  btnRow.append(moveBtn, completeBtn);

  controls.append(btnRow, releaseGroup);
  box.append(controls, msg);
  return box;
}

function gatePanel(caseId, view, card) {
  // `view.gate` is the board's own stub pending object (see board/board/api.py's
  // `case()`), not the raw interrupt payload — `.gate` on it is the escalation
  // reason string.
  const rawReason = view.gate && view.gate.gate;
  const reason = GATE_OPTIONS[rawReason] ? rawReason : null;
  const severe = reason === "safety_fail";
  const box = el("div", "section gate-resolve" + (severe ? " severe" : ""));
  box.append(el("h3", null, "Resolve gate"));

  const heading = reason === "discrepancy" || reason === "low_confidence"
    ? `${GATE_HEADINGS[reason]} — nurse proposed ${card ? card.nurse_proposed_acuity : "—"}, `
      + `system proposed ${card ? card.system_proposed_acuity : "—"} (gap ${view.acuity_gap}).`
    : reason
      ? GATE_HEADINGS[reason]
      : `Awaiting a charge nurse's decision (${rawReason || "reason unknown"}).`;
  box.append(el("div", "heading", heading));

  const role = el("select");
  [["charge_nurse", "charge_nurse"], ["shift_lead", "shift_lead"],
   ["nurse", "nurse (not authorized — will be refused)"]].forEach(([value, label]) => {
    const opt = el("option", null, label);
    opt.value = value;
    role.append(opt);
  });

  // Readable stand-ins for the raw decision codes the API expects. The two
  // acuity-reason decisions get the actual proposed number, so the nurse
  // isn't cross-referencing the heading above to know what each button does.
  const decisionLabels = {
    use_nurse_acuity: `Use nurse's acuity — ESI ${card ? card.nurse_proposed_acuity : "?"}`,
    use_system_acuity: `Use system's acuity — ESI ${card ? card.system_proposed_acuity : "?"}`,
    corrected: "Corrected — resubmit for revalidation",
    escalate_further: "Escalate further",
  };

  // A safety failure names what contradicts what — the case continues only once
  // that's corrected and passes safety again. Show it, and take the corrected
  // level here: "Corrected" with nothing changed is refused by the gate.
  const reasons = severe && view.safety_reasons && view.safety_reasons.length
    ? el("ul", "reasons") : null;
  if (reasons) view.safety_reasons.forEach((r) => reasons.append(el("li", null, r)));
  let correction = null;
  if (severe) {
    correction = el("select");
    const placeholder = el("option", null, "corrected acuity — select ESI level");
    placeholder.value = "";
    correction.append(placeholder);
    [1, 2, 3, 4, 5].forEach((n) => {
      const opt = el("option", null, `ESI ${n}`);
      opt.value = String(n);
      correction.append(opt);
    });
  }

  const msg = el("div", "msg");
  const options = el("div", "controls");
  (GATE_OPTIONS[reason] || []).forEach((decision) => {
    const btn = el("button", null, decisionLabels[decision] || decision);
    btn.onclick = async () => {
      if (decision === "corrected" && correction && !correction.value) {
        msg.className = "msg err";
        msg.textContent = "choose the corrected acuity first";
        return;
      }
      options.querySelectorAll("button").forEach((b) => (b.disabled = true));
      msg.className = "msg";
      msg.textContent = "resolving…";
      const body = { decision, resolver_role: role.value };
      if (decision === "corrected" && correction) body.corrections = { acuity: Number(correction.value) };
      try {
        const res = await fetch(`/api/case/${encodeURIComponent(caseId)}/resume`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          msg.className = "msg err";
          msg.textContent = body.detail || `failed (${res.status})`;
          options.querySelectorAll("button").forEach((b) => (b.disabled = false));
          return;
        }
        msg.className = "msg ok";
        msg.textContent = "resolved";
        refresh();
        setTimeout(() => openPanel(caseId), 300);
      } catch {
        msg.className = "msg err";
        msg.textContent = "network error";
        options.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    };
    options.append(btn);
  });

  if (reasons) box.append(reasons);
  box.append(el("label", null, "Resolver role"), role);
  if (correction) box.append(el("label", null, "Corrected acuity"), correction);
  box.append(options, msg);
  return box;
}

function refilePanel(caseId) {
  const box = el("div", "section refile");
  box.append(el("h3", null, "Re-file reassessment"),
             el("div", "meta", "The reassessment timer fired. Enter this patient's current "
               + "observations to re-triage them — the system never carries the old numbers "
               + "forward on its own."));

  const acuity = el("select");
  const placeholder = el("option", null, "select ESI level");
  placeholder.value = "";
  acuity.append(placeholder);
  [1, 2, 3, 4, 5].forEach((n) => {
    const opt = el("option", null, `ESI ${n}`);
    opt.value = String(n);
    acuity.append(opt);
  });

  // A code from the fixed set, never typed prose — the model receives only
  // approved, fixed-choice fields.
  const complaint = el("select");
  const complaintPlaceholder = el("option", null, "select chief complaint");
  complaintPlaceholder.value = "";
  complaint.append(complaintPlaceholder);
  chiefComplaints.forEach((code) => {
    const opt = el("option", null, code.replace(/_/g, " "));
    opt.value = code;
    complaint.append(opt);
  });

  const hr = el("input"); hr.type = "number"; hr.placeholder = "HR";
  const bp = el("input"); bp.type = "text"; bp.placeholder = "BP (e.g. 120/80)";
  const spo2 = el("input"); spo2.type = "number"; spo2.placeholder = "SpO2";
  const temp = el("input"); temp.type = "number"; temp.step = "0.1"; temp.placeholder = "Temp °C";

  const msg = el("div", "msg");
  const submit = el("button", null, "Submit re-file");
  const vitalsBox = el("div", "vitals");
  vitalsBox.append(hr, bp, spo2, temp);

  box.append(
    el("label", null, "Nurse-proposed acuity"), acuity,
    el("label", null, "Chief complaint"), complaint,
    el("label", null, "Vitals"), vitalsBox,
    submit, msg,
  );

  submit.onclick = async () => {
    if (!acuity.value || !complaint.value.trim()) {
      msg.className = "msg err";
      msg.textContent = "acuity and chief complaint are required";
      return;
    }
    submit.disabled = true;
    msg.className = "msg";
    msg.textContent = "submitting…";
    try {
      const res = await fetch(`/api/case/${encodeURIComponent(caseId)}/reassess`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          nurse_proposed_acuity: Number(acuity.value),
          chief_complaint: complaint.value.trim(),
          vitals: { hr: hr.value ? Number(hr.value) : null, bp: bp.value || null,
                    spo2: spo2.value ? Number(spo2.value) : null,
                    temp_c: temp.value ? Number(temp.value) : null },
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        msg.className = "msg err";
        msg.textContent = body.detail || `failed (${res.status})`;
        submit.disabled = false;
        return;
      }
      msg.className = "msg ok";
      msg.textContent = "re-filed — re-entering intake";
      refresh();
      setTimeout(() => openPanel(caseId), 300);
    } catch (err) {
      msg.className = "msg err";
      msg.textContent = "network error";
      submit.disabled = false;
    }
  };

  return box;
}
