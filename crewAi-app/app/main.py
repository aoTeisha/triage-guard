#!/usr/bin/env python
"""Triage Guard entrypoint — runs the deterministic CrewAI Flow.

Kicks off TriageFlow with a mock intake case and prints the resulting state
and audit trail. No LLM is called (the one LLM step returns mock output), and
no external services are required — this proves the Flow wiring and the
control-plane shape end to end.

Usage:
    uv run triage-guard            # runs the clean demo case
    uv run triage-guard missing    # runs a different demo case (clean/missing/failed/injection)
"""

from __future__ import annotations

import json
import sys

from dotenv import load_dotenv

from app.flow import TriageFlow, TriageState
from app.mock_cases import DEMO_CASES


def run() -> None:
    load_dotenv()

    which = sys.argv[1] if len(sys.argv) > 1 else "clean"
    case = DEMO_CASES.get(which, DEMO_CASES["clean"])

    # Seed the state from the mock case. Everything downstream reads/writes
    # self.state; nothing else writes it.
    flow = TriageFlow()
    flow.state.raw_payload = dict(case)
    flow.state.case_id = case["case_id"]
    flow.state.nurse_proposed_acuity = case.get("nurse_proposed_acuity")

    flow.kickoff()

    _report(flow.state, which)


def _report(state: TriageState, which: str) -> None:
    print(f"\n=== Triage Guard Flow — demo case: {which} ===")
    print(f"final control_state : {state.control_state}")
    print(f"intake_outcome      : {state.intake_outcome}")
    print(f"acuity (final)      : {state.acuity}  (source={state.acuity_source}, gap={state.acuity_gap})")
    print(f"clinical_status     : {state.clinical_status}")
    print(f"order_key           : {state.order_key}")
    print(f"safety_passed       : {state.safety_passed}   approved: {state.approved}")
    if state.degraded:
        print(f"degraded agents     : {state.degraded}")
    if state.flags:
        print(f"flags               : {state.flags}")

    print("\n--- audit trail (emit_event_log) ---")
    for row in state.audit_log:
        print(f"  [{row.get('arrow','—'):>12}] {row['action']:<24} {row['explanation']}")


def plot() -> None:
    """Generate the interactive Flow plot (TriageFlowPlot.html)."""
    TriageFlow().plot("TriageFlowPlot")


if __name__ == "__main__":
    run()
