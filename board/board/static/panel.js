// The case detail panel: what happened to one case, and its audit trail.

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

// The after-run trace check re-reads this case's own audit log and reports a
// rule it found broken. It only ever reports — it never blocked or changed
// the case — so seeing this means a guard elsewhere had a bug.
function traceAlert(violations) {
  const box = el("div", "section trace-alert");
  box.append(el("h3", null, "Trace check found a problem"));
  const list = el("ul");
  (violations || []).forEach((v) => {
    const item = el("li");
    const rule = Object.keys(TRACE_RULE_LABELS).find((r) => v.includes(r.replace(/_/g, " ")));
    if (rule) item.append(el("b", null, TRACE_RULE_LABELS[rule]), " — ");
    item.append(v);
    list.append(item);
  });
  box.append(list);
  return box;
}

// Z3 proves the acuity-gap rules once, ahead of time — it doesn't run per
// case — so its chip gets a tooltip saying that instead of the plain "engine
// that decided this row" reading the others carry.
const ENGINE_TITLES = {
  Z3: "gap rules proven by Z3 ahead of time, not re-run per case",
};

function engineChips(engines) {
  const box = el("span", "engines");
  (engines || []).forEach((name) => {
    const chip = el("span", "engine-chip", name);
    if (ENGINE_TITLES[name]) chip.title = ENGINE_TITLES[name];
    box.append(chip);
  });
  return box;
}

function trail(records) {
  const table = el("table", "trail");
  records.slice().reverse().forEach((r) => {
    // An action mapped to "" is one whose explanation already reads as a
    // sentence — `emit_event_log — cleared to queue` says nothing extra.
    const said = r.action in ACTION_LABELS ? ACTION_LABELS[r.action] : r.action;
    const explanation = readable(r.explanation);
    const tr = el("tr");
    // The transition and the raw action name are what tie a row back to the
    // Transitions table. They belong in the tooltip, not in a nurse's line of
    // sight — the row itself is one sentence about what happened.
    tr.title = `${r.transition ? "transition " + r.transition + " · " : ""}${r.action}`;
    const line = el("td");
    line.append(said && explanation ? `${said} — ${explanation}`
                                    : said || explanation || r.action,
                engineChips(r.engines));
    tr.append(el("td", "at", (r.at || "").slice(11, 19)), line);
    table.append(tr);
  });
  return table;
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
    [waitLabel(card), card ? formatWait(card.waited_min) + waitSuffix(card) : "—"],
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
  // `case_view` has always carried this; the panel never showed it, so an
  // incomplete submission said only that details were missing, not which.
  if (view.missing_fields && view.missing_fields.length) {
    mainInfo.push(["details still missing", view.missing_fields.map(readable).join(", ")]);
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
  if (view.trace_safety === false) body.append(traceAlert(view.trace_violations));
  else body.append(el("div", "trace-ok", "Trace check: passed ✓ — the audit log breaks none of the safety rules"));

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
