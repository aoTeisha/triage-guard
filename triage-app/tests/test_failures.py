"""Agent-failure and output-verification rows (AF·* and V·*).

These branches existed on paper before the migration and were never executed:
the old Flow logged `V·exhausted·classifier` on the *first* verification failure,
having attempted no retry at all. Each row below is now driven for real.
"""

from __future__ import annotations

import pytest

from app.actors import normalizer, safety
from app.budgets import RETRY_BUDGET, retry_budget_left
from app.mock_cases import DEMO_CASES
from app.schemas import SafetyVerdict
from app.states import State
from tests.conftest import arrows

GAP_FREE_CASE = {
    "case_id": "case-plain",
    "channel": "website",
    "stable_patient_id": "300000010",
    "nurse_proposed_acuity": 2,
    "chief_complaint": "ankle sprain",
    "vitals": {"hr": 78, "bp": "118/76", "spo2": 99, "temp_c": 36.7},
    "free_text": "Rolled ankle on the stairs.",
}


# ---- the budget guard itself -------------------------------------------------


def test_budget_is_spent_after_n_attempts():
    counts: dict[str, int] = {}
    for attempt in range(RETRY_BUDGET["acuity_classifier"]):
        assert retry_budget_left(counts, "acuity_classifier")
        counts["acuity_classifier"] = attempt + 1
    assert not retry_budget_left(counts, "acuity_classifier")


def test_schema_drop_gets_no_retries_at_all():
    """N=0 is a design statement: a deterministic fault will not fix itself, and
    the failure model marks this step critical-closed.
    """
    assert RETRY_BUDGET["pii_schema_drop"] == 0
    assert not retry_budget_left({}, "pii_schema_drop")


def test_an_unknown_agent_fails_closed():
    """No budget entry means no budget — never an unbounded retry."""
    assert not retry_budget_left({}, "not_an_agent")


# ---- V·halt·PII: structural, never retried -----------------------------------


def test_an_identifier_leak_halts_the_case(run, monkeypatch):
    """The redaction step is the one place the line stops rather than degrades."""
    monkeypatch.setattr(
        normalizer, "drop_identifiers", lambda fields: dict(fields)   # drop nothing
    )

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["control_state"] == State.AGENT_FAILED.value
    assert "V·halt·PII" in arrows(state)
    assert state["failed_stage"] == State.REDACTING_ROUTING.value


def test_a_leaked_payload_is_never_written_to_state(run, monkeypatch):
    """'A malformed or unsafe output is never written to state.'"""
    monkeypatch.setattr(normalizer, "drop_identifiers", lambda fields: dict(fields))

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["redacted_payload"] == {}
    assert state["system_proposed_acuity"] is None


def test_a_halted_case_never_reaches_the_queue(run, monkeypatch):
    monkeypatch.setattr(normalizer, "drop_identifiers", lambda fields: dict(fields))

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["control_state"] != State.MONITORING.value
    assert state["approved"] is False


# ---- 10·fail: safety failure routes to a human -------------------------------


def test_a_failing_verdict_routes_to_the_gate(run, monkeypatch):
    monkeypatch.setattr(
        safety, "validate",
        lambda case: SafetyVerdict(verdict="fail", reasons=["acuity outside safe band"]),
    )

    state, pending, _ = run(GAP_FREE_CASE)

    assert pending is not None
    assert pending["gate"] == "safety_fail"
    assert "10·fail" in arrows(state)
    assert state["safety_passed"] is False


def test_a_failing_verdict_never_reaches_monitoring_unattended(run, monkeypatch):
    """No approval bypass, even when nobody answers the gate."""
    monkeypatch.setattr(
        safety, "validate", lambda case: SafetyVerdict(verdict="fail", reasons=["x"])
    )

    state, pending, _ = run(GAP_FREE_CASE)

    assert pending is not None
    assert state["control_state"] != State.MONITORING.value
    assert state["approved"] is False


# ---- AF·db: CRM outage degrades, it does not stop the line -------------------


def test_a_crm_outage_degrades_and_continues(run):
    """The stub is not running in the test environment, which is the outage."""
    state, _, _ = run(DEMO_CASES["clean"])

    assert "crm" in state["degraded"]
    assert "crm_down_intake_only" in state["flags"]
    assert "AF·db" in arrows(state)
    # Fail-open: the case still gets triaged.
    assert state["control_state"] == State.MONITORING.value


def test_a_degraded_case_carries_no_history_into_the_payload(run):
    state, _, _ = run(DEMO_CASES["clean"])
    assert "history" not in state["redacted_payload"]


# ---- AF·pii_bert_ner: safe-drop of free text ---------------------------------


def test_a_dead_urgency_scorer_drops_free_text_and_continues(run, monkeypatch):
    """Fail-open, safe-drop: no unredacted prose reaches the model."""

    def boom(payload):
        raise normalizer.ScorerUnavailable("scorer unreachable")

    monkeypatch.setattr(normalizer, "score_urgency", boom)

    state, _, _ = run(DEMO_CASES["clean"])

    assert "pii_bert_ner" in state["degraded"]
    assert "urgency_scores_unavailable" in state["flags"]
    assert state["control_state"] == State.MONITORING.value
