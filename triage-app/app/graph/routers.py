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
from app.labels import Transition, Route
from app.states import ClinicalStatus, State
from app.symbolic import prolog


def route_intake(state: TriageState) -> Event:
    """SUBMISSION_VALID / MISSING_FIELDS / SUBMISSION_UNUSABLE / INVALID_INPUT —
    the four mutually exclusive intake outcomes."""
    return Event(state.intake_outcome)


def route_after_identity(state: TriageState) -> Route:
    """CRM_FOUND / CRM_NEW / AF_DB all continue; a malformed record retries;
    a duplicate active case for this patient (I19) is denied outright.

    A DB outage is not a verification failure — it is the fail-open degrade path,
    and the case proceeds on intake-only data.
    """
    if state.control_state == State.INPUT_REJECTED.value:
        return Route.DENIED
    if state.crm_status is None:
        return (
            Route.RETRY if retry_budget_left(state.retry_count, "crm")
            else Route.PROCEED     # exhausted: continue on intake-only data
        )
    return Route.PROCEED


def route_after_redaction(state: TriageState) -> Route:
    """PAYLOAD_CLEAN / PRIVACY_REFUSED / PRIVACY_GATE_DOWN.

    The model sees only a payload the privacy check approved. A refusal is a
    block and an OPA outage is a degrade; both skip the model and settle on the
    nurse's acuity. A crash of the step itself never reaches here: its error
    handler halts the case (AF_PII).
    """
    # No payload is never an approved one: the model gets nothing to classify.
    if state.payload_refused or not state.redacted_payload:
        return Route.BLOCKED      # refused: the guard did its job, the model is skipped
    if state.payload_unverified:
        return Route.DEGRADED     # OPA down: skip the model, use the nurse's acuity
    return Route.PROCEED


def route_after_classify(state: TriageState) -> Route:
    """ACUITY_PROPOSED / V_RETRY_CLASSIFIER / V_EXHAUSTED_CLASSIFIER.

    Exhaustion is not a halt: the classifier is non-critical and fail-open, so the
    case degrades to the nurse's acuity with the gate disabled.
    """
    if state.system_proposed_acuity is not None:
        return Route.PROCEED
    if retry_budget_left(state.retry_count, "acuity_classifier"):
        return Route.RETRY
    return Route.EXHAUSTED


def route_acuity_gap(state: TriageState) -> Route:
    """ACUITY_AGREE / ACUITY_GAP_MINOR (settled) vs ACUITY_GAP_MAJOR (charge nurse decides).

    `nurse_proposed_acuity` is mandatory for a DATA_PARSED case, so it is present
    here — but if either value is missing there is no gap to resolve and the case
    goes to a human rather than to an invented number.
    """
    if state.nurse_proposed_acuity is None or state.system_proposed_acuity is None:
        return Route.ESCALATE
    return Route.PROCEED if state.acuity is not None else Route.ESCALATE


def route_after_safety(state: TriageState) -> Route:
    """SAFETY_PASSED / SAFETY_FAILED. No verdict is a bug: raise into the crash path."""
    if state.safety_verdict is None:
        raise RuntimeError("safety_validating returned without a verdict")
    return Route.CLEARED if state.safety_passed else Route.ESCALATE


def route_verdict(state: TriageState) -> Route:
    """ESCALATION_NEEDED / CLEARED_TO_QUEUE.

    `escalation_needed` = "verdict fail ∨ low confidence ∨ policy hit" is a
    cross-cutting condition, not a single-node guard: it names every reason to
    invoke the Human Escalation agent, and each disjunct fires wherever its reason
    arises. "verdict fail" fires on the SAFETY_FAILED edge, which never reaches
    this node, so what is left to test here is the confidence disjunct.

    "policy hit" has no definition anywhere in the spec and is not wired; when it
    gains one it becomes an `or` on the line below.
    """
    # A human who has already answered a gate for this case has answered the very
    # question this guard asks. Without it the gate re-fires on the way back
    # through — resolving it does not change `confidence`, so the case would
    # bounce between the gate and safety validation until the recursion limit.
    #
    # The test is `human_decision`, which only the gate writes. Not
    # `acuity_source == human_confirmed`: the gate sets that only when it settles
    # an acuity, and a validator-down answer (revalidate, clear) settles none.
    if state.human_decision is not None:
        return Route.CLEARED
    if not confidence_ok(state.confidence, state.gate_disabled):
        return Route.ESCALATE
    return Route.CLEARED


