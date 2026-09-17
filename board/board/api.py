"""FastAPI server for the triage board — the read-only dashboard nurses watch,
as opposed to intake-channel (the separate service that accepts new cases and
answers gate questions).

    GET  /                            the board itself
    GET  /api/board                   columns, cards, counters — one call per refresh
    GET  /api/case/{case_id}          detail panel: the shared case view + its trail
    GET  /api/health
    GET  /api/heartbeat               is the background sweeper process still alive?
    POST /api/case/{case_id}/deteriorated   nurse-initiated DETERIORATION_DETECTED

Read-only by design, with one exception. `POST /move` and `/release` (moving a
case into treatment, or releasing it) are not built yet — the code that would
manage those transitions doesn't exist (see docs/STATUS.md item 4) — and until
it does, this board must not become a second place that writes case state on
top of it. The UI shows those controls disabled and there is no endpoint
behind them.

`/deteriorated` is the deliberate exception: a nurse reports the patient's
condition worsening while still waiting. It does not write case state
directly — it re-enters that case's paused LangGraph run with a
`Command(resume=...)`, the same mechanism intake-channel's own `/resume`
endpoint uses.

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


@app.post("/api/case/{case_id}/deteriorated")
def deteriorated(case_id: str, report: DeteriorationReport):
    """Lets a nurse report that a patient's condition is worsening while they
    wait. This manual path stays useful even once a real vitals-monitoring
    feed exists — a human noticing something is always valid input. It
    re-enters the case's paused LangGraph run with a `Command(resume=...)`
    into that case's own thread, the same mechanism intake-channel's own
    `/resume` endpoint uses, rather than writing to case state directly.
    """
    g = runner.graph()
    config = config_for(case_id)
    snapshot = g.get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    # This only makes sense while the case is genuinely paused waiting in the
    # queue: `control_state` is set to `monitoring` and stays there exactly
    # while a run is suspended at that pause point — the same fact
    # `app.monitor.fire.dispatch` checks before firing a reassessment timer.
    if snapshot.values.get("control_state") != State.MONITORING.value:
        raise HTTPException(status_code=409, detail="case is not currently waiting in the queue")

    g.invoke(
        Command(resume={"event": "DETERIORATION_DETECTED", "signal": report.signal,
                         "actor_role": report.actor_role}),
        config,
    )
    return {"status": "ok"}


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
