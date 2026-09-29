"""FastAPI server for the triage board — the one nurse-facing service: the live
queue board and the intake front door on one page.

    GET  /                            the board itself
    GET  /api/board                   columns, cards, counters — one call per refresh
    GET  /api/case/{case_id}          detail panel: the shared case view + its trail
    GET  /api/health
    GET  /api/heartbeat               is the background sweeper process still alive?
    POST /api/case/{case_id}/deteriorated       nurse-initiated DETERIORATION_DETECTED
    POST /api/case/{case_id}/move-to-treatment  nurse-initiated MOVE_REQUESTED
    POST /api/case/{case_id}/treatment-complete nurse-initiated TREATMENT_COMPLETE
    POST /api/case/{case_id}/release            nurse-initiated RELEASE_REQUESTED

The `intake` router, mounted below, adds the five endpoints that create a case
and answer its pauses: `POST /api/submit`, `POST /api/case/{case_id}/resume`,
`/reassess`, `/fields`, and `/recover`.

Every one of these nine writes re-enters the case's own paused LangGraph run
through `commands.answer_pause` rather than touching case state directly. That
function owns the case lock, validates the pause inside it, and invokes the
graph; each endpoint here only decides which pause it answers and how it
formats the reply. The real authorization check for a move, a release, a gate
answer or a re-file (`move_authorized`, `release_authorized`, the resolver-role
check, and so on) runs inside the graph node that receives the resume, not
here — a request this layer accepts can still come back refused.

This is the minimal version of the treatment-move and release endpoints: they
only work while a case is genuinely parked in the waiting-room pause
(`control_state == monitoring`) or, for release, any open pause. See
docs/superpowers/plans/2026-09-17-treatment-move-and-release/findings.md
for why the gate and re-file pauses are out of scope for those two, and
docs/STATUS.md item 4 for the full treatment-move execution machine (Tool
Gateway, idempotency, reconciliation) this version deliberately skips.

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
from pydantic import BaseModel

from app import outages
from app.guards import CHIEF_COMPLAINTS
from app.budgets import HEARTBEAT_STALE_MULTIPLIER
from app.labels import Transition
from app.monitor import timers
from app.monitor.sweeper import SWEEP_INTERVAL_SECONDS
from app.runner import case_history_check, history
from app.budgets import BOARD_FEED_WINDOW_MINUTES, BOARD_RED_AFTER_MINUTES
from app.states import AcuityBucket, ClinicalStatus, State
from app.views import BOARD_COLUMNS, CaseCard, card_from_state, case_view

from . import commands
from .intake import router as intake_router
from .mock_cases import PLANTED
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
app.include_router(intake_router)


@app.middleware("http")
async def always_recheck_static(request, call_next):
    """The browser must revalidate the page and its scripts on every load. A
    heuristically cached script paired with fresh HTML once left the new-case
    form missing its fields. Revalidation is cheap: unchanged files answer 304.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["cache-control"] = "no-cache"
    return response

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


