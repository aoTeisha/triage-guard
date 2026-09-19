"""FastAPI server for intake-channel — the nurse-facing front door.

Five endpoints:

    GET  /lookup/{id}      thin CRM proxy, for enriching the form
    POST /submit           build a case and run it through the real graph
    POST /resume/{case_id} answer a human gate
    POST /reassess/{case_id} answer the reassessment re-filing pause
    GET  /case/{case_id}   the persisted state and audit trail

`/submit` used to build a real payload and then hand it to a canned
`MOCK_PARSE_RESULTS` dict — the pre-graph mock loop. It now invokes
`app.graph`, so the UI drives the actual control plane: real routing through
the four intake outcomes, a real pause at the acuity gate, and a real audit
trail. The graph itself still runs on mocked actors (no LLM, no symbolic
engines) unless `TRIAGE_LLM=live`.

Run:
    uv run intake-channel
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import app.runner as runner
from app.observability import agent_span, flush
from app.runner import config_for, resume_case, snapshot, start_case
from app.guards import NURSE_SUPPLIED_FIELDS
from app.states import State
from app.views import case_view as _view

from .mock_cases import SubmissionType, build_case
from .patient_lookup import fetch_patient

load_dotenv()

STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="Triage Guard — intake-channel", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# The board (a separate service, separate origin) links a nurse straight from
# a case card into /reassess/{case_id} — the one cross-service browser call in
# the system, so it needs its own explicit allow-list rather than the default
# same-origin policy. Single dev deployment: one hardcoded origin, matching
# board's own BOARD_HOST/BOARD_PORT defaults, not a general CORS policy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("BOARD_ORIGIN", "http://localhost:8002")],
    allow_methods=["POST"],
    allow_headers=["content-type"],
)


class SubmitRequest(BaseModel):
    stable_patient_id: str
    submission_type: SubmissionType


class ResumeRequest(BaseModel):
    """A charge nurse's answer to a gate. Structured, never free text — no model
    interprets a clinician's decision.
    """

    decision: str
    resolver_role: str  # required: a missing role must not default to charge nurse (I14)
    # For a safety failure: the corrected values (I7), e.g. {"acuity": 2}.
    corrections: dict[str, Any] | None = None


@app.get("/")
def index():
    """Serves the intake form itself."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/lookup/{stable_patient_id}")
def lookup(stable_patient_id: str):
    """Thin proxy to the CRM stub's GET /patients/{id}.

    Always 200 with a status field — found / not_found / db_error are all valid
    outcomes the UI must render distinctly, not HTTP errors to branch on.
    """
    result = fetch_patient(stable_patient_id)
    return {"status": result.status, "record": result.record}


@app.post("/submit")
def submit(body: SubmitRequest):
    """Build a case from the form and run it through the graph.

    The span covers only the work this service does outside the graph; the
    graph's own nodes are traced by the Langfuse callback handler that
    `app.runner` attaches.
    """
    lookup_result = fetch_patient(body.stable_patient_id)
    case = build_case(lookup_result, body.stable_patient_id, body.submission_type)

    with agent_span("intake-submission", case_id=case["case_id"]) as span:
        span.update(input=case)
        state, pending = start_case(case)
        span.update(output={"control_state": state.get("control_state")})
    flush()

    return _view(state, pending)


def _answer_pause(case_id: str, decision: dict) -> dict:
    """Resume a paused case with `decision` and return its view. Shared tail
    for every endpoint that answers a pause once its own guard has confirmed
    the case is genuinely waiting there.
    """
    try:
        state, pending = resume_case(case_id, decision)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"cannot resume {case_id}: {exc}")
    flush()
    return _view(state, pending)


