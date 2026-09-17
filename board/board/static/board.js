// The board UI. One fetch of /api/board every 5s, one render.
//
// No framework and no build step — the page is a projection of one JSON payload,
// and SSE is a later swap behind the same shape. Nothing here decides ordering or
// position: both arrive from the server, which reads the persisted order_key.
// ponytail: revisit the no-framework call if this file passes ~400 lines.

const REFRESH_MS = 5000;
// /reassess lives on intake-channel, a separate service/origin from the
// board — one hardcoded dev-deployment constant, matching intake-channel's
// own CORS allow-list default (BOARD_ORIGIN in channel/api.py).
const INTAKE_CHANNEL_ORIGIN = "http://localhost:8001";
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
  formal_validation: "Formal validation",
  patient_released: "Released",
};
// Nothing writes formal_validation yet — this minimal version releases
// straight from treatment_started, skipping it (see findings.md). The
// column is rendered anyway, with the reason — an empty column is the
// honest state.
const NOT_YET_WRITTEN = {
  formal_validation: "not in use yet — this version releases without a formal sign-off step",
};

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
// away and nobody has to learn arrow numbers to use the board.
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

// What a notification is about, in words. Arrow codes stay in the tooltip.
const NOTICE_LABELS = {
  "16": "details missing",
  "17": "submission unusable",
  "18": "unsafe input blocked",
  "20": "approval requested",
  "12": "charge nurse answered",
  "14": "reassessment due",
  "19": "move to treatment confirmed",
  BLK: "action refused",
};

const plain = (map, key, fallback) => map[key] || fallback || key || "—";

let selected = null;
// Case ids seen on the previous poll. A new arrival sorts into the middle of a
// long column by arrival time — correct clinically, invisible in practice — so
// it gets a brief highlight. Display only: nothing about the queue changes.
let seen = null;

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

function counter(label, value, alert) {
  const c = el("div", "counter" + (alert ? " alert" : ""));
  c.append(el("div", "n", String(value)), el("div", "l", label));
  return c;
}

function renderCounters(c) {
  const box = document.getElementById("counters");
  box.replaceChildren(
    counter("in queue", c.waiting),
    counter("emergent", c.emergent, c.emergent > 0),
    counter("awaiting approval", c.gate_pending, c.gate_pending > 0),
    counter("reassess due", c.reassessment_required),
    counter("longest wait", formatWait(c.longest_wait_min)),
    counter("avg wait", formatWait(Math.round(c.avg_wait_min))),
  );
}

function renderNotifications(items) {
  const badge = document.getElementById("notif-badge");
  badge.textContent = String(items.length);
  badge.hidden = items.length === 0;

  const box = document.getElementById("notif-list");
  if (!items.length) {
    box.replaceChildren(el("div", "notif-item", "no notifications"));
    return;
  }
  box.replaceChildren(...items.map((n) => {
    const node = el("div", "notif-item" + (n.arrow === "BLK" ? " blk" : ""));
    node.append(el("div", "arrow", plain(NOTICE_LABELS, n.arrow)),
                el("div", "complaint", n.complaint || ""),
                el("div", "at", n.at));
    node.title = `${n.case_id} · arrow ${n.arrow} · ${n.action} · ${readable(n.explanation)} · ${n.at}`;
    // Clicking a notification opens the case panel but leaves this sidebar
    // open — only the X closes it.
    node.onclick = () => openPanel(n.case_id);
    return node;
  }));
}

function toggleNotifPanel() {
  const panel = document.getElementById("notif-panel");
  if (panel.classList.contains("open")) {
    closeNotifPanel();
  } else {
    panel.hidden = false;
    panel.classList.add("open");
  }
}

function closeNotifPanel() {
  const panel = document.getElementById("notif-panel");
  panel.classList.remove("open");
  panel.hidden = true;
}

function formatWait(min) {
  return min < 60 ? `${min}m` : `${Math.floor(min / 60)}h ${min % 60}m`;
}

function isRed(card, thresholds) {
  const limit = thresholds[card.bucket];
  return limit !== undefined && card.waited_min >= limit;
}

