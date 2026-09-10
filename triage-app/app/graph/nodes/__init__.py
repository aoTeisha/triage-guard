"""Graph nodes — one function per control-plane state, split by phase.

Every node follows the same contract, and it is the contract the spec's State rule
demands: call an actor, verify what it proposed, then return a partial state update.
Actors propose; nodes write; nothing else writes.

Nodes return dicts instead of mutating `state`. LangGraph replays a node on resume
(and on retry), so in-place mutation would double-count. The `audit_log`, `degraded`
and `flags` reducers in `state.py` turn each returned list into an append.

`graph/build.py` imports this package as `nodes` and calls its functions by
attribute (`nodes.intake_received`, ...), so every node — and `audit`, used by
build.py's own crash handler — is re-exported here flat, regardless of which
phase module it lives in.
"""

from __future__ import annotations

from app.deterministic import audit
from app.graph.nodes.classify import acuity_proposed, classifier_fallback, classifying
from app.graph.nodes.gate import awaiting_human_approval
from app.graph.nodes.identity import crm_fallback, resolving_identity
from app.graph.nodes.intake import (
    data_parsed,
    input_rejected,
    intake_received,
    missing_fields_requested,
    parsing,
    submission_failed,
)
from app.graph.nodes.redaction import redacting_routing
from app.graph.nodes.safety import safety_fallback, safety_validating, verdict_proposed
from app.graph.nodes.terminal import action_denied, agent_failed, monitoring

__all__ = [
    "audit",
    "intake_received",
    "parsing",
    "missing_fields_requested",
    "submission_failed",
    "input_rejected",
    "data_parsed",
    "resolving_identity",
    "crm_fallback",
    "redacting_routing",
    "classifying",
    "classifier_fallback",
    "acuity_proposed",
    "safety_validating",
    "safety_fallback",
    "verdict_proposed",
    "awaiting_human_approval",
    "monitoring",
    "agent_failed",
    "action_denied",
]
