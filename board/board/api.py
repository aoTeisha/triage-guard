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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel

from app import runner
from app.guards import CHIEF_COMPLAINTS
from app.budgets import HEARTBEAT_STALE_MULTIPLIER
from app.labels import Transition
from app.monitor import timers
from app.monitor.sweeper import SWEEP_INTERVAL_SECONDS
from app.observability import case_trace, record_outcome
from app.runner import case_history_check, config_for, history
from app.budgets import BOARD_FEED_WINDOW_MINUTES, BOARD_RED_AFTER_MINUTES
from app.states import AcuityBucket, ClinicalStatus, State
from app.views import BOARD_COLUMNS, CaseCard, card_from_state, case_view

from .ordering import positions, sort_cards
from .repo import CheckpointRepo

load_dotenv()

STATIC_DIR = Path(__file__).with_name("static")


# Which case events show up in the notification strip along the top of the
# board. Every transition a case makes gets logged under its own name (e.g.
# "missing_fields", "gate_reminder"). Only the ones a nurse actually needs to
# act on or notice are listed here. Deliberately excluded: CLEARED_TO_QUEUE —
# every normal case emits that one, so including it would add a "nothing
# wrong" entry per card to a strip meant to surface exceptions, not routine
# success.
NOTIFY_TRANSITIONS = {
    Transition.MISSING_FIELDS.value,        # intake was missing required fields
    Transition.SUBMISSION_UNUSABLE.value,   # scan failed / erroneous file
    Transition.INVALID_INPUT.value,         # injection attempt refused
    Transition.APPROVAL_REQUESTED.value,    # a charge nurse's approval was requested
    Transition.ESCALATION_RECORDED.value,   # a charge nurse answered an escalation
    Transition.REASSESSMENT_DUE.value,      # a reassessment timer fired
    Transition.MOVE_CONFIRMED.value,        # move to treatment confirmed
    Transition.BLK.value,                   # an attempted action was refused — worth seeing most
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
    itself — a dead process can't self-report. Reads the same Postgres
    database the timers live in, opened read-write like the rest of
    `timers.py`, even though the board itself never writes to it.
    """
    return timers.heartbeat_status(
        timers.connection(), stale_after_seconds=HEARTBEAT_STALE_MULTIPLIER * SWEEP_INTERVAL_SECONDS
    )


def _utc_iso(value: datetime) -> str:
    """Postgres hands back `timestamptz` in the session's timezone; the audit
    log's own timestamps are always UTC. Normalising here is what lets the two
    streams sort against each other.
    """
    return value.astimezone(timezone.utc).isoformat()


def _at_key(at: str | None) -> datetime:
    """Sort key for a record from either stream. Parsed rather than compared
    as a string: `sent_at` carries microseconds and `now_iso()` sometimes
    doesn't, and `+` sorts before `.`, which would order same-second records
    wrongly.
    """
    try:
        return datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _split_reason(reason: str) -> tuple[str, int | None]:
    """`"gate_reminder_1"` -> `("gate_reminder", 1)`, mirroring how `fire.py`
    builds the reason as `f"{kind}_{schedule_seq}"`.
    """
    kind, _, tail = reason.rpartition("_")
    return (kind, int(tail)) if kind and tail.isdigit() else (reason, None)


def monitor_feed() -> list[dict]:
    """Reminders and escalations the monitor recorded recently.

    Two queries per refresh, never one per card. This uses the connection
    `heartbeat_status()` already opens on every `/api/board` call, so it adds
    no new way for this endpoint to fail.
    """
    conn = timers.connection()
    window = BOARD_FEED_WINDOW_MINUTES
    feed = []
    for row in timers.recent_notifications(conn, window_minutes=window):
        kind, schedule_seq = _split_reason(row["reason"])
        feed.append({"source": "reminder", "case_id": row["case_id"], "kind": kind,
                     "schedule_seq": schedule_seq, "recipient_class": row["recipient_class"],
                     "at": _utc_iso(row["sent_at"])})
    for row in timers.recent_escalations(conn, window_minutes=window):
        feed.append({"source": "escalation", "case_id": row["case_id"], "kind": row["reason"],
                     "schedule_seq": None, "recipient_class": row["recipient_class"],
                     "at": _utc_iso(row["raised_at"])})
    return feed


def _nudge_rank(rec: dict) -> tuple[bool, str]:
    return (rec["source"] == "escalation", rec["at"])


def nudges_by_case(feed: list[dict], now: datetime) -> dict[str, dict]:
    """The one nudge worth putting on each card: an escalation outranks any
    reminder, and among reminders the newest wins — which is also the widest
    rung, because rungs only ever widen.
    """
    best: dict[str, dict] = {}
    for rec in feed:
        current = best.get(rec["case_id"])
        if current is None or _nudge_rank(rec) > _nudge_rank(current):
            best[rec["case_id"]] = rec
    return {
        case_id: rec | {"elapsed_min": max(0, int((now - _at_key(rec["at"])).total_seconds() // 60))}
        for case_id, rec in best.items()
    }


def _complaint(state: dict) -> str:
    """The patient's chief complaint, so a notification says which patient
    it's about without anyone memorizing case ids. Checked in order: redacted
    payload, then parsed fields, then the raw payload. The cases that generate
    the most notifications (missing fields, unusable scan, refused input) are
    exactly the ones that failed before a redacted payload was ever built, and
    a notification reading only "invalid input" helps nobody. Only the
    complaint text is ever read from the raw payload — never a patient
    identifier.
    """
    code = (
        (state.get("redacted_payload") or {}).get("chief_complaint")
        or (state.get("parsed_fields") or {}).get("chief_complaint")
        or (state.get("raw_payload") or {}).get("chief_complaint")
        or ""
    )
    return str(code).replace("_", " ")      # a code from the fixed set, shown as words


def notifications(states: list[dict], feed: list[dict] | None = None) -> list[dict]:
    """The notification strip along the top of the board, from two sources.

    Case events come from each case's audit log, filtered by `NOTIFY_TRANSITIONS`
    — every one of them is already recorded there by the node that caused it.
    Staff reminders and monitor escalations cannot come from there: the
    sweeper sends them without resuming the case, by design (`fire.notify`
    changes no case state), so they live in the monitor's own `notifications`
    and `escalations` tables and are joined here by `case_id`.

    That join is the drift this function used to avoid by reading one store:
    a feed row whose case has since closed still shows for the rest of the
    window. It is the accepted cost of surfacing a reminder at all — the
    alternative was writing to the checkpoint store from the sweeper, which
    would disturb the `snapshot.next` that both `repo.load()` and
    `fire._context()` read.
    """
    complaints = {(state.get("case_id") or ""): _complaint(state) for state in states}
    records = [
        {
            "case_id": rec.get("case_id") or state.get("case_id"),
            "complaint": _complaint(state),
            "at": rec.get("at"),
            "transition": rec.get("transition"),
            "action": rec.get("action"),
            "explanation": rec.get("explanation"),
        }
        for state in states
        for rec in state.get("audit_log", [])
        if rec.get("transition") in NOTIFY_TRANSITIONS
    ]
    records += [
        rec | {"complaint": complaints.get(rec["case_id"], "")} for rec in (feed or [])
    ]
    records.sort(key=lambda r: _at_key(r.get("at")), reverse=True)
    return records[:12]


def board_payload() -> dict:
    """Everything the page needs in one response."""
    scanned = repo.scan()
    states = [s for s, _ in scanned]
    cards = sort_cards([c for c in (card_from_state(s, None, g) for s, g in scanned) if c])
    place = positions(cards)
    feed = monitor_feed()
    nudges = nudges_by_case(feed, datetime.now(timezone.utc))
    return {
        "columns": BOARD_COLUMNS,
        # The re-file form offers these and nothing else: the complaint is a code
        # (I12), and the vocabulary has one home, app/guards/fields.py.
        "chief_complaints": list(CHIEF_COMPLAINTS),
        "counters": counters(cards),
        "red_after_min": BOARD_RED_AFTER_MINUTES,
        "notifications": notifications(states, feed),
        "cards": [
            c.model_dump()
            | {"position": place.get(c.case_id), "reminders": nudges.get(c.case_id)}
            for c in cards
        ],
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
    more query against the same Postgres database `/api/board` already reads
    the timer store from.
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
    reassessment timer). Shared by `deteriorated` and `move_to_treatment`;
    `release` uses `_paused_snapshot`, since a release may come from any pause.
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

    Routed through `_resume_waiting_case` (I22): this mutates the same case
    thread `/move-to-treatment` and `/release` do, through the same
    `g.invoke()` mechanism, so it needs the same `runner.case_lock` —
    without it, a deterioration report racing a concurrent release could hit
    a thread the release had already closed out from under it.
    """
    return _resume_waiting_case(
        case_id, {"event": "DETERIORATION_DETECTED", "signal": report.signal,
                  "actor_role": report.actor_role}
    )


class MoveToTreatmentReport(BaseModel):
    actor_role: str = "nurse"


class ReleaseReport(BaseModel):
    reason: str
    actor_role: str = "nurse"


def _paused_snapshot(case_id: str):
    """Like `_waiting_snapshot`, but for release (I9): any pause of a case that
    is not already closed qualifies, not just the waiting room.
    """
    g = runner.graph()
    config = config_for(case_id)
    snapshot = g.get_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    if snapshot.values.get("control_state") == State.CASE_CLOSED.value or not snapshot.next:
        raise HTTPException(status_code=409, detail="case is closed or not paused")
    return g, config, snapshot


def _resume_waiting_case(case_id: str, resume: dict, any_pause: bool = False) -> dict:
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
    `g.get_state()` call after invoking — a second post-invoke `get_state()`
    would risk reading whichever request's audit row landed last rather than
    this call's own. If the audit log didn't grow at all, this request's
    resume never actually applied — so it's reported the same way as "case
    not currently waiting" rather than a false "ok".

    I22: the pause-validity snapshot is taken *after* acquiring
    `runner.case_lock`, not before. Two near-simultaneous requests for the
    same case (a double-click, or two nurses) used to both pass this
    function's own guard while the case still looked paused to both, then
    race into `g.invoke()` — the loser's `g.invoke()` could land on a thread
    the winner had *already closed*, and LangGraph's `Command(resume=...)`
    on an ended thread with no pending task just returns the current (now
    fully-updated) state rather than erroring, so the loser's audit-log
    check would see growth and misreport the *winner's* outcome as its own.
    Locking first, then snapshotting, means a second request only ever sees
    truth: either the pause is genuinely still open (rare true idempotent
    replay — the length check below still catches that), or the case has
    already moved on and `_paused_snapshot`/`_waiting_snapshot` itself
    refuses with its normal 404/409 before `g.invoke()` is ever called.
    """
    with runner.case_lock(case_id):
        g, config, snapshot = (_paused_snapshot if any_pause else _waiting_snapshot)(case_id)
        before = len(snapshot.values.get("audit_log") or [])
        with case_trace(case_id, "board-action", snapshot.values) as span:
            result = g.invoke(Command(resume=resume), config)
            record_outcome(span, snapshot.values, result)

    after_log = result.get("audit_log") or []
    if len(after_log) <= before:
        raise HTTPException(status_code=409, detail="case already left the pause")

    last = after_log[-1]
    if last.get("transition") == Transition.BLK.value:
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
    """Nurse-initiated release, from any pause of an open case (I9). Same
    shape as `/deteriorated` and `/move-to-treatment` above. The real authorization check
    (`release_authorized`, charge-role + valid reason) runs inside the
    graph node, not here.
    """
    return _resume_waiting_case(
        case_id,
        {"event": "RELEASE_REQUESTED", "reason": report.reason, "actor_role": report.actor_role},
        any_pause=True,
    )


@app.get("/api/case/{case_id}")
def case(case_id: str):
    """The case detail panel: the same view intake-channel renders for this
    case, plus how many checkpoints (state snapshots) it has. The event
    trail is `view["audit_log"]`, already formatted as
    `at · transition · action · explanation` by `deterministic.audit()`.
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
        # I2 and I21, re-read over every checkpoint — the two rules the audit
        # log alone cannot answer, beside `view["trace_safety"]` which it can.
        "history_safety": case_history_check(case_id),
    }


def run() -> None:
    """`uv run board` — serve on BOARD_HOST / BOARD_PORT."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("BOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("BOARD_PORT", "8002")),
    )
