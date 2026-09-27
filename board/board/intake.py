"""The front door: creating a case, and answering the pauses that belong to the
intake side of the board page.

These endpoints and the rest of the board share one process and one origin:
the browser makes no cross-origin request, and this is the one front door a
nurse uses to both create a case and answer its pauses.

Every endpoint that answers a pause goes through `commands.answer_pause`, which
owns the case lock, the pause validation and the graph invocation. Each of them
passes `require_applied=False` and replies with the bare case view: none of
these four confirm that the resume actually grew the audit log. Case creation
does not go through it: `runner.start_case` opens a new LangGraph thread
rather than re-entering a paused one, which is a genuinely different operation,
and it takes its own locks — one scoped to the patient (so two submissions for
one patient serialize, for the I19 duplicate check) and one scoped to the
thread.

Which pause each answer belongs to is stated explicitly per endpoint, because
the two pause mechanisms in this graph are not interchangeable. The
human-approval gate pauses via a bare `interrupt()`, so it shows up only as a
pending task and `control_state` still reads whatever the previous node set —
`paused_at` is the only honest test there. The reassessment re-file pause is a
commit node followed by a pause node, so `control_state` stays equal to
`reassessment_required` for exactly as long as the pause lasts, and
`in_control_state` reads it directly.

Getting that wrong drops patients rather than returning an error. A
`ResumeRequest` body has no `nurse_proposed_acuity`, `chief_complaint` or
`vitals`; if `/resume` were allowed to answer the re-filing pause, that pause's
own field reads would take `None` for each of them and the case would continue
with its clinical fields emptied, for a patient still physically in the waiting
room.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import runner
from app.guards import ACUITY_LEVELS, NURSE_SUPPLIED_FIELDS, unusable_fields
from app.states import State
from app.views import case_view

from . import commands
from .mock_cases import SubmissionType, build_case
from .patient_lookup import fetch_patient

router = APIRouter(prefix="/api")

# These two pauses have no entry in the State enum — they are node names, not
# control states, and they report themselves as pending tasks on the checkpoint.
AWAITING_INTAKE_FIX = "awaiting_intake_fix"
AWAITING_RECOVERY = "awaiting_recovery"

# `/resume`'s own refusal text — kept distinct from the generic `paused_at`
# wording so the reply a nurse reads does not change.
_NOT_AT_GATE = "case is not awaiting human approval"


def _answer_pause(case_id, resume, check):
    """`commands.answer_pause` plus one behavior specific to these endpoints:
    a `CaseLockTimeout` (another writer still holding the case past the 5s
    timeout) is a 409, not an unhandled 500 the page cannot render.

    Scoped to this module rather than added inside `commands.answer_pause`
    itself: the board's own four write endpoints in `api.py` have no such
    wrapper, and giving them one now would be an unrequested behavior change on
    that half of the contract.
    """
    try:
        return commands.answer_pause(case_id, resume, check, require_applied=False)
    except runner.CaseLockTimeout as exc:
        raise HTTPException(status_code=409, detail=f"cannot resume {case_id}: {exc}")


class SubmitRequest(BaseModel):
    national_id: str            # what the patient carries; the CRM returns the internal id
    submission_type: SubmissionType


class ResumeRequest(BaseModel):
    """A charge nurse's answer to a gate. Structured, never free text — no model
    interprets a clinician's decision.
    """

    decision: str
    # required: every human action is authorized by the actor's own role, so a
    # missing role must not silently default to charge nurse
    resolver_role: str
    # For a safety failure: the corrected values that let the case pass safety
    # validation again, e.g. {"acuity": 2}.
    corrections: dict[str, Any] | None = None


class ReassessmentSubmission(BaseModel):
    """A nurse re-filing a case after its reassessment timer fired (or a
    reported deterioration brought it back to the front door). Structured, like
    `ResumeRequest` — the same clinical fields `/submit` accepts, minus the
    routing metadata that does not change for a case that already exists.
    """

    nurse_proposed_acuity: int = Field(ge=min(ACUITY_LEVELS), le=max(ACUITY_LEVELS))
    chief_complaint: str
    vitals: dict


@router.get("/lookup/{national_id}")
def lookup(national_id: str):
    """Thin proxy to the CRM stub's national-id lookup.

    Always 200 with a status field — found / not_found / db_error are all valid
    outcomes the page must render distinctly, not HTTP errors to branch on.
    """
    result = fetch_patient(national_id)
    return {"status": result.status, "record": result.record}


@router.post("/submit")
def submit(body: SubmitRequest):
    """Build a case from the form and run it through the graph.

    Tracing happens in `start_case`: its `case-start` span is the root of this
    case's trace.
    """
    lookup_result = fetch_patient(body.national_id)
    case = build_case(lookup_result, body.national_id, body.submission_type)

    try:
        state, pending = runner.start_case(case)
    except runner.CaseClosedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return case_view(state, pending)


@router.post("/case/{case_id}/resume")
def resume(case_id: str, body: ResumeRequest):
    """Answer the human-approval gate and let the case continue.

    Replies with the bare case view, as it always has. A BLK refusal shows up in
    that view's own `audit_log` and leaves `gate` populated, which is how the page
    knows the gate is still open for someone authorized.
    """
    outcome = _answer_pause(
        case_id,
        body.model_dump(),
        commands.paused_at(State.AWAITING_HUMAN_APPROVAL.value, _NOT_AT_GATE),
    )
    return outcome.view()


@router.post("/case/{case_id}/reassess")
def reassess(case_id: str, body: ReassessmentSubmission):
    """Answer the reassessment re-filing pause with fresh observations."""
    unusable = unusable_fields(body.model_dump())
    if unusable:
        raise HTTPException(status_code=422, detail=f"values outside their allowed set: {unusable}")
    outcome = _answer_pause(
        case_id,
        body.model_dump(),
        commands.in_control_state(
            State.REASSESSMENT_REQUIRED.value,
            "case is not awaiting a reassessment re-file",
        ),
    )
    return outcome.view()


@router.post("/case/{case_id}/fields")
def fields(case_id: str, body: dict[str, Any]):
    """Complete an incomplete intake. The same case continues rather than a
    new one starting, so the patient keeps their original arrival time and
    queue position, and is never dropped from the pipeline.
    """
    unknown = sorted(set(body) - NURSE_SUPPLIED_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"not intake form fields: {unknown}")
    unusable = unusable_fields(body)
    if unusable:
        raise HTTPException(status_code=422, detail=f"values outside their allowed range: {unusable}")
    outcome = _answer_pause(case_id, body, commands.paused_at(AWAITING_INTAKE_FIX))
    return outcome.view()


@router.post("/case/{case_id}/recover")
def recover(case_id: str):
    """The technician reports that a halted agent recovered; the case re-enters
    at the stage that halted.
    """
    outcome = _answer_pause(
        case_id, {"event": "AGENT_RECOVERED"}, commands.paused_at(AWAITING_RECOVERY)
    )
    return outcome.view()
