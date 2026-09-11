// The board UI. One fetch of /api/board every 5s, one render.
//
// No framework and no build step — the page is a projection of one JSON payload,
// and SSE is a later swap behind the same shape. Nothing here decides ordering or
// position: both arrive from the server, which reads the persisted order_key.
// ponytail: revisit the no-framework call if this file passes ~400 lines.

const REFRESH_MS = 5000;
const COLUMN_LABELS = {
  waiting: "Waiting",
  human_review: "Human review",
  reassessment_required: "Reassessment required",
  treatment_started: "Treatment started",
  formal_validation: "Formal validation",
  patient_released: "Released",
};
// Nothing writes these yet (docs/STATUS.md items 4 / 5a / 5b). The column is
// rendered anyway, with the reason — an empty column is the honest state.
const NOT_YET_WRITTEN = {
  reassessment_required: "not in use yet — the waiting-room timers are not built",
  treatment_started: "not in use yet — moving a patient into treatment is not built",
  formal_validation: "not in use yet — discharge and release are not built",
  patient_released: "not in use yet — discharge and release are not built",
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
  rule_forced: "forced by red-flag rule",
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
  const box = document.getElementById("notifications");
  if (!items.length) {
    box.replaceChildren(el("div", "note", "no notifications"));
    return;
  }
  box.replaceChildren(...items.map((n) => {
    const node = el("div", "note" + (n.arrow === "BLK" ? " blk" : ""));
    node.append(el("span", "arrow", plain(NOTICE_LABELS, n.arrow)),
                document.createTextNode(n.complaint ? "  " + n.complaint : ""));
    node.title = `${n.case_id} · arrow ${n.arrow} · ${n.action} · ${readable(n.explanation)} · ${n.at}`;
    node.onclick = () => openPanel(n.case_id);
    return node;
  }));
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
  if (card.red_flag_fired) chips.append(el("span", "chip redflag", "red flag"));
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
                        : [el("div", "todo", NOT_YET_WRITTEN[DRAWER_COLUMN])]));
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

function movesSection() {
  const box = el("div", "section moves");
  box.append(el("h3", null, "Manual status change"));
  ["Start treatment", "Release patient"].forEach((label) => {
    const b = el("button", null, label);
    b.disabled = true;
    b.title = "Milestone M2 — needs the treatment-move machine (STATUS item 4)";
    box.append(b);
  });
  box.append(el("div", "why",
    "Disabled: the board issues no writes yet. Every transition goes through the "
    + "graph, so these arrive with the treatment-move and release steps, not before."));
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

  body.replaceChildren(
    el("h2", null, (card && card.complaint) || caseId),
    el("div", "meta",
       `patient ${card && card.patient_id ? card.patient_id : "unknown"}`
       + `${card ? " · " + card.patient_label : ""} · case ${caseId}`),
    kv([
      ["waiting", card ? formatWait(card.waited_min) : "—"],
      ["queue position", card && card.position ? card.position : "—"],
      ["on the board in", card ? plain(COLUMN_LABELS, card.status) : "off the board"],
      ["right now", plain(STAGE_LABELS, view.control_state)],
      ["the submission", plain(OUTCOME_LABELS, view.outcome, "not read yet")],
      ["safety check", view.safety_passed ? "passed" : "not passed"],
      ["cleared to the queue", view.approved ? "yes" : "not yet"],
      ["saved steps", checkpoints],
    ]),
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

  body.append(movesSection());

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
  } catch (err) {
    // A failed poll is not a reason to blank a clinical board: keep the last
    // render on screen and try again on the next tick.
    console.error("board refresh failed", err);
  }
}

document.getElementById("panel-close").onclick = closePanel;
document.addEventListener("keydown", (e) => e.key === "Escape" && closePanel());
if (location.hash.startsWith("#case-")) openPanel(location.hash.slice("#case-".length));
refresh();
setInterval(refresh, REFRESH_MS);
