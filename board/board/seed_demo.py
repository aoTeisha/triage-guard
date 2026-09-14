"""Populate the board with mock cases — `uv run seed-board`.

Cases go in through the real graph: real routing, a real pause at the acuity
gate, a real audit trail, real `order_key`s. Nothing here writes case state.

Two things this script does control, because a demo board with one kind of card
on it demonstrates nothing:

  * **what the mocked classifier proposes.** `mocks/acuity_classifier.json` is a
    single canned number, so every mock case settles on the same acuity and the
    whole board comes out emergent. The stand-in below varies that number per
    case — exactly the role the JSON file plays, just not one-size-fits-all. The
    red-flag pre-check still runs first and still wins, because a rule that a
    demo can switch off is not a rule.
  * **which cases are left at the gate.** Two are started and deliberately not
    resumed, so the `human_review` column has something real in it: cases
    genuinely suspended, waiting for a charge nurse.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from uuid import uuid4

from dotenv import load_dotenv

from app.actors import acuity_classifier
from app.observability import flush
from app.runner import run_to_completion, start_case
from app.schemas import AcuityProposal

# (chief_complaint, free_text, nurse_acuity, system_acuity)
# The chest / stroke complaints trip the red-flag rule and are forced emergent
# whatever the numbers say; the rest settle through the normal gap bands.
SEED_CASES = [
    ("chest tightness for 2 hours", "Pressure in the chest, worse on exertion.", 3, 3),
    ("shortness of breath", "Struggling to catch breath since this morning.", 2, 2),
    ("ankle pain after a fall", "Twisted stepping off a curb.", 4, 4),
    ("sore throat, 3 days", "Painful swallowing, no fever.", 5, 5),
    ("migraine", "Light sensitivity, third day running.", 4, 3),
    ("laceration to forearm", "Kitchen knife, bleeding controlled.", 3, 3),
    ("fever 38.9 and chills", "Since last night, no rash.", 3, 4),
    ("abdominal pain, lower right", "Sharp, worse on movement.", 3, 2),
    ("possible stroke symptoms", "Slurred speech noticed by family.", 2, 2),
    ("rash on both arms", "Itchy, started after new detergent.", 5, 5),
    ("back pain after lifting", "No numbness, can walk.", 4, 4),
    ("ear pain", "Child tugging at left ear.", 5, 4),
]

# Started and left suspended at the acuity gate: gap >= 2 sends them to the
# charge nurse, and nobody answers. These are the human_review column.
GATE_CASES = [
    ("dizziness on standing", "Two episodes today.", 5, 2),
    ("persistent cough", "Three weeks, no blood.", 5, 2),
]


@contextmanager
def classifier_proposing(acuity_by_case: dict[str, int]):
    """Stand in for the canned mock, per case. Red flags still fire first."""
    real = acuity_classifier.classify

    def classify(payload):
        if acuity_classifier.red_flag(payload):
            return real(payload)
        proposed = acuity_by_case.get(payload.get("case_id"), 3)
        return AcuityProposal(
            system_proposed_acuity=proposed,
            confidence=0.81,
            acuity_source="system",
            rationale="SEED: fixed proposal for the demo board",
        )

    acuity_classifier.classify = classify
    try:
        yield
    finally:
        acuity_classifier.classify = real


def _case(complaint: str, free_text: str, nurse: int) -> dict:
    # A fresh case_id per run: the checkpointed thread legitimately accumulates,
    # so re-seeding onto a reused id would pile runs onto one audit trail.
    return {
        "case_id": f"case-{uuid4().hex[:8]}",
        "channel": "website",
        "stable_patient_id": f"3000000{uuid4().int % 90 + 10}",
        "nurse_proposed_acuity": nurse,
        "chief_complaint": complaint,
        "vitals": {"hr": 88, "bp": "128/82", "spo2": 97, "temp_c": 37.0},
        "free_text": free_text,
    }


def seed() -> tuple[int, int]:
    """Run the fixtures. Returns (settled, left at the gate)."""
    load_dotenv()

    settled = [_case(c, t, n) for c, t, n, _ in SEED_CASES]
    at_gate = [_case(c, t, n) for c, t, n, _ in GATE_CASES]
    proposals = {case["case_id"]: fixture[3]
                 for case, fixture in zip(settled + at_gate, SEED_CASES + GATE_CASES)}

    with classifier_proposing(proposals):
        for case in settled:
            run_to_completion(case, thread_id=case["case_id"])
        for case in at_gate:
            # start_case only: the run suspends at the gate and stays there.
            start_case(case, thread_id=case["case_id"])

    flush()
    return len(settled), len(at_gate)


def main() -> None:
    settled, at_gate = seed()
    print(f"seeded {settled + at_gate} cases — {at_gate} left waiting at the acuity gate")
    print("start the board with `uv run board` → http://127.0.0.1:8002")


if __name__ == "__main__":
    sys.exit(main())
