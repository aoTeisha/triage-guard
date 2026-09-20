"""The OPA runtime gate: `opa eval` over `policy/monitor.rego`, called right
before a side effect happens (resuming a case, sending a reminder, moving or
releasing a patient). Fail-closed by construction: anything that stops the
engine from proving `allow` is a deny with a reason, never an exception.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

POLICY = Path(__file__).parent / "policy" / "monitor.rego"
QUERY = "data.triage.monitor.decision"


def evaluate(input: dict[str, Any]) -> dict[str, Any]:
    """`{"allow": bool, "deny_reasons": [str]}` for one proposed action.

    The binary is `opa` on PATH, or whatever `OPA_BIN` points at. Read per
    call, not at import, so a test can point it at nothing and prove the
    deny path.
    """
    # ponytail: one subprocess per call (~12 ms measured). `opa run --server`
    # sidecar + httpx if the sweeper ever gates hundreds of actions a second.
    try:
        completed = subprocess.run(
            [os.environ.get("OPA_BIN", "opa"), "eval", "-d", str(POLICY), "-I", "--format=raw", QUERY],
            input=json.dumps(input), capture_output=True, text=True, timeout=5, check=True,
        )
        decision = json.loads(completed.stdout)
        return {"allow": decision["allow"] is True, "deny_reasons": list(decision["deny_reasons"])}
    except Exception as exc:  # noqa: BLE001 — missing binary, timeout, policy error, bad JSON: all mean "cannot prove allowed"
        return {"allow": False, "deny_reasons": [f"engine_unavailable:opa ({type(exc).__name__}: {exc})"]}
