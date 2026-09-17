"""FastAPI server for the triage board — the read-only dashboard nurses watch,
as opposed to intake-channel (the separate service that accepts new cases and
answers gate questions).

    GET  /                            the board itself
    GET  /api/board                   columns, cards, counters — one call per refresh
    GET  /api/case/{case_id}          detail panel: the shared case view + its trail
    GET  /api/health
    GET  /api/heartbeat               is the background sweeper process still alive?
    POST /api/case/{case_id}/deteriorated       nurse-initiated DETERIORATION_DETECTED
    POST /api/case/{case_id}/move-to-treatment  nurse-initiated MOVE_REQUESTED
    POST /api/case/{case_id}/release            nurse-initiated RELEASE_REQUESTED

Read-only by design, with three deliberate exceptions — `/deteriorated`,
`/move-to-treatment`, and `/release`. None of them write case state
directly: each re-enters that case's paused LangGraph run with a
`Command(resume=...)`, the same mechanism intake-channel's own `/resume`
endpoint uses. The real authorization check for a move or a release
(`move_authorized` / `release_authorized`) runs inside the graph node that
receives the resume, not here — a request this layer accepts can still come
back refused, the same way `/deteriorated` already can.

This is the minimal version: both new endpoints only work while a case is
genuinely parked in the waiting-room pause (`control_state == monitoring`),
not from the human-approval gate or the reassessment re-file pause. See
docs/superpowers/plans/2026-09-17-treatment-move-and-release/findings.md
for why those two are out of scope here, and docs/STATUS.md item 4 for the
full treatment-move execution machine (Tool Gateway, idempotency,
reconciliation) this version deliberately skips.

This server issues no other writes, but the page it serves does: a case
sitting at `reassessment_required` shows a re-filing form in its detail
panel that calls `intake-channel`'s `POST /reassess/{case_id}` directly from
the browser, not through this backend — a second front door for the same
"answer a paused case" action `/deteriorated` and intake-channel's own
`/resume` already do, just reached from here instead of the intake form.

Run:
    uv run board
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

from app import runner
from app.budgets import HEARTBEAT_STALE_MULTIPLIER
from app.labels import Arrow
from app.monitor import timers
from app.monitor.sweeper import SWEEP_INTERVAL_SECONDS
from app.runner import config_for, history
from app.states import AcuityBucket, ClinicalStatus, State
from app.views import BOARD_COLUMNS, CaseCard, card_from_state, case_view

from .ordering import positions, sort_cards
from .repo import CheckpointRepo

load_dotenv()

STATIC_DIR = Path(__file__).with_name("static")

# Minutes a case can wait before its card turns red in the UI. No SLA
# threshold has been defined yet, so these are placeholders kept in one spot
# instead of scattered as magic numbers in the frontend JS.
# ponytail: replace with real SLA numbers (and per-acuity-band ones, if the
# data supports it) once real wait-time data exists to set them from.
RED_AFTER_MIN = {AcuityBucket.EMERGENT.value: 15, AcuityBucket.QUEUED.value: 60}

# Which case events show up in the notification strip along the top of the
# board. Every transition a case makes gets logged with an "arrow" — a short
# code identifying which step just happened (e.g. "missing fields", "gate
# reminder"). Only the ones a nurse actually needs to act on or notice are
# listed here. Deliberately excluded: cleared-to-queue (`11·pass`) — every
# normal case emits that one, so including it would add a "nothing wrong"
# entry per card to a strip meant to surface exceptions, not routine success.
NOTIFY_ARROWS = {
    Arrow.MISSING_FIELDS.value,        # intake was missing required fields
    Arrow.SUBMISSION_UNUSABLE.value,   # scan failed / erroneous file
    Arrow.INVALID_INPUT.value,         # injection attempt refused
    Arrow.APPROVAL_REQUESTED.value,    # a charge nurse's approval was requested
    Arrow.ESCALATION_RECORDED.value,   # a charge nurse answered an escalation
    Arrow.REASSESSMENT_DUE.value,      # a reassessment timer fired
    Arrow.MOVE_CONFIRMED.value,        # move to treatment confirmed
    Arrow.BLK.value,                   # an attempted action was refused — worth seeing most
}

app = FastAPI(title="Triage Guard — board", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

repo = CheckpointRepo()


def counters(cards: list[CaseCard]) -> dict[str, int | float]:
    """The numbers a charge nurse scans first. All derived, none stored."""
    waits = [c.waited_min for c in cards if c.status == ClinicalStatus.WAITING.value]
    return {
        "waiting": len(waits),
        "human_review": sum(c.status == ClinicalStatus.HUMAN_REVIEW.value for c in cards),
        "reassessment_required": sum(
            c.status == ClinicalStatus.REASSESSMENT_REQUIRED.value for c in cards
        ),
        "emergent": sum(c.bucket == AcuityBucket.EMERGENT.value for c in cards),
        "gate_pending": sum(c.gate_pending for c in cards),
        "longest_wait_min": max(waits, default=0),
        "avg_wait_min": round(sum(waits) / len(waits), 1) if waits else 0,
        "monitor_degraded": heartbeat_status()["degraded"],
    }


def heartbeat_status() -> dict:
    """Whether the background sweeper process (which fires reassessment
    timers) is still alive, checked here rather than trusted from the sweeper
    itself — a dead process can't self-report. Reads the same SQLite file the
    timers live in, opened read-write like the rest of `timers.py`, even
    though the board itself never writes to it.
    """
    return timers.heartbeat_status(
        timers.connection(), stale_after_seconds=HEARTBEAT_STALE_MULTIPLIER * SWEEP_INTERVAL_SECONDS
    )


def notifications(states: list[dict]) -> list[dict]:
    """The notification strip along the top of the board, built directly from
    each case's audit log instead of a separate notification table.

    Every event worth surfacing (missing fields, a failed scan, an injection
    refusal, an approval request, a transition accepted) is already recorded
    as an audit-log entry tagged with an "arrow" code, so filtering
    `audit_log` by `NOTIFY_ARROWS` is enough — there's no second store that
    could drift out of sync with the case that caused the entry.
    """
    records = [
        {
            "case_id": rec.get("case_id") or state.get("case_id"),
            # The patient's chief complaint, so a notification says which
            # patient it's about without anyone memorizing case ids. Checked
            # in order: redacted payload, then parsed fields, then the raw
            # payload. The cases that generate the most notifications
            # (missing fields, unusable scan, refused input) are exactly the
            # ones that failed before a redacted payload was ever built, and
            # a notification reading only "invalid input" helps nobody. Only
            # the complaint text is ever read from the raw payload — never a
            # patient identifier.
            "complaint": (
                (state.get("redacted_payload") or {}).get("chief_complaint")
                or (state.get("parsed_fields") or {}).get("chief_complaint")
                or (state.get("raw_payload") or {}).get("chief_complaint")
                or ""
            ),
            "at": rec.get("at"),
            "arrow": rec.get("arrow"),
            "action": rec.get("action"),
            "explanation": rec.get("explanation"),
        }
        for state in states
        for rec in state.get("audit_log", [])
        if rec.get("arrow") in NOTIFY_ARROWS
    ]
    records.sort(key=lambda r: r.get("at") or "", reverse=True)
    return records[:12]


def board_payload() -> dict:
    """Everything the page needs in one response."""
    scanned = repo.scan()
    states = [s for s, _ in scanned]
    cards = sort_cards([c for c in (card_from_state(s, None, g) for s, g in scanned) if c])
    place = positions(cards)
    return {
        "columns": BOARD_COLUMNS,
        "counters": counters(cards),
        "red_after_min": RED_AFTER_MIN,
        "notifications": notifications(states),
        "cards": [c.model_dump() | {"position": place.get(c.case_id)} for c in cards],
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/heartbeat")
def heartbeat():
    """Whether the background sweeper is alive, as its own endpoint so the
    frontend can poll it independently of `/api/board`. This just adds one
    more query against the same SQLite file `/api/board` already reads case
    data from.
    """
    return heartbeat_status()


@app.get("/api/board")
def board():
    """One payload, one render. Polled; no CRM call anywhere in this path, so a
    CRM outage cannot blank the board.
    """
    return board_payload()


class DeteriorationReport(BaseModel):
    signal: str
    actor_role: str = "nurse"


def _waiting_snapshot(case_id: str):
    """Fetch a case's graph handle + config, refusing (404/409) unless it's
    genuinely parked in the waiting-room pause (`control_state == monitoring`
    — the same fact `app.monitor.fire.dispatch` checks before firing a
    reassessment timer). Shared by `deteriorated`, `move_to_treatment`, and
    `release` — the only three endpoints that resume a paused run.
    """
    g = runner.graph()
    config = config_for(case_id)
    snapshot = g.get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    if snapshot.values.get("control_state") != State.MONITORING.value:
        raise HTTPException(status_code=409, detail="case is not currently waiting in the queue")
    return g, config, snapshot


@app.post("/api/case/{case_id}/deteriorated")
def deteriorated(case_id: str, report: DeteriorationReport):
    """Lets a nurse report that a patient's condition is worsening while they
    wait. This manual path stays useful even once a real vitals-monitoring
    feed exists — a human noticing something is always valid input. It
    re-enters the case's paused LangGraph run with a `Command(resume=...)`
    into that case's own thread, the same mechanism intake-channel's own
    `/resume` endpoint uses, rather than writing to case state directly.
    """
    g, config, _ = _waiting_snapshot(case_id)
    g.invoke(
        Command(resume={"event": "DETERIORATION_DETECTED", "signal": report.signal,
                         "actor_role": report.actor_role}),
        config,
    )
    return {"status": "ok"}


class MoveToTreatmentReport(BaseModel):
    actor_role: str = "nurse"


class ReleaseReport(BaseModel):
    reason: str
    actor_role: str = "nurse"


def _resume_waiting_case(case_id: str, resume: dict) -> dict:
    """Shared by `/move-to-treatment` and `/release`: re-enter a case's
    waiting-room pause with `Command(resume=...)`, refusing (404/409) unless
    it's genuinely parked there, then report whether the in-graph guard
    (`move_authorized` / `release_authorized`) actually accepted the action.

    A guard refusal is a normal outcome, not an HTTP error — the request was
    valid and processed, the graph just said no (same convention
    `/deteriorated` already follows) — so this still returns 200, with
    `status: "denied"` and the guard's own explanation, letting the caller
    (the board UI) tell "accepted" apart from "refused" instead of assuming
    every 200 means success.

    Reads the outcome from `g.invoke()`'s own return value, not a second
    `g.get_state()` call — two near-simultaneous requests for the same case
    (a double-click, or two nurses) can otherwise interleave, and a second
    `get_state()` risks reading whichever request's audit row landed last
    rather than this call's own. If the audit log didn't grow at all, this
    request's resume never actually applied — another request already
    consumed the pending interrupt between our guard check and this
    `invoke()` — so it's reported the same way as "case not currently
    waiting" rather than a false "ok".
    """
    g, config, snapshot = _waiting_snapshot(case_id)
    before = len(snapshot.values.get("audit_log") or [])

    result = g.invoke(Command(resume=resume), config)

    after_log = result.get("audit_log") or []
    if len(after_log) <= before:
        raise HTTPException(status_code=409, detail="case already left the waiting-room pause")

    last = after_log[-1]
    if last.get("arrow") == Arrow.BLK.value:
        return {"status": "denied", "detail": last.get("explanation")}
    return {"status": "ok"}


@app.post("/api/case/{case_id}/move-to-treatment")
def move_to_treatment(case_id: str, report: MoveToTreatmentReport):
    """Nurse-initiated move into treatment. Same shape as `/deteriorated`:
    re-enters the paused run rather than writing case state directly.
    Only works from the waiting-room pause (see
    docs/superpowers/plans/2026-09-17-treatment-move-and-release/
    findings.md: only that pause is wired in this version).
    """
    return _resume_waiting_case(
        case_id, {"event": "MOVE_REQUESTED", "actor_role": report.actor_role}
    )


@app.post("/api/case/{case_id}/release")
def release(case_id: str, report: ReleaseReport):
    """Nurse-initiated release. Same shape as `/deteriorated` and
    `/move-to-treatment` above. The real authorization check
    (`release_authorized`, charge-role + valid reason) runs inside the
    graph node, not here.
    """
    return _resume_waiting_case(
        case_id,
        {"event": "RELEASE_REQUESTED", "reason": report.reason, "actor_role": report.actor_role},
    )


@app.get("/api/case/{case_id}")
def case(case_id: str):
    """The case detail panel: the same view intake-channel renders for this
    case, plus how many checkpoints (state snapshots) it has. The event
    trail is `view["audit_log"]`, already formatted as
    `at · arrow · action · explanation` by `deterministic.audit()`.
    """
    values, gated = repo.load(case_id)
    if not values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    # `gated` means the run is currently paused waiting for a charge nurse's
    # decision. A paused run hasn't written any gate-related fields to its
    # state yet — the pause itself only shows up as a pending task on the
    # checkpoint. The panel just needs to know it's paused; actually
    # answering the gate is intake-channel's job, not the board's.
    pending = {"gate": values.get("escalation_reason") or "awaiting charge nurse"} if gated else None
    card = card_from_state(values, None, gated)
    # A case's position in the queue isn't stored on the case itself — it can
    # only be computed by comparing it against every other card, the same way
    # /api/board computes positions for the whole board.
    place = positions(repo.cards()).get(case_id) if card else None
    return {
        "view": case_view(values, pending),
        "card": (card.model_dump() | {"position": place}) if card else None,
        "checkpoints": len(history(case_id)),
    }


def run() -> None:
    """`uv run board` — serve on BOARD_HOST / BOARD_PORT."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("BOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("BOARD_PORT", "8002")),
    )