def route_gate(state: TriageState) -> Route:
    """GATE_ACUITY_RESOLVED / GATE_SAFETY_CORRECTED / GATE_REVALIDATE / BLK, plus the
    correction-round loop guard.

    Both resolved branches return to `safety_validating`: the acuity branch because
    a newly settled acuity must be validated, the safety branch because the spec
    allows correct-and-revalidate but never override.
    """
    released_or_refused = release_route(state)
    if released_or_refused:
        return released_or_refused
    if state.audit_log and state.audit_log[-1].get("transition") == Transition.GATE_SAFETY_WAIVED.value:
        return Route.CLEARED      # a shift lead cleared it; the check it lacks cannot run
    authorized, _ = prolog.may_resolve_gate(state.resolver_role, senior_required=state.senior_required)
    if not authorized:
        return Route.DENIED
    # Handed to a senior when the rounds run out or a charge nurse escalates.
    # Once a senior holds the case, rounds no longer count, so it can't loop
    # past them (I8).
    safety_branch = state.escalation_reason in ("safety_fail", "validator_down")
    if safety_branch and not state.senior_required and (
        state.human_decision == "escalate_further"
        or not correction_rounds_left(state.correction_rounds - 1)
    ):
        return Route.EXHAUSTED
    return Route.PROCEED


def route_wait_resume(state: TriageState) -> Route:
    """Where `awaiting_reassessment` goes next, based on what it just wrote.

    BLK and RELEASE are told apart by the transition the node just logged, not
    by re-deriving authorization here — the node already decided that. A move
    is told apart by `clinical_status` rather than its transition: once a case is
    `treatment_started`, ANY later resume (a stale reassessment timer, a
    deterioration report — the timer scheduled at queue-entry keeps running
    independently of this case's later moves) must keep routing back here
    instead of falling through to `REASSESSMENT_REQUIRED`, which would wrongly
    revert `clinical_status` for a patient who is already in treatment.
    """
    released_or_refused = release_route(state)
    if released_or_refused:
        return released_or_refused
    # Signed off (formal_validation) for the same reason: past treatment, a
    # patient only ever leaves by release.
    if state.clinical_status in (ClinicalStatus.TREATMENT_STARTED.value,
                                 ClinicalStatus.FORMAL_VALIDATION.value):
        return Route.MOVED
    return Route.PROCEED


def release_route(state: TriageState) -> Route | None:
    """What a pause just did, read from the last audit row (the node already
    decided): RELEASED ends the run, DENIED re-pauses, None means its own answer.
    """
    last_transition = state.audit_log[-1].get("transition") if state.audit_log else None
    if last_transition == Transition.RELEASE.value:
        return Route.RELEASED
    if last_transition == Transition.BLK.value:
        return Route.DENIED
    return None


def route_pause_exit(state: TriageState) -> Route:
    """Exit of the intake-fix and re-file pauses: released, refused, or on to parsing."""
    return release_route(state) or Route.PROCEED


def route_refile_exit(state: TriageState) -> Route:
    """Exit of the re-filing pause: released, refused, moved into treatment
    before a re-file (on to the treatment pause), or on to parsing."""
    released_or_refused = release_route(state)
    if released_or_refused:
        return released_or_refused
    if state.clinical_status == ClinicalStatus.TREATMENT_STARTED.value:
        return Route.MOVED
    return Route.PROCEED


def route_after_recovery(state: TriageState) -> Route | State:
    """AF_RECOVER: re-enter at the stage that halted, unless released. Only a
    crash of `redacting_routing` halts today (AF_PII)."""
    return release_route(state) or state.failed_stage