@app.post("/resume/{case_id}")
def resume(case_id: str, body: ResumeRequest):
    """Answer a human gate and let the case continue.

    Only valid while the case is genuinely paused at the human-approval gate.
    `awaiting_human_approval` pauses via a bare `interrupt()` in
    `app.actors.human_bridge.request_decision` rather than the commit-node/
    pause-node split `reassessment_required` uses, so `control_state` cannot
    be trusted here — while genuinely paused, it still reflects whatever the
    *previous* node set. The full state snapshot's `.next` is what actually
    reflects the pause (see `board.repo.CheckpointRepo.load`). Without this
    guard, posting a `ResumeRequest` body to a case parked at the *other*
    pause (`awaiting_reassessment_submission`) would be silently consumed by
    that pause's `submitted.get(...)` calls, sending `None` into the case's
    clinical fields and dropping a patient still in the waiting room.
    """
    g = runner.graph()
    config = config_for(case_id)
    state_snapshot = g.get_state(config)
    if not state_snapshot.values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    at_gate = State.AWAITING_HUMAN_APPROVAL.value in [
        getattr(n, "value", n) for n in state_snapshot.next
    ]
    if not at_gate:
        raise HTTPException(status_code=409, detail="case is not awaiting human approval")

    return _answer_pause(case_id, body.model_dump())


class ReassessmentSubmission(BaseModel):
    """A nurse re-filing a case after its reassessment timer fired (or a
    reported deterioration brought it back to the front door). Structured,
    like `ResumeRequest` — the same clinical fields `/submit` accepts, minus
    the routing metadata that doesn't change for a case that already exists.
    """

    nurse_proposed_acuity: int
    chief_complaint: str
    vitals: dict


@app.post("/reassess/{case_id}")
def reassess(case_id: str, body: ReassessmentSubmission):
    """Answers the reassessment re-filing pause with fresh observations.

    Only valid while the case is genuinely waiting there: `control_state`
    stays `reassessment_required` for exactly as long as that pause lasts,
    which is the point of splitting the commit node from the pause node.
    """
    values = snapshot(case_id)
    if not values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    if values.get("control_state") != State.REASSESSMENT_REQUIRED.value:
        raise HTTPException(status_code=409, detail="case is not awaiting a reassessment re-file")

    return _answer_pause(case_id, body.model_dump())


def _require_pause(case_id: str, node: str) -> None:
    """404 for an unknown case; 409 unless the case is paused at `node`. Each
    pause accepts only its own kind of answer.
    """
    state_snapshot = runner.graph().get_state(config_for(case_id))
    if not state_snapshot.values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    if node not in [getattr(n, "value", n) for n in state_snapshot.next]:
        raise HTTPException(status_code=409, detail=f"case is not paused at {node}")


@app.post("/fields/{case_id}")
def fields(case_id: str, body: dict[str, Any]):
    """Completes an incomplete intake (arrows 1b.x / 1a·resubmit). The same
    case continues, so the patient keeps their arrival time (I2, I10).
    """
    unknown = sorted(set(body) - NURSE_SUPPLIED_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"not intake form fields: {unknown}")
    _require_pause(case_id, "awaiting_intake_fix")
    return _answer_pause(case_id, body)


@app.post("/recover/{case_id}")
def recover(case_id: str):
    """The technician reports AGENT_RECOVERED; the case re-enters at the
    stage that halted (arrow AF·recover).
    """
    _require_pause(case_id, "awaiting_recovery")
    return _answer_pause(case_id, {"event": "AGENT_RECOVERED"})


@app.get("/case/{case_id}")
def case(case_id: str):
    """Current persisted state for a case. Reads the checkpoint, runs nothing."""
    values = snapshot(case_id)
    if not values:
        raise HTTPException(status_code=404, detail=f"no case {case_id}")
    return _view(values, None)


def run() -> None:
    """`uv run intake-channel` — serve on CHANNEL_HOST / CHANNEL_PORT."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("CHANNEL_HOST", "127.0.0.1"),
        port=int(os.environ.get("CHANNEL_PORT", "8001")),
    )
