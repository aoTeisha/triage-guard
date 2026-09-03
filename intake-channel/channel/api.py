"""FastAPI server for intake-channel.

Two endpoints: a thin CRM lookup proxy, and the submit flow that builds a
mock intake payload and runs it through the same parse_intake span shape
app/main.py already produces. No crew.kickoff() yet — MOCK_PARSE_RESULTS
still stands in for a real Intake Parser run, exactly as today's mock loop
does for the other tasks.

Run:
    uv run intake-channel
    uvicorn channel.api:app --reload
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.mock_data import MOCK_PARSE_RESULTS
from app.observability import agent_span, langfuse

from .mock_cases import SubmissionType, build_case
from .patient_lookup import fetch_patient

load_dotenv()

STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="Triage Guard — intake-channel", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# -- request/response schemas ----------------------------------------------

class SubmitRequest(BaseModel):
    stable_patient_id: str
    submission_type: SubmissionType


# -- endpoints ---------------------------------------------------------------

@app.get("/")
def index():
    """Serves the intake form itself."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/lookup/{stable_patient_id}")
def lookup(stable_patient_id: str):
    """Thin proxy to the CRM stub's GET /patients/{id}.

    Always returns 200 with a status field — found / not_found / db_error
    are all valid outcomes the UI must render distinctly, not HTTP errors
    the caller should branch on. See SPECIFICATION.md's "New patient vs.
    DB down" note: both continue on intake-only data.
    """
    result = fetch_patient(stable_patient_id)
    return {"status": result.status, "record": result.record}


@app.post("/submit")
def submit(body: SubmitRequest):
    """Build a mock intake payload and run it through parse_intake.

    Mirrors app/main.py's existing span shape exactly: a root
    "triage-case" span per case, with one child span for the Intake
    Parser role. The only difference from main.py's own loop is that the
    canned output is chosen by submission_type instead of being fixed.
    """
    lookup_result = fetch_patient(body.stable_patient_id)
    case = build_case(lookup_result, body.stable_patient_id, body.submission_type)
    result = MOCK_PARSE_RESULTS[body.submission_type]

    # On a clean submission, echo the real built payload back as the parsed
    # fields — MOCK_PARSE_RESULTS itself intentionally holds no payload copy
    # (see app/mock_data.py), so it's filled in here from the actual case.
    if body.submission_type == "clean":
        result = {**result, "parsed_fields": case}

    with agent_span("triage-case", case_id=case["case_id"]) as root:
        root.update(input=case)
        with agent_span("Intake Parser", task="parse_intake") as span:
            span.update(input=case, output=result)
        root.update(output=result)
    langfuse.flush()

    return {"case_id": case["case_id"], **result}


def run() -> None:
    """`uv run intake-channel` — serve on CHANNEL_HOST / CHANNEL_PORT."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("CHANNEL_HOST", "127.0.0.1"),
        port=int(os.environ.get("CHANNEL_PORT", "8001")),
    )
