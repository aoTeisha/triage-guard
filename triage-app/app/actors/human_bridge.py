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
# 9c for the acuity discrepancy, 10·fail / AF·safety for the safety branch).
DISCREPANCY = "discrepancy"
SAFETY_FAIL = "safety_fail"

_OPTIONS: dict[str, list[str]] = {
    DISCREPANCY: ["use_nurse_acuity", "use_system_acuity"],
    SAFETY_FAIL: ["corrected", "escalate_further"],
}


def request_decision(reason: str, case: dict[str, Any]) -> dict[str, Any]:
    """Pause the run and surface the gate to a human.

    Returns whatever the resumer supplied — a mapping with at least `decision` and
    `resolver_role`. The caller is responsible for authorizing the resolver; this
    bridge only carries the message.
    """
    return interrupt(
        {
            "gate": reason,
            "required_role": "charge_nurse",
            "options": _OPTIONS[reason],
            **case,
        }
    )


def mock_resume_for(reason: str) -> dict[str, Any]:
    """The canned charge-nurse reply for unattended demo runs and tests.

    Edit `mocks/human_escalation.json` to change what the mock resolver decides.
    """
    return load_mock("human_escalation")[reason]
