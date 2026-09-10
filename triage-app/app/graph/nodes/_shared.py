"""Shared helper used across the node modules."""

from __future__ import annotations

from app.graph.state import TriageState


def _bump(state: TriageState, agent: str) -> dict[str, int]:
    """Increment this agent's shared retry counter (§ retry_budget_left)."""
    return {agent: state.retry_count.get(agent, 0) + 1}
