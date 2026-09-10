"""FastAPI server for intake-channel — the nurse-facing front door.

Four endpoints:

    GET  /lookup/{id}      thin CRM proxy, for enriching the form
    POST /submit           build a case and run it through the real graph
    POST /resume/{case_id} answer a human gate
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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.observability import agent_span, flush
from app.runner import resume_case, snapshot, start_case

from .mock_cases import SubmissionType, build_case
from .patient_lookup import fetch_patient

load_dotenv()

STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="Triage Guard — intake-channel", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SubmitRequest(BaseModel):
    stable_patient_id: str
    submission_type: SubmissionType


class ResumeRequest(BaseModel):
    """A charge nurse's answer to a gate. Structured, never free text — no model
    interprets a clinician's decision.
    """

    decision: str
    resolver_role: str = "charge_nurse"


def _view(state: dict[str, Any], pending: dict[str, Any] | None) -> dict[str, Any]:
    """What the browser needs: the outcome, the gate if any, and the trail."""
    return {
        "case_id": state.get("case_id"),
        "status": "awaiting_human_approval" if pending else "settled",
        "control_state": state.get("control_state"),
        "outcome": state.get("intake_outcome"),
        "missing_fields": state.get("missing_fields", []),
        "reason": state.get("intake_reason"),
        "acuity": state.get("acuity"),
        "acuity_source": state.get("acuity_source"),
        "acuity_gap": state.get("acuity_gap"),
        "safety_passed": state.get("safety_passed"),
        "approved": state.get("approved"),
        "degraded": state.get("degraded", []),
        "flags": state.get("flags", []),
        "gate": pending,
        "audit_log": state.get("audit_log", []),
    }


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


@app.post("/resume/{case_id}")
def resume(case_id: str, body: ResumeRequest):
    """Answer a human gate and let the case continue."""
    try:
        state, pending = resume_case(case_id, body.model_dump())
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"cannot resume {case_id}: {exc}")
    flush()
    return _view(state, pending)


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
