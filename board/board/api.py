"""FastAPI server for the board — the room, as opposed to the front door.

    GET  /                    the board itself
    GET  /api/board           columns, cards, counters — one call per refresh
    GET  /api/case/{case_id}  detail panel: the shared case view + its trail
    GET  /api/health

Read-only by design. `POST /move` and `/release` are milestone M2 and wait on the
treatment-move machine (docs/STATUS.md item 4); until that exists the board must
not become the place where a second writer to the World plane gets improvised.
The UI renders those controls disabled, and there is no endpoint behind them.

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

from app.labels import Arrow
from app.runner import history
from app.states import AcuityBucket, ClinicalStatus
from app.views import BOARD_COLUMNS, CaseCard, card_from_state, case_view

from .ordering import positions, sort_cards
from .repo import CheckpointRepo

load_dotenv()

STATIC_DIR = Path(__file__).with_name("static")

# When a card turns red. No threshold is defined anywhere in the spec yet, so
# these are placeholders in one place rather than magic numbers in the JS.
# ponytail: replace with real SLA numbers (and per-band ones, if the data says
# so) once there is wait-time data to set them from.
RED_AFTER_MIN = {AcuityBucket.EMERGENT.value: 15, AcuityBucket.QUEUED.value: 60}

# Items 15-20 of the board diagram, as the arrows that already record them.
# Deliberately not here: `11·pass` (cleared to queue). Every normal case emits it,
# so it fills the strip with one entry per card saying what the card already says.
# A notification is for something that needs attention, not for routine success.
NOTIFY_ARROWS = {
    Arrow.MISSING_FIELDS.value,        # 16  missing fields
    Arrow.SUBMISSION_UNUSABLE.value,   # 17  scan failed / erroneous file
    Arrow.INVALID_INPUT.value,         # 18  injection refused
    Arrow.APPROVAL_REQUESTED.value,    # 20  approval requested
    Arrow.ESCALATION_RECORDED.value,   # 12  escalation answered
    Arrow.REASSESSMENT_DUE.value,      # 14  reassessment needed
    Arrow.MOVE_CONFIRMED.value,        # 19  move to treatment confirmed
    Arrow.BLK.value,                   # refusal — the one worth seeing most
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
    }


def notifications(states: list[dict]) -> list[dict]:
    """The strip along the top, read out of the audit trail.

    The arrows are already the notification vocabulary — missing fields, a failed
    scan, an injection refusal, an approval request, a transition accepted — so
    sourcing the strip from `audit_log` means there is no second notification
    store to keep in sync with the case that caused the entry.
    """
    records = [
        {
            "case_id": rec.get("case_id") or state.get("case_id"),
            # The complaint, so a notification says which patient it is about
            # without anyone having to memorise case ids. Redacted payload first,
            # then parsed, then raw: the cases that generate most notifications
            # (missing fields, unusable scan, refused input) are exactly the ones
            # that failed before a redacted payload was ever built, and a
            # notification reading only "invalid input" helps nobody. Only the
            # complaint is read from the raw payload — never an identifier.
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


@app.get("/api/board")
def board():
    """One payload, one render. Polled; no CRM call anywhere in this path, so a
    CRM outage cannot blank the board.
    """
    return board_payload()


@app.get("/api/case/{case_id}")
def case(case_id: str):
    """The detail panel: the same view intake-channel renders, plus the
    checkpoint trail. The arrow trail is `view["audit_log"]` — already in
    `at · arrow · action · explanation` shape from `deterministic.audit()`.
    """
    values, gated = repo.load(case_id)
    if not values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    # `gated` says the run is suspended at the human gate: a suspended run has
    # not written its gate state yet, so the pause lives in the checkpoint's next
    # task. The panel needs the fact, not the interrupt payload — answering the
    # gate is intake-channel's job, not the board's.
    pending = {"gate": values.get("escalation_reason") or "awaiting charge nurse"} if gated else None
    card = card_from_state(values, None, gated)
    # Position is a property of the queue, not of the case, so it can only be
    # derived against every other card — the same derivation /api/board does.
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