function renderCard(card, thresholds) {
  const node = el("div", "card" + (card.bucket === "emergent" ? " emergent" : "")
    + (card.case_id === selected ? " selected" : "")
    + (seen && !seen.has(card.case_id) ? " just-arrived" : ""));
  node.append(el("div", "pos", card.position ? String(card.position) : "–"));

  // The complaint is the headline: it is what a nurse scans a board for, and it
  // is identifier-free. The patient id is a reference, so it reads as one.
  node.append(el("div", "who", card.complaint || "no complaint recorded"));

  const meta = el("div", "meta");
  const wait = el("span", "wait" + (isRed(card, thresholds) ? " red" : ""),
                  "waiting " + formatWait(card.waited_min));
  meta.append(wait, document.createTextNode(
    `  ·  ${card.patient_label}  ·  patient ${card.patient_id || "unknown"}`));
  node.append(meta);

  const chips = el("div", "chips");
  if (card.acuity !== null && card.acuity !== undefined) {
    const chip = el("span", "chip acuity" + (card.bucket === "emergent" ? " emergent" : ""),
      `ESI ${card.acuity} · ${card.bucket}`);
    chip.title = `acuity decided by: ${SOURCE_LABELS[card.acuity_source] || card.acuity_source}`;
    chips.append(chip, el("span", "chip", SOURCE_LABELS[card.acuity_source] || card.acuity_source));
  } else {
    chips.append(el("span", "chip", "not yet triaged"));
  }
  if (card.gate_pending) chips.append(el("span", "chip gate", "awaiting charge nurse"));
  card.degraded.forEach((d) => chips.append(el("span", "chip degraded", plain(DEGRADED_LABELS, d))));
  card.flags.forEach((f) => chips.append(el("span", "chip", plain(FLAG_LABELS, f))));
  node.append(chips);

  node.onclick = () => openPanel(card.case_id);
  return node;
}

function renderBoard(data) {
  const main = document.getElementById("columns");
  const byStatus = {};
  data.columns.forEach((c) => (byStatus[c] = []));
  data.cards.forEach((c) => (byStatus[c.status] ||= []).push(c));

  main.replaceChildren(...data.columns.filter((c) => c !== DRAWER_COLUMN).map((status) => {
    const cards = byStatus[status] || [];
    const col = el("div", "column" + (cards.length ? "" : " empty-col"));
    const head = el("h2");
    head.append(el("span", null, COLUMN_LABELS[status] || status),
                el("span", null, String(cards.length)));
    col.append(head);
    if (!cards.length && NOT_YET_WRITTEN[status]) {
      col.append(el("div", "todo", NOT_YET_WRITTEN[status]));
    }
    cards.forEach((c) => col.append(renderCard(c, data.red_after_min)));
    return col;
  }));

  const released = byStatus[DRAWER_COLUMN] || [];
  document.getElementById("released").replaceChildren(
    ...(released.length ? released.map((c) => renderCard(c, data.red_after_min))
                        : [el("div", "todo", "no patients released yet")]));
}

function kv(pairs) {
  const box = el("div", "kv");
  pairs.forEach(([k, v]) => {
    box.append(el("div", "k", k), el("div", "v", v === null || v === undefined ? "—" : String(v)));
  });
  return box;
}

function readable(text) {
  // Explanations are written for a log, so a few spec tokens leak through.
  return Object.entries({ ...SOURCE_LABELS, ...OUTCOME_LABELS }).reduce(
    (s, [token, label]) => s.split(token).join(label), text || "");
}

function trail(records) {
  const table = el("table", "trail");
  records.slice().reverse().forEach((r) => {
    // An action mapped to "" is one whose explanation already reads as a
    // sentence — `emit_event_log — cleared to queue` says nothing extra.
    const said = r.action in ACTION_LABELS ? ACTION_LABELS[r.action] : r.action;
    const explanation = readable(r.explanation);
    const tr = el("tr");
    // The arrow and the raw action name are what tie a row back to the
    // Transitions table. They belong in the tooltip, not in a nurse's line of
    // sight — the row itself is one sentence about what happened.
    tr.title = `${r.arrow ? "arrow " + r.arrow + " · " : ""}${r.action}`;
    tr.append(el("td", "at", (r.at || "").slice(11, 19)),
              el("td", null, said && explanation ? `${said} — ${explanation}`
                                                 : said || explanation || r.action));
    table.append(tr);
  });
  return table;
}

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

  const canRelease = card && ["waiting", "treatment_started"].includes(card.status);

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

  controls.append(moveBtn, releaseGroup);
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

  const msg = el("div", "msg");
  const options = el("div", "controls");
  (GATE_OPTIONS[reason] || []).forEach((decision) => {
    const btn = el("button", null, decisionLabels[decision] || decision);
    btn.onclick = async () => {
      options.querySelectorAll("button").forEach((b) => (b.disabled = true));
      msg.className = "msg";
      msg.textContent = "resolving…";
      try {
        const res = await fetch(`${INTAKE_CHANNEL_ORIGIN}/resume/${encodeURIComponent(caseId)}`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ decision, resolver_role: role.value }),
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
        msg.textContent = "network error — is intake-channel running?";
        options.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    };
    options.append(btn);
  });

  box.append(el("label", null, "Resolver role"), role, options, msg);
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

  const complaint = el("input");
  complaint.type = "text";
  complaint.placeholder = "chief complaint";

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
      const res = await fetch(`${INTAKE_CHANNEL_ORIGIN}/reassess/${encodeURIComponent(caseId)}`, {
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
      msg.textContent = "network error — is intake-channel running?";
      submit.disabled = false;
    }
  };

  return box;
}

