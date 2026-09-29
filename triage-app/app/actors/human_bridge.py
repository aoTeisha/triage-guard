"""Human Escalation bridge (docs/actors/human_escalation.jsonc).

The gate is a real pause. `interrupt()` suspends the run, LangGraph checkpoints the
state, and the process may exit; the case resumes when someone supplies a decision
via `Command(resume=...)`. That is the whole point of the gate, so it is NOT
short-circuited in mock mode.

What mock mode changes is only *who answers*: `mock_resume_for()` reads the canned
reply from `mocks/human_escalation.json` and the demo runner (or a test) feeds it
back through the same resume path a charge nurse would use. The pause, the
checkpoint, and the role check all still happen.

No model is involved. The spec's invariant is that the Acuity Classifier is the only
generative model in the decision path, and a framework that asked an LLM to
interpret a clinician's free-text ruling would break it at the highest-stakes moment
in the flow. The resume payload is structured, and the graph reads the fields.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.actors import load_mock

# Reasons the gate can be entered. One control state, two causes (§ Transitions:
# ACUITY_GAP_MAJOR for the acuity discrepancy, SAFETY_FAILED for the safety branch,
# AF_SAFETY / V_EXHAUSTED_SAFETY when the safety check could not run).
DISCREPANCY = "discrepancy"
LOW_CONFIDENCE = "low_confidence"
SAFETY_FAIL = "safety_fail"
# The safety check could not run (an engine down). Nothing to correct: the
# charge nurse asks for the check again, or hands the case up.
VALIDATOR_DOWN = "validator_down"
# A shift lead's answer to VALIDATOR_DOWN: queue the case without the check, so an
# outage never keeps a patient from treatment.
CLEAR_BY_SHIFT_LEAD = "clear_by_shift_lead"

# Reasons that end in the human choosing an acuity. Both present the same question
# ("which level is right?"); they differ only in what prompted the ask.
ACUITY_REASONS = frozenset({DISCREPANCY, LOW_CONFIDENCE})

_OPTIONS: dict[str, list[str]] = {
    DISCREPANCY: ["use_nurse_acuity", "use_system_acuity"],
    LOW_CONFIDENCE: ["use_nurse_acuity", "use_system_acuity"],
    SAFETY_FAIL: ["corrected", "escalate_further"],
    VALIDATOR_DOWN: ["revalidate", "escalate_further", CLEAR_BY_SHIFT_LEAD],
}


def request_decision(
    reason: str, case: dict[str, Any], *, senior_required: bool = False
) -> dict[str, Any]:
    """Pause the run and surface the gate to a human.

    Returns whatever the resumer supplied: a mapping with at least `decision` and
    `resolver_role`. The caller is responsible for authorizing the resolver; this
    bridge only carries the message. `required_role` names who `may_resolve_gate`
    will actually accept: a shift lead once the correction loop has escalated,
    a charge nurse otherwise.
    """
    return interrupt(
        {
            "gate": reason,
            "required_role": "shift_lead" if senior_required else "charge_nurse",
            "options": _OPTIONS[reason],
            **case,
        }
    )


def mock_resume_for(reason: str) -> dict[str, Any]:
    """The canned charge-nurse reply for unattended demo runs and tests.

    Edit `mocks/human_escalation.json` to change what the mock resolver decides.
    """
    return load_mock("human_escalation")[reason]
