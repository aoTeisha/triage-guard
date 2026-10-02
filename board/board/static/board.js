// The board UI. One fetch of /api/board every 5s, one render.
//
// No framework and no build step — the page is a projection of one JSON payload,
// and SSE is a later swap behind the same shape. Nothing here decides ordering or
// position: both arrive from the server, which reads the persisted order_key.
//
// Split across plain <script> tags (see index.html) that share one global
// scope, loaded in order; this file is the entry point and loads last.

async function refresh() {
  try {
    const data = await (await fetch("/api/board")).json();
    renderCounters(data.counters);
    renderOutages(data.outages);
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

document.getElementById("panel-close").onclick = closePanel;
document.getElementById("notif-bell").onclick = toggleNotifPanel;
document.getElementById("notif-panel-close").onclick = closeNotifPanel;
document.getElementById("new-case-btn").onclick = toggleIntakePanel;
document.getElementById("health-btn").onclick = () => {
  const panel = document.getElementById("health-panel");
  panel.hidden = !panel.hidden;
};
document.getElementById("health-panel-close").onclick = () => {
  document.getElementById("health-panel").hidden = true;
};
document.getElementById("intake-panel-close").onclick = closeIntakePanel;
document.getElementById("submit-btn").onclick = submitNewCase;
document.getElementById("lookup-btn").onclick = runLookup;
document.getElementById("patient-id").addEventListener("change", () => {
  clearLookup();
  updateSubmitEnabled();
  if (caseMode() === "demo") renderPayloadPreview();
});
document.getElementById("intake-panel").addEventListener("change", (e) => {
  if (e.target.name === "case_mode" || e.target.name === "submission_type"
      || e.target.id === "violation") updateModeFields();
  if (e.target.closest("#real-inputs") || e.target.name === "case_mode") updateSubmitEnabled();
});
document.getElementById("intake-panel").addEventListener("input", (e) => {
  if (e.target.closest("#real-inputs")) updateSubmitEnabled();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closePanel();
  closeNotifPanel();
  closeIntakePanel();
});
if (location.hash.startsWith("#case-")) openPanel(location.hash.slice("#case-".length));
refresh();
setInterval(refresh, REFRESH_MS);
