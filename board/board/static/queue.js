// The queue: counters, notifications, cards and columns, rendered from one
// /api/board payload.

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

// A notification has no id of its own: the list is rebuilt from the audit log
// and the monitor feed on every poll. These fields never change for one event.
const notifKey = (n) => n.source
  ? [n.case_id, n.source, n.kind, n.schedule_seq, n.at].join("|")
  : [n.case_id, n.transition, n.at].join("|");

// Dismissed notifications, per browser. Clearing one is a personal "seen it",
// not a clinical action, so it never reaches the server and another nurse's
// screen still shows it. Storage can be blocked (private window, cleared site
// data); then a dismissal lasts until reload.
const DISMISSED_KEY = "triage-guard.dismissed-notifications";
let dismissed = new Set();
try { dismissed = new Set(JSON.parse(localStorage.getItem(DISMISSED_KEY)) || []); } catch {}
let lastNotifications = [];

function dismissNotification(n) {
  dismissed.add(notifKey(n));
  try { localStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissed])); } catch {}
  renderNotifications(lastNotifications);
}

function renderNotifications(items) {
  lastNotifications = items;
  items = items.filter((n) => !dismissed.has(notifKey(n)));
  const badge = document.getElementById("notif-badge");
  badge.textContent = String(items.length);
  badge.hidden = items.length === 0;

  const box = document.getElementById("notif-list");
  if (!items.length) {
    box.replaceChildren(el("div", "notif-item", "no notifications"));
    return;
  }
  box.replaceChildren(...items.map((n) => {
    // An escalation borrows the red `.blk` treatment: both mean the system
    // refused something or could not do it, which is what a nurse needs to
    // spot first.
    const escalated = n.source === "escalation";
    const node = el("div", "notif-item"
      + (n.transition === "blk" || escalated ? " blk" : "")
      + (n.source === "reminder" ? " nudge" : ""));
    const clear = el("button", "clear", "✕");
    clear.setAttribute("aria-label", "Clear notification");
    // Clearing must not also open the case panel behind it.
    clear.onclick = (e) => { e.stopPropagation(); dismissNotification(n); };
    node.append(clear,
                el("div", "transition", n.source ? nudgeLabel(n) : plain(NOTICE_LABELS, n.transition)),
                el("div", "complaint", n.complaint || ""),
                el("div", "at", n.at));
    node.title = n.source
      ? `${n.case_id} · ${n.source} · ${nudgeLabel(n)} · ${n.at}`
      : `${n.case_id} · transition ${n.transition} · ${n.action} · ${readable(n.explanation)} · ${n.at}`;
    // Clicking a notification opens the case panel but leaves this sidebar
    // open — only the sidebar's close button closes it.
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

const WAIT_LABELS = {
  treatment_started: "Treatment started",
  patient_released: "Patient released",
};

function waitLabel(card) {
  return (card && WAIT_LABELS[card.status]) || "waiting";
}

// "waiting" is ongoing, so it takes no "ago"; the other two labels name a
// past event, so "ago" reads correctly after their elapsed time.
function waitSuffix(card) {
  return card && WAIT_LABELS[card.status] ? " ago" : "";
}

function isRed(card, thresholds) {
  if (card.status in WAIT_LABELS) return false;
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
                  waitLabel(card) + " " + formatWait(card.waited_min) + waitSuffix(card));
  const who = el("div", "patient-line",
                 `${card.patient_label} · patient ${card.patient_id || "unknown"}`);
  meta.append(wait, who);
  node.append(meta);

  const chips = el("div", "chips");
  if (card.acuity !== null && card.acuity !== undefined) {
    const chip = el("span", "chip acuity" + (card.bucket === "emergent" ? " emergent" : ""),
      `ESI ${card.acuity} · ${card.bucket}`);
    chip.title = `acuity decided by: ${SOURCE_LABELS[card.acuity_source] || card.acuity_source}`;
    chips.append(chip, el("span", "chip", SOURCE_LABELS[card.acuity_source] || card.acuity_source));
  }
  // ESI decision point D: shown exactly as computed, and it changes no level —
  // the nurse and the classifier weigh it (SPECIFICATION.md § Safety invariants).
  if (card.danger_zone_vitals && card.danger_zone_vitals.length) {
    const danger = el("span", "chip danger", `vitals: ${card.danger_zone_vitals.join(", ")}`);
    danger.title = "outside the ESI danger-zone limits for this age band — annotation only";
    chips.append(danger);
  }
  if (card.acuity === null || card.acuity === undefined) {
    chips.append(el("span", "chip", "not yet triaged"));
  }
  if (card.gate_pending) {
    chips.append(el("span", "chip gate",
      card.senior_required ? "awaiting shift lead" : "awaiting charge nurse"));
  }
  if (card.reminders) {
    chips.append(el("span",
      "chip " + (card.reminders.source === "escalation" ? "escalated" : "nudge"),
      `${nudgeLabel(card.reminders)} ${formatWait(card.reminders.elapsed_min)} ago`));
  }
  card.degraded.forEach((d) => chips.append(el("span", "chip degraded", plain(DEGRADED_LABELS, d))));
  card.flags.forEach((f) => chips.append(el("span", "chip", plain(FLAG_LABELS, f))));
  node.append(chips);

  node.onclick = () => openPanel(card.case_id);
  return node;
}

// The complaint vocabulary, from /api/board. Served rather than copied so the
// re-file form cannot drift from what intake accepts.
let chiefComplaints = [];

function renderBoard(data) {
  chiefComplaints = data.chief_complaints || chiefComplaints;
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
