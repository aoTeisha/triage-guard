"""Agent-failure and output-verification rows (AF_* and V_*).

These branches existed on paper before the migration and were never executed:
the old Flow logged `V_EXHAUSTED_CLASSIFIER` on the *first* verification failure,
having attempted no retry at all. Each row below is now driven for real.
"""

from __future__ import annotations

from app.actors import normalizer, safety
from app.budgets import RETRY_BUDGET, retry_budget_left
from app.labels import Transition
from app.mock_cases import DEMO_CASES
from app.schemas import SafetyVerdict
from app.states import State
from tests.conftest import transitions

GAP_FREE_CASE = {
    "case_id": "case-plain",
    "channel": "website",
    "national_id": "300000010",
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


# ---- V_HALT_PII: structural, never retried -------------------------------


def test_an_identifier_leak_halts_the_case(run, monkeypatch):
    """The redaction step is the one place the line stops rather than degrades."""
    monkeypatch.setattr(
        normalizer, "drop_identifiers", lambda fields: dict(fields)   # drop nothing
    )

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["control_state"] == State.AGENT_FAILED.value
    assert Transition.V_HALT_PII in transitions(state)
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


# ---- SAFETY_FAILED: safety failure routes to a human ----------------------


def test_a_failing_verdict_routes_to_the_gate(run, monkeypatch):
    monkeypatch.setattr(
        safety, "validate",
        lambda case: SafetyVerdict(verdict="fail", reasons=["acuity outside safe band"]),
    )

    state, pending, _ = run(GAP_FREE_CASE)

    assert pending is not None
    assert pending["gate"] == "safety_fail"
    assert Transition.SAFETY_FAILED in transitions(state)
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


# ---- AF_DB: CRM outage degrades, it does not stop the line ----------------


def _crm_unreachable(monkeypatch):
    """Force the outage instead of relying on nothing listening on the CRM port.

    These two tests used to pass only while the stub was down, so they went green
    for the wrong reason the moment a developer started it.
    """
    from app import crm_client

    monkeypatch.setattr(crm_client, "CRM_BASE_URL", "http://127.0.0.1:1")


def test_a_crm_outage_degrades_and_continues(run, monkeypatch):
    _crm_unreachable(monkeypatch)
    state, _, _ = run(DEMO_CASES["clean"])

    assert "crm" in state["degraded"]
    assert "crm_down_intake_only" in state["flags"]
    assert Transition.AF_DB in transitions(state)
    # Fail-open: the case still gets triaged.
    assert state["control_state"] == State.MONITORING.value


def test_a_degraded_case_carries_no_history_into_the_payload(run, monkeypatch):
    _crm_unreachable(monkeypatch)
    state, _, _ = run(DEMO_CASES["clean"])
    assert "history" not in state["redacted_payload"]




def test_a_halted_case_waits_for_recovery_then_resumes_at_redaction(graph, run, monkeypatch):
    """I10: agent_failed no longer ends the run. AGENT_RECOVERED re-runs the
    stage that halted; with the fault fixed, the case continues.
    """
    from langgraph.types import Command

    from app.runner import config_for, hydrate

    real_drop = normalizer.drop_identifiers
    monkeypatch.setattr(normalizer, "drop_identifiers", lambda fields: dict(fields))
    _, pending, thread = run(DEMO_CASES["clean"])
    assert pending["recovery_pending"] is True

    monkeypatch.setattr(normalizer, "drop_identifiers", real_drop)  # the technician's fix
    result = hydrate(graph.invoke(Command(resume={"event": "AGENT_RECOVERED"}),
                                  config_for(thread)))

    assert Transition.AF_RECOVER in transitions(result)
    assert result["redacted_payload"]
    assert graph.get_state(config_for(thread)).next == ("awaiting_reassessment",)
