"""The OPA runtime gate: `opa eval` over a Rego policy, called right before
something irreversible happens — a case resumes, a reminder goes out, a patient
moves or is released, a payload is handed to the model. Fail-closed by
construction: anything that stops the engine from proving `allow` is a deny with
a reason, never an exception.

Two policies: `policy/monitor.rego` (who may act, I5/I9 and the monitor's own
gates) and `policy/privacy.rego` (what the model may see, I11/I12).
"""

from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

POLICY = Path(__file__).parent / "policy" / "monitor.rego"
QUERY = "data.triage.monitor.decision"
PRIVACY_POLICY = Path(__file__).parent / "policy" / "privacy.rego"
PRIVACY_QUERY = "data.triage.privacy.decision"


def evaluate(input: dict[str, Any], *, policy: Path = POLICY, query: str = QUERY) -> dict[str, Any]:
    """`{"allow": bool, "deny_reasons": [str]}` for one proposed action.

    The binary is `opa` on PATH, or whatever `OPA_BIN` points at. Read per
    call, not at import, so a test can point it at nothing and prove the
    deny path.
    """
    try:
        url = os.environ.get("OPA_URL")
        decision = _via_server(url, query, input) if url else _via_subprocess(policy, query, input)
        return {"allow": decision["allow"] is True, "deny_reasons": list(decision["deny_reasons"])}
    except Exception as exc:  # noqa: BLE001 — missing binary or server, timeout, policy error, bad JSON: all mean "cannot prove allowed"
        return {"allow": False, "deny_reasons": [f"engine_unavailable:opa ({type(exc).__name__}: {exc})"]}


def _via_subprocess(policy: Path, query: str, input: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        [os.environ.get("OPA_BIN", "opa"), "eval", "-d", str(policy), "-I", "--format=raw", query],
        input=json.dumps(input), capture_output=True, text=True, timeout=5, check=True,
    )
    return json.loads(completed.stdout)


@lru_cache(maxsize=1)
def _client():
    """One HTTP client for the process. `httpx.post` builds a fresh client — and
    an SSL context — per call, which measured ~600 ms on Windows against 2 ms
    for a reused one. `trust_env=False`: a proxy must never sit between a gate
    and its policy. Thread-safe, so the board's threadpool can share it.
    """
    import httpx

    return httpx.Client(timeout=5, trust_env=False)


def _via_server(url: str, query: str, input: dict[str, Any]) -> dict[str, Any]:
    """`data.triage.monitor.decision` is served at `/v1/data/triage/monitor/decision`."""
    path = query.removeprefix("data.").replace(".", "/")
    response = _client().post(f"{url.rstrip('/')}/v1/data/{path}", json={"input": input})
    response.raise_for_status()
    return response.json()["result"]


def serve() -> None:
    """`uv run opa-sidecar`: run OPA as a server over both policies, so every
    gate is an HTTP call instead of a process spawn. Point `OPA_URL` at it.
    """
    addr = os.environ.get("OPA_ADDR", "127.0.0.1:8181")
    os.execvp(os.environ.get("OPA_BIN", "opa"),
              [os.environ.get("OPA_BIN", "opa"), "run", "--server", "--addr", addr, str(POLICY.parent)])