def _nudge_is_stale(nudge: dict, last_transition_at: str | None) -> bool:
    """A nudge from before the case's most recent transition describes a
    situation that has since moved on."""
    return last_transition_at is not None and _at_key(last_transition_at) > _at_key(nudge["at"])


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
        # Records the trace-violation demo planted are not events that
        # happened to a patient; the red alert in the case panel shows them.
        if rec.get("transition") in NOTIFY_TRANSITIONS and rec.get("action") != PLANTED
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
    last_transition_at = {
        (state.get("case_id") or ""): ((state.get("audit_log") or [{}])[-1]).get("at")
        for state in states
    }
    return {
        "columns": BOARD_COLUMNS,
        # The re-file form offers these and nothing else: the complaint must be a
        # code from a fixed set, never typed prose, and the vocabulary has one
        # home, app/guards/fields.py.
        "chief_complaints": list(CHIEF_COMPLAINTS),
        "counters": counters(cards),
        "outages": outages_status(),
        "red_after_min": BOARD_RED_AFTER_MINUTES,
        "notifications": notifications(states, feed),
        "cards": [
            c.model_dump()
            | {
                "position": place.get(c.case_id),
                # A released card keeps no nudge: unlike the notification strip
                # below, this is a live status chip, and a reminder from before
                # release is no longer live. Same for any other transition since
                # the nudge was sent — it was about a situation the case has
                # since moved on from.
                "reminders": (
                    None if c.status == ClinicalStatus.PATIENT_RELEASED.value
                    or (nudges.get(c.case_id) is not None
                        and _nudge_is_stale(nudges[c.case_id], last_transition_at.get(c.case_id)))
                    else nudges.get(c.case_id)
                ),
            }
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


class OutageSwitch(BaseModel):
    down: bool


@app.get("/api/outages")
def outages_status():
    """The outage switch: which components are simulated down for every patient.
    `enabled` is false unless the board runs with `DEMO_OUTAGES=1`."""
    return {"enabled": outages.enabled(), "components": list(outages.COMPONENTS),
            "down": outages.down_list()}


@app.put("/api/outages/{component}")
def set_outage(component: str, body: OutageSwitch):
    """Take one component down for every patient, or bring it back. The sweeper
    reads the same flags, so it follows within a tick."""
    if not outages.enabled():
        raise HTTPException(status_code=404, detail="outage switch disabled (set DEMO_OUTAGES=1)")
    if component not in outages.COMPONENTS:
        raise HTTPException(status_code=404, detail=f"no component {component!r}")
    outages.set_down(component, body.down)
    return outages_status()


@app.get("/api/board")
def board():
    """One payload, one render. Polled; no CRM call anywhere in this path, so a
    CRM outage cannot blank the board.
    """
    return board_payload()


class DeteriorationReport(BaseModel):
    signal: str
    actor_role: str = "nurse"


class MoveToTreatmentReport(BaseModel):
    actor_role: str = "nurse"


class ReleaseReport(BaseModel):
    reason: str
    actor_role: str = "nurse"


# The waiting-room pause reports itself through `control_state`, the same fact
# `app.monitor.fire.dispatch` checks before firing a reassessment timer.
_WAITING = "case is not currently waiting in the queue"


def _report(outcome: commands.Outcome) -> dict:
    """The board's own reply shape, unchanged since before `commands.py` existed.

    A guard refusal is not an HTTP error — the request was valid and processed,
    the graph just said no — so this still returns 200, with `status: "denied"`
    and the guard's own explanation. That is what lets the page tell "accepted"
    apart from "refused" instead of assuming every 200 means success.
    """
    if not outcome.accepted:
        return {"status": "denied", "detail": outcome.refusal}
    return {"status": "ok"}


@app.post("/api/case/{case_id}/deteriorated")
def deteriorated(case_id: str, report: DeteriorationReport):
    """Lets a nurse report that a patient's condition is worsening while they
    wait. This manual path stays useful even once a real vitals-monitoring feed
    exists — a human noticing something is always valid input.
    """
    return _report(commands.answer_pause(
        case_id,
        {"event": "DETERIORATION_DETECTED", "signal": report.signal,
         "actor_role": report.actor_role},
        commands.in_control_state(State.MONITORING.value, _WAITING),
        require_applied=True,
    ))


@app.post("/api/case/{case_id}/move-to-treatment")
def move_to_treatment(case_id: str, report: MoveToTreatmentReport):
    """Nurse-initiated move into treatment. Only wired from the waiting-room
    pause in this version. The real authorization check (`move_authorized`) runs
    inside the graph node that receives the resume, not here, so a request this
    layer accepts can still come back refused.
    """
    return _report(commands.answer_pause(
        case_id,
        {"event": "MOVE_REQUESTED", "actor_role": report.actor_role},
        commands.in_control_state(State.MONITORING.value, _WAITING),
        require_applied=True,
    ))


@app.post("/api/case/{case_id}/treatment-complete")
def treatment_complete(case_id: str, report: MoveToTreatmentReport):
    """Treatment is done: the case moves to the sign-off column before release.
    The pause node refuses it for a patient who is not in treatment.
    """
    return _report(commands.answer_pause(
        case_id,
        {"event": "TREATMENT_COMPLETE", "actor_role": report.actor_role},
        commands.in_control_state(State.MONITORING.value, _WAITING),
        require_applied=True,
    ))


@app.post("/api/case/{case_id}/release")
def release(case_id: str, report: ReleaseReport):
    """Nurse-initiated release: possible from any pause of a case that is not
    yet closed, and only with a valid reason. The real authorization check
    (`release_authorized`, charge role plus a valid reason) runs inside the
    graph node, not here.
    """
    return _report(commands.answer_pause(
        case_id,
        {"event": "RELEASE_REQUESTED", "reason": report.reason,
         "actor_role": report.actor_role},
        commands.open_pause(),
        require_applied=True,
    ))


@app.get("/api/case/{case_id}")
def case(case_id: str):
    """The case detail panel: the same view `board.intake`'s endpoints render
    for this case, plus how many checkpoints (state snapshots) it has. The
    event trail is `view["audit_log"]`, already formatted as
    `at · transition · action · explanation` by `deterministic.audit()`.
    """
    values, gated = repo.load(case_id)
    if not values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    # `gated` means the run is currently paused waiting for a charge nurse's
    # decision. A paused run hasn't written any gate-related fields to its
    # state yet — the pause itself only shows up as a pending task on the
    # checkpoint. This read endpoint just needs to know it's paused; actually
    # answering the gate is `board.intake.resume`'s job, not this one's.
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