async function openPanel(caseId) {
  selected = caseId;
  // A case stays reachable by link — including one that has left the board.
  location.hash = "case-" + caseId;
  const panel = document.getElementById("panel");
  const body = document.getElementById("panel-body");
  panel.hidden = false;
  panel.classList.add("open");
  body.replaceChildren(el("p", "meta", "loading…"));

  const res = await fetch(`/api/case/${encodeURIComponent(caseId)}`);
  if (!res.ok) {
    body.replaceChildren(el("p", "meta", `no case ${caseId}`));
    return;
  }
  const { view, card, checkpoints } = await res.json();

  const mainInfo = [
    ["waiting", card ? formatWait(card.waited_min) : "—"],
    ["queue position", card && card.position ? card.position : "—"],
    ["on the board in", card ? plain(COLUMN_LABELS, card.status) : "off the board"],
    ["right now", plain(STAGE_LABELS, view.control_state)],
    ["the submission", plain(OUTCOME_LABELS, view.outcome, "not read yet")],
    ["safety check", view.safety_passed ? "passed" : "not passed"],
    ["cleared to the queue", view.approved ? "yes" : "not yet"],
    ["saved steps", checkpoints],
  ];
  if (view.release_reason) {
    mainInfo.push(["release reason", plain(RELEASE_REASON_LABELS, view.release_reason)]);
  }

  body.replaceChildren(
    el("h2", null, (card && card.complaint) || caseId),
    el("div", "meta",
       `patient ${card && card.patient_id ? card.patient_id : "unknown"}`
       + `${card ? " · " + card.patient_label : ""} · case ${caseId}`),
    kv(mainInfo),
  );

  const acuity = el("div", "section");
  acuity.append(el("h3", null, "Triage level (ESI 1 = most acute)"), kv([
    ["nurse proposed", card ? card.nurse_proposed_acuity : null],
    ["system proposed", card ? card.system_proposed_acuity : null],
    ["gap between them", view.acuity_gap],
    ["final level", view.acuity],
    ["decided by", SOURCE_LABELS[view.acuity_source] || view.acuity_source],
    ["queue bucket", card ? card.bucket : null],
  ]));
  body.append(acuity);

  body.append(movesSection(caseId, card));
  if (view.status === "awaiting_human_approval") body.append(gatePanel(caseId, view, card));
  if (view.control_state === "reassessment_required") body.append(refilePanel(caseId));

  const trailBox = el("div", "section");
  trailBox.append(el("h3", null, "Audit trail — everything that happened to this case"),
                  trail(view.audit_log || []));
  body.append(trailBox);
}

function closePanel() {
  selected = null;
  if (location.hash) history.replaceState(null, "", location.pathname);
  const panel = document.getElementById("panel");
  panel.classList.remove("open");
  panel.hidden = true;
}

async function refresh() {
  try {
    const data = await (await fetch("/api/board")).json();
    renderCounters(data.counters);
    renderNotifications(data.notifications);
    renderBoard(data);
    // After the render, so the first load highlights nothing.
    seen = new Set(data.cards.map((c) => c.case_id));
    if (selected) await refreshMovesSection(selected);
  } catch (err) {
    // A failed poll is not a reason to blank a clinical board: keep the last
    // render on screen and try again on the next tick.
    console.error("board refresh failed", err);
  }
}

// The open panel's "Start treatment"/"Release patient" buttons are built
// from a one-time card snapshot (see movesSection) and otherwise never
// re-evaluated while the panel stays open — a status change elsewhere
// (another nurse, another tab) would leave a stale-enabled button showing.
// Re-fetching and swapping just this section on every poll, rather than the
// whole panel via openPanel(), avoids a "loading…" flash and doesn't wipe
// the audit trail or scroll position every 5s.
async function refreshMovesSection(caseId) {
  const oldMoves = document.querySelector("#panel-body .moves");
  if (!oldMoves) return;
  const res = await fetch(`/api/case/${encodeURIComponent(caseId)}`);
  if (!res.ok) return;
  const { card } = await res.json();
  const prevReason = oldMoves.querySelector(".reason-select")?.value;
  const freshMoves = movesSection(caseId, card);
  if (prevReason) freshMoves.querySelector(".reason-select").value = prevReason;
  oldMoves.replaceWith(freshMoves);
}

document.getElementById("panel-close").onclick = closePanel;
document.getElementById("notif-bell").onclick = toggleNotifPanel;
document.getElementById("notif-panel-close").onclick = closeNotifPanel;
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closePanel();
  closeNotifPanel();
});
if (location.hash.startsWith("#case-")) openPanel(location.hash.slice("#case-".length));
refresh();
setInterval(refresh, REFRESH_MS);
