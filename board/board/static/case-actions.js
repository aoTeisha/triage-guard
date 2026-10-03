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

// Who is acting, per case. The panel re-renders on every 5s poll while open;
// without remembering the pick here, each re-render would forget it and the
// OPA-down default below would silently overwrite whatever the user chose.
const lastActorRole = {};

// OPA authorizes moves and releases; while it is down only a shift lead may
// sign them, so the picker defaults to one and says why — but only the first
// time it's shown for a case, never on top of a role the user already picked.
function actorPicker(caseId, defaultRole) {
  const role = el("select", "role-select");
  [["nurse", "nurse"], ["charge_nurse", "charge nurse"], ["shift_lead", "shift lead"]]
    .forEach(([value, label]) => {
      const opt = el("option", null, label);
      opt.value = value;
      role.append(opt);
    });
  role.value = lastActorRole[caseId] || (isDown("opa") ? "shift_lead" : defaultRole);
  role.onchange = () => { lastActorRole[caseId] = role.value; };
  return role;
}

function movesSection(caseId, card) {
  const box = el("div", "section moves");
  box.append(el("h3", null, "Manual status change"));

  const controls = el("div", "controls");
  const msg = el("div", "msg");
  const role = actorPicker(caseId, "charge_nurse");
  controls.append(el("span", "meta", "acting as"), role);
  if (isDown("opa")) {
    controls.append(el("div", "role-note",
      "OPA is down: moves and releases need a shift lead's sign-off."));
  }

  const moveBtn = el("button", "move-btn", "Start treatment");
  moveBtn.disabled = card ? card.status !== "waiting" : true;
  moveBtn.title = moveBtn.disabled
    ? "only a patient currently waiting can be moved into treatment"
    : "";
  moveBtn.onclick = () => postCaseAction(
    moveBtn, msg, `/api/case/${encodeURIComponent(caseId)}/move-to-treatment`,
    { actor_role: role.value }, "moving…", "moved to treatment",
    () => setTimeout(() => openPanel(caseId), 300),
  );

  // Treatment done: the case moves to the sign-off column (spec arrow FV).
  const completeBtn = el("button", "move-btn", "Treatment complete");
  completeBtn.disabled = card ? card.status !== "treatment_started" : true;
  completeBtn.title = completeBtn.disabled ? "only a patient in treatment can be signed off" : "";
  completeBtn.onclick = () => postCaseAction(
    completeBtn, msg, `/api/case/${encodeURIComponent(caseId)}/treatment-complete`,
    { actor_role: role.value }, "marking treated…", "treated — awaiting discharge",
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
      { reason: reasonSelect.value, actor_role: role.value }, "releasing…", "released",
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
    : reason === "validator_down"
      ? `${(view.validator_down || []).map((e) => e[0].toUpperCase() + e.slice(1)).join(" and ")
          || "The safety validator"} unavailable — ${GATE_HEADINGS[reason]}.`
    : reason
      ? GATE_HEADINGS[reason]
      : `Awaiting a charge nurse's decision (${rawReason || "reason unknown"}).`;
  box.append(el("div", "heading", heading));
  // Why the model chose its level, and how sure it was — what a charge nurse
  // weighs when picking between the two. Only on the acuity gates, and only
  // while there is a model proposal: a classifier fallback has no reason to show.
  const aiReason = (reason === "discrepancy" || reason === "low_confidence")
    && card && card.system_proposed_acuity != null && view.classifier_rationale;
  if (aiReason) {
    const sure = typeof view.confidence === "number"
      ? ` (${Math.round(view.confidence * 100)}% confident)` : "";
    const why = el("div", "ai-reasoning");
    why.append(el("span", "ai-label", `AI's reasoning${sure}:`), " ", view.classifier_rationale);
    box.append(why);
  }

  const role = el("select");
  [["charge_nurse", "charge_nurse"], ["shift_lead", "shift_lead"],
   ["nurse", "nurse (not authorized — will be refused)"]].forEach(([value, label]) => {
    const opt = el("option", null, label);
    opt.value = value;
    role.append(opt);
  });
  // Once the correction loop has escalated to a shift lead, the gate refuses
  // a charge nurse. Default the picker to whoever it will actually accept.
  if (view.senior_required || reason === "validator_down") role.value = "shift_lead";
  // Prolog checks who may answer a gate; while it is down a shift lead answers.
  const prologDown = isDown("prolog");
  if (prologDown) role.value = "shift_lead";

  // Readable stand-ins for the raw decision codes the API expects. The two
  // acuity-reason decisions get the actual proposed number, so the nurse
  // isn't cross-referencing the heading above to know what each button does.
  const decisionLabels = {
    use_nurse_acuity: `Use nurse's acuity — ESI ${card ? card.nurse_proposed_acuity : "?"}`,
    use_system_acuity: `Use system's acuity — ESI ${card ? card.system_proposed_acuity : "?"}`,
    corrected: "Corrected — resubmit for revalidation",
    revalidate: "Revalidate — run the safety check again",
    escalate_further: "Escalate further",
    clear_by_shift_lead: "Clear to queue without the check (shift lead)",
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
        // /resume replies 200 with the bare case view even when the gate refuses:
        // the refusal is only the newest audit row being a BLK.
        const view = await res.json().catch(() => null);
        const last = view && (view.audit_log || []).slice(-1)[0];
        if (last && last.transition === "blk") {
          msg.className = "msg err";
          msg.textContent = readable(last.explanation) || "refused";
          options.querySelectorAll("button").forEach((b) => (b.disabled = false));
          refresh();
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
  if (prologDown) {
    box.append(el("div", "role-note", "Prolog is down: only a shift lead can answer this gate."));
  }
  if (correction) box.append(el("label", null, "Corrected acuity"), correction);
  box.append(options, msg);
  return box;
}

// The clinical fields a nurse types: acuity, complaint, vitals. Shared by the
// re-file form and the new-case form so the two cannot drift. `read()` returns
// only what was filled in — a blank field is left out, never sent as null.
function clinicalInputs() {
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

  // min/max mirror VITAL_BOUNDS in triage-app/app/guards/fields.py as a typing
  // aid only; the server's unusable_fields check is the one that counts.
  const hr = el("input"); hr.type = "number"; hr.placeholder = "e.g. 88";
  hr.min = "20"; hr.max = "300";
  const bp = el("input"); bp.type = "text"; bp.inputMode = "numeric";
  bp.placeholder = "e.g. 120/80"; bp.maxLength = 7;
  // Digits only; the slash is inserted automatically once the systolic
  // number (always 3 digits, adult vitals) is typed — never free text.
  bp.addEventListener("input", () => {
    const digits = bp.value.replace(/\D/g, "").slice(0, 6);
    bp.value = digits.length > 3 ? `${digits.slice(0, 3)}/${digits.slice(3)}` : digits;
  });
  const spo2 = el("input"); spo2.type = "number"; spo2.placeholder = "e.g. 98";
  spo2.min = "30"; spo2.max = "100";
  const temp = el("input"); temp.type = "number"; temp.step = "0.1"; temp.placeholder = "e.g. 37.0";
  temp.min = "20"; temp.max = "46";

  // A persistent caption above each vital, so the field's meaning survives
  // once the placeholder is gone (the moment the nurse starts typing).
  const field = (caption, full, input) => {
    const wrap = el("div", "field");
    const label = el("span", "field-label", caption);
    label.title = full;
    input.title = full;
    wrap.append(label, input);
    return wrap;
  };
  const vitalsBox = el("div", "vitals");
  vitalsBox.append(field("HR", "Heart rate", hr), field("BP", "Blood pressure", bp), field("SpO2", "Oxygen saturation", spo2), field("Temp °C", "Temperature", temp));

  return {
    nodes: [
      el("label", null, "Nurse-proposed acuity"), acuity,
      el("label", null, "Chief complaint"), complaint,
      el("label", null, "Vitals"), vitalsBox,
    ],
    read() {
      const fields = {};
      if (acuity.value) fields.nurse_proposed_acuity = Number(acuity.value);
      if (complaint.value) fields.chief_complaint = complaint.value;
      const vitals = {};
      if (hr.value) vitals.hr = Number(hr.value);
      if (bp.value.trim()) vitals.bp = bp.value.trim();
      if (spo2.value) vitals.spo2 = Number(spo2.value);
      if (temp.value) vitals.temp_c = Number(temp.value);
      if (Object.keys(vitals).length) fields.vitals = vitals;
      return fields;
    },
    allFilled: () => Boolean(acuity.value && complaint.value && hr.value
      && bp.value.trim() && spo2.value && temp.value),
  };
}

function refilePanel(caseId) {
  const box = el("div", "section refile");
  box.append(el("h3", null, "Re-file reassessment"),
             el("div", "meta", "The reassessment timer fired. Enter this patient's current "
               + "observations to re-triage them — the system never carries the old numbers "
               + "forward on its own."));

  const msg = el("div", "msg");
  const submit = el("button", null, "Submit re-file");
  const inputs = clinicalInputs();
  box.append(...inputs.nodes, submit, msg);

  submit.onclick = async () => {
    const fields = inputs.read();
    if (!fields.nurse_proposed_acuity || !fields.chief_complaint) {
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
        body: JSON.stringify({ vitals: {}, ...fields }),
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
