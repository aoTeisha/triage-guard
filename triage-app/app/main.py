#!/usr/bin/env python
"""Triage Guard entrypoint — runs the LangGraph control plane on a demo case.

    uv run triage-guard             # clean case, full happy path
    uv run triage-guard missing     # arrow 16 — missing fields
    uv run triage-guard failed      # arrow 17 — nothing usable
    uv run triage-guard injection   # arrow 18 — prompt injection rejected
    uv run plot                     # write the control-plane diagram

Mock by default: no LLM, no running CRM stub, no key. `TRIAGE_LLM=live` calls a
real model. Gates are real either way — the run genuinely suspends and is resumed
through the same path the UI uses; in mock mode the canned charge-nurse reply
supplies the answer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from app.mock_cases import DEMO_CASES
from app.observability import flush
from app.runner import run_to_completion


def kickoff(which: str = "clean") -> dict:
    """Run one demo case end to end and return its final state."""
    load_dotenv()
    case = DEMO_CASES.get(which, DEMO_CASES["clean"])
    # A fresh thread each run: the demo fixtures reuse their case_ids, and a
    # checkpointed thread legitimately accumulates, so without this the audit
    # trail would grow by one full run every time you re-ran the same case.
    state, gates = run_to_completion(case, thread_id=f"{case['case_id']}-{uuid4().hex[:8]}")
    _report(which, state, gates)
    flush()
    return state


def run() -> None:
    kickoff(sys.argv[1] if len(sys.argv) > 1 else "clean")


def _val(x):
    """Enum members print as `State.MONITORING`; the spec name is what matters."""
    return getattr(x, "value", x)


def _report(which: str, state: dict, gates: list[dict]) -> None:
    # The audit trail carries "·" and "—"; a cp1252 console would mangle them.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    get = lambda k, d=None: _val(state.get(k, d))
    print(f"\n=== Triage Guard — demo case: {which} ===")
    print(f"final control_state : {get('control_state')}")
    print(f"intake_outcome      : {get('intake_outcome')}")
    print(f"acuity (final)      : {get('acuity')}  "
          f"(source={get('acuity_source')}, gap={get('acuity_gap')})")
    print(f"clinical_status     : {get('clinical_status')}")
    print(f"order_key           : {get('order_key')}")
    print(f"safety_passed       : {get('safety_passed')}   approved: {get('approved')}")
    if get("degraded"):
        print(f"degraded            : {get('degraded')}")
    if get("flags"):
        print(f"flags               : {get('flags')}")
    if gates:
        print(f"human gates hit     : {[g['gate'] for g in gates]}")

    print("\n--- audit trail ---")
    for row in get("audit_log", []):
        print(f"  [{str(row.get('arrow') or '—'):>22}] "
              f"{row['action']:<26} {row['explanation']}")


def plot() -> None:
    """Render the control plane from the *declared* edge set.

    Generated from the compiled graph, so the picture cannot disagree with what
    runs — the failure mode the old CrewAI `plot()` had, where edges were inferred
    from source and silently dropped.
    """
    from app.graph import build_graph

    # Repo-root relative, not cwd-relative: `uv run plot` is run from
    # triage-app/ and the diagram belongs beside the docs it illustrates.
    out = Path(__file__).resolve().parents[2] / "docs" / "diagrams" / "control-plane.mmd"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_graph().get_graph().draw_mermaid(), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    run()
