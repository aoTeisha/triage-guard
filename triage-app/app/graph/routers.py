"""Conditional-edge functions — the guards of the Transitions table.

Each function reads state and returns a label. `graph/build.py` maps those labels
onto destination nodes, so a router plus its edge map is exactly one block of rows
from docs/SPECIFICATION.md.

Routers are pure: no state writes, no I/O, no clock. That makes every branch in the
control plane unit-testable by constructing a `TriageState` and calling a function —
no graph, no checkpointer, no mocks.
"""

from __future__ import annotations

from app.budgets import confidence_ok, correction_rounds_left, retry_budget_left
from app.events import Event
from app.graph.state import TriageState
from app.labels import Route
from app.states import State


def route_intake(state: TriageState) -> Event:
    """arrows 4 / 16 / 17 / 18 — the four mutually exclusive intake outcomes."""
    return Event(state.intake_outcome)


def route_after_identity(state: TriageState) -> Route:
    """4b·found / 4b·new / AF·db all continue; only a malformed record retries.

    A DB outage is not a verification failure — it is the fail-open degrade path,
    and the case proceeds on intake-only data.
    """
    if state.crm_status is None:
        return (
            Route.RETRY if retry_budget_left(state.retry_count, "crm")
            else Route.PROCEED     # exhausted: continue on intake-only data
        )
    return Route.PROCEED


def route_after_redaction(state: TriageState) -> Route:
    """arrow 6 / V·halt·PII / AF·PII.

    `pii_schema_drop` carries N=0 by design, so a recoverable fault here exhausts
    on its first occurrence and halts. That is the critical-closed rule from the
    failure model, expressed as a budget rather than a special case.
    """
    if state.redacted_payload:
        return Route.PROCEED
    if state.failed_stage == State.REDACTING_ROUTING and not retry_budget_left(
        state.retry_count, "pii_schema_drop"
    ):
        return Route.HALT
    return Route.RETRY


def route_after_classify(state: TriageState) -> Route:
    """arrow 8 / V·retry·classifier / V·exhausted·classifier.

    Exhaustion is not a halt: the classifier is non-critical and fail-open, so the
    case degrades to the nurse's acuity with the gate disabled.
    """
    if state.system_proposed_acuity is not None:
        return Route.PROCEED
    if retry_budget_left(state.retry_count, "acuity_classifier"):
        return Route.RETRY
    return Route.EXHAUSTED


def route_acuity_gap(state: TriageState) -> Route:
    """arrows 9a / 9b (settled) vs 9c (charge nurse decides).

    `nurse_proposed_acuity` is mandatory for a DATA_PARSED case, so it is present
    here — but if either value is missing there is no gap to resolve and the case
    goes to a human rather than to an invented number.
    """
    if state.nurse_proposed_acuity is None or state.system_proposed_acuity is None:
        return Route.ESCALATE
    return Route.PROCEED if state.acuity is not None else Route.ESCALATE


def route_after_safety(state: TriageState) -> Route:
    """arrows 10 / 10·fail / V·retry·safety / AF·safety."""
    if state.safety_verdict is None:
        return (
            Route.RETRY if retry_budget_left(state.retry_count, "safety_validation")
            else Route.EXHAUSTED
        )
    return Route.CLEARED if state.safety_passed else Route.ESCALATE


def route_verdict(state: TriageState) -> Route:
    """arrows 11 / 11·pass.

    `escalation_needed` = "verdict fail ∨ low confidence ∨ policy hit" is a
    cross-cutting condition, not a single-node guard: it names every reason to
    invoke the Human Escalation agent, and each disjunct fires wherever its reason
    arises. "verdict fail" fires on the 10·fail edge, which never reaches this
    node, so what is left to test here is the confidence disjunct.

    "policy hit" has no definition anywhere in the spec and is not wired; when it
    gains one it becomes an `or` on the line below.
    """
    # A human who has already answered a gate for this case has answered the very
    # question this guard asks. Without it the gate re-fires on the way back
    # through — resolving it does not change `confidence`, so the case would
    # bounce between the gate and safety validation until the recursion limit.
    #
    # The test is `human_decision`, which only the gate writes. NOT
    # `acuity_source == human_confirmed`: arrow 9a sets that when the nurse and
    # the system merely agree, with no human consulted, so it would suppress the
    # gate for exactly the cases that never reached one.
    if state.human_decision is not None:
        return Route.CLEARED
    if not confidence_ok(state.confidence, state.gate_disabled):
        return Route.ESCALATE
    return Route.CLEARED


def route_gate(state: TriageState) -> Route:
    """arrows 1b.z·acuity / 1b.z·safety / BLK, plus the correction-round loop guard.

    Both resolved branches return to `safety_validating`: the acuity branch because
    a newly settled acuity must be validated, the safety branch because the spec
    allows correct-and-revalidate but never override.
    """
    if state.resolver_role not in {"charge_nurse", "shift_lead"}:
        return Route.DENIED
    if state.escalation_reason == "safety_fail" and not correction_rounds_left(
        state.correction_rounds - 1
    ):
        # Loop guard exhausted. The case stays at the gate, escalated; the spec
        # does not name who it escalates to, so it is not routed onward here.
        return Route.EXHAUSTED
    return Route.PROCEED
