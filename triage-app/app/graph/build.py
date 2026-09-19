"""The graph: wires every state (a step in a case's workflow, like "classifying
acuity" or "awaiting human approval") to the states it's allowed to move to
next.

Every `add_conditional_edges` call below declares one state's legal next
moves as an explicit map from outcome to destination state. A destination
not in that map cannot be reached — LangGraph checks the map against the
declared nodes when the graph is compiled, so a typo or a missing edge fails
immediately instead of silently routing nowhere at runtime.

That's an improvement over an earlier version of this system (built on
CrewAI Flow), where a routing function could return any string and whichever
listener happened to match would run — the set of allowed transitions only
existed as a convention spread across code, unchecked by anything.

`tests/test_edges.py` asserts this wiring is complete, and
`UNIMPLEMENTED_STATES` lists the states this version intentionally doesn't
wire up yet, so that gap stays visible instead of silently missing.
"""

from __future__ import annotations

from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy

from app.budgets import RETRY_BUDGET
from app.events import Event
from app.graph import nodes, routers
from app.graph.state import TriageState
from app.labels import Arrow, Route
from app.states import State

# States this version deliberately does not implement yet — listed here so
# the gap is explicit rather than accidental. `treatment move` and `release`
# (what happens after a case leaves the waiting queue) are out of scope for
# now. `tests/test_edges.py` fails if any state is neither wired as a node
# below nor listed in this set, so nothing can silently fall through the
# cracks.
# `CASE_CLOSED` isn't wired yet. `ACTION_DENIED` is deliberately not a node:
# the spec (§ Transitions, the ACTION_DENIED row) says a refused action
# "stays in current state", so a refusal is a BLK audit row written by the
# node that refused, which then re-pauses — never a place a case rests.
UNIMPLEMENTED_STATES: frozenset[State] = frozenset({State.CASE_CLOSED, State.ACTION_DENIED})

# States whose node calls out to something that can fail at the transport
# level (a crashed process, a dropped connection) rather than just returning
# a bad answer. `RetryPolicy` below handles that transport-level crash by
# re-running the whole node; a *malformed but successfully-returned* answer
# is a separate failure mode, handled by each state's own retry edge in the
# conditional-edge maps further down. The two need separate counters because
# they're triggered by different things — a crash vs. a bad result.
_CRASH_RETRY: dict[State, str] = {
    State.RESOLVING_IDENTITY: "crm",
    State.CLASSIFYING: "acuity_classifier",
    State.SAFETY_VALIDATING: "safety_validation",
}


def _retry_policy(state: State) -> RetryPolicy | None:
    agent = _CRASH_RETRY.get(state)
    if agent is None:
        return None
    attempts = RETRY_BUDGET[agent] + 1     # N retries = N+1 attempts
    return RetryPolicy(max_attempts=attempts) if attempts > 1 else None


# These error handlers run once a node's retry policy is exhausted and it
# still raised. Without a handler, that exception would propagate out of
# `graph.invoke` and kill the whole run — logging a failure only covers a
# node that returned a bad result, never one that crashed outright, which is
# the more serious case this needs to handle.
#
# The `error: NodeError` parameter below is matched by its type annotation,
# not by its position in the function signature. Remove the annotation and
# LangGraph will call the handler with just the state and it will fail with
# a missing-argument TypeError.


# `goto` may only name a destination already declared as reachable from the
# failing node. The degrade nodes below are those declared destinations, so
# each handler logs why its agent crashed and then hands off to the same
# fallback node that the normal validation-failure path also uses — one
# degrade implementation per agent, reached whether it crashed or just
# returned something unusable.


def _crash(state: TriageState, node: State, agent: str, error: NodeError) -> dict:
    return {
        "audit_log": [
            nodes.audit(state.case_id, node, "alert_technician",
                        f"{agent} raised after its retry budget: {error.error}",
                        Arrow.AF_RECOVER)
        ]
    }


def _on_classifier_error(state: TriageState, error: NodeError) -> Command:
    """The acuity classifier crashed. Non-critical — fail open by degrading
    to the nurse's own proposed acuity instead of the model's.
    """
    return Command(
        update=_crash(state, State.CLASSIFYING, "acuity_classifier", error),
        goto="classifier_fallback",
    )


def _on_safety_error(state: TriageState, error: NodeError) -> Command:
    """The safety validator crashed. Critical — degrade to a human instead of
    the model: route every case to a charge nurse so the rule "nothing skips
    approval" still holds even while the validator is down.
    """
    return Command(
        update=_crash(state, State.SAFETY_VALIDATING, "safety_validation", error),
        goto="safety_fallback",
    )


def _on_crm_error(state: TriageState, error: NodeError) -> Command:
    """The CRM (patient-lookup service) crashed. Non-critical — fail open by
    continuing with only the data the patient submitted at intake.
    """
    return Command(
        update=nodes.crm_fallback(state, str(error.error)),
        goto=State.REDACTING_ROUTING.value,
    )


_ERROR_HANDLERS = {
    State.CLASSIFYING: _on_classifier_error,
    State.SAFETY_VALIDATING: _on_safety_error,
    State.RESOLVING_IDENTITY: _on_crm_error,
}


def build_graph(checkpointer=None):
    """Assemble and compile the graph: every state as a node, wired together
    by the edges declared below.

    `checkpointer` is required to actually run the human approval gate —
    `interrupt()` needs somewhere to save the paused state to so it can be
    resumed later. Tests that only check routing logic can compile without
    one.
    """
    b = StateGraph(TriageState)

    # ---- nodes: one per step in a case's workflow -----------------------------
    b.add_node(State.INTAKE_RECEIVED, nodes.intake_received)
    b.add_node(State.PARSING, nodes.parsing)
    b.add_node(State.MISSING_FIELDS_REQUESTED, nodes.missing_fields_requested)
    b.add_node(State.SUBMISSION_FAILED, nodes.submission_failed)
    b.add_node(State.INPUT_REJECTED, nodes.input_rejected)
    b.add_node(State.DATA_PARSED, nodes.data_parsed)
    b.add_node(State.RESOLVING_IDENTITY, nodes.resolving_identity,
               retry_policy=_retry_policy(State.RESOLVING_IDENTITY),
               error_handler=_ERROR_HANDLERS[State.RESOLVING_IDENTITY])
    b.add_node(State.REDACTING_ROUTING, nodes.redacting_routing)
    b.add_node(State.CLASSIFYING, nodes.classifying,
               retry_policy=_retry_policy(State.CLASSIFYING),
               error_handler=_ERROR_HANDLERS[State.CLASSIFYING])
    b.add_node(State.ACUITY_PROPOSED, nodes.acuity_proposed)
    b.add_node(State.SAFETY_VALIDATING, nodes.safety_validating,
               retry_policy=_retry_policy(State.SAFETY_VALIDATING),
               error_handler=_ERROR_HANDLERS[State.SAFETY_VALIDATING])
    b.add_node(State.VERDICT_PROPOSED, nodes.verdict_proposed)
    b.add_node(State.AWAITING_HUMAN_APPROVAL, nodes.awaiting_human_approval)
    b.add_node(State.MONITORING, nodes.monitoring)
    b.add_node("awaiting_reassessment", nodes.awaiting_reassessment)
    b.add_node(State.REASSESSMENT_REQUIRED, nodes.reassessment_required)
    b.add_node("awaiting_reassessment_submission", nodes.awaiting_reassessment_submission)
    b.add_node(State.AGENT_FAILED, nodes.agent_failed)
    b.add_node("awaiting_intake_fix", nodes.awaiting_intake_fix)
    b.add_node("awaiting_recovery", nodes.awaiting_recovery)
    b.add_node("escalate_to_senior", nodes.escalate_to_senior)
    # Degrade handlers, as separate nodes rather than branches folded inside
    # their step, so a crash-triggered fallback shows up as its own box in
    # the rendered graph instead of being buried inside an if-statement.
    b.add_node("classifier_fallback", nodes.classifier_fallback)
    b.add_node("safety_fallback", nodes.safety_fallback)

    # ---- entry point: a new case starts parsing --------------------------------
    b.add_edge(START, State.INTAKE_RECEIVED)
    b.add_edge(State.INTAKE_RECEIVED, State.PARSING)

    # ---- parsing a submission has four possible outcomes -----------------------
    b.add_conditional_edges(
        State.PARSING,
        routers.route_intake,
        {
            Event.DATA_PARSED:             State.DATA_PARSED,
            Event.MISSING_FIELDS_DETECTED: State.MISSING_FIELDS_REQUESTED,
            Event.SUBMISSION_FAILED:       State.SUBMISSION_FAILED,
            Event.INVALID_INPUT_DETECTED:  State.INPUT_REJECTED,
        },
    )

    # Missing fields and an unusable submission pause for the nurse and
    # continue the same case (I10). Rejected input is not a patient, so it ends.
    b.add_edge(State.MISSING_FIELDS_REQUESTED, "awaiting_intake_fix")
    b.add_edge(State.SUBMISSION_FAILED, "awaiting_intake_fix")
    b.add_conditional_edges(
        "awaiting_intake_fix",
        routers.route_pause_exit,
        {Route.PROCEED: State.PARSING, Route.RELEASED: END, Route.DENIED: "awaiting_intake_fix"},
    )
    b.add_edge(State.INPUT_REJECTED, END)

    # ---- once data is parsed, look up the patient's identity in the CRM --------
    b.add_edge(State.DATA_PARSED, State.RESOLVING_IDENTITY)

    # ---- identity lookup either succeeds (patient found or new) or fails -------
    b.add_conditional_edges(
        State.RESOLVING_IDENTITY,
        routers.route_after_identity,
        {
            Route.PROCEED: State.REDACTING_ROUTING,
            Route.RETRY:   State.RESOLVING_IDENTITY,
        },
    )

    # ---- redact identifying fields before anything reaches the model -----------
    b.add_conditional_edges(
        State.REDACTING_ROUTING,
        routers.route_after_redaction,
        {
            Route.PROCEED: State.CLASSIFYING,
            Route.RETRY:   State.REDACTING_ROUTING,
            Route.HALT:    State.AGENT_FAILED,
        },
    )

    # ---- classify acuity: success, retry, or give up once retries are spent ----
    b.add_conditional_edges(
        State.CLASSIFYING,
        routers.route_after_classify,
        {
            Route.PROCEED:   State.ACUITY_PROPOSED,
            Route.RETRY:     State.CLASSIFYING,
            Route.EXHAUSTED: "classifier_fallback",
        },
    )
    # If the classifier is down and we fall back to the nurse's own acuity,
    # there's no separate system-proposed acuity left to compare it against —
    # so the discrepancy check below is skipped entirely and the case goes
    # straight to safety validation.
    b.add_edge("classifier_fallback", State.SAFETY_VALIDATING)

    # ---- after classification: proceed, or escalate on a nurse/system disagreement --
    b.add_conditional_edges(
        State.ACUITY_PROPOSED,
        routers.route_acuity_gap,
        {
            Route.PROCEED:  State.SAFETY_VALIDATING,
            Route.ESCALATE: State.AWAITING_HUMAN_APPROVAL,
        },
    )

    # ---- safety validation: cleared, failed, retry, or exhausted ---------------
    b.add_conditional_edges(
        State.SAFETY_VALIDATING,
        routers.route_after_safety,
        {
            Route.CLEARED:   State.VERDICT_PROPOSED,
            Route.ESCALATE:  State.AWAITING_HUMAN_APPROVAL,
            Route.RETRY:     State.SAFETY_VALIDATING,
            Route.EXHAUSTED: "safety_fallback",
        },
    )
    b.add_edge("safety_fallback", State.AWAITING_HUMAN_APPROVAL)

    # ---- after a passing safety verdict: queue, or escalate on low confidence --
    b.add_conditional_edges(
        State.VERDICT_PROPOSED,
        routers.route_verdict,
        {
            Route.CLEARED:  State.MONITORING,
            Route.ESCALATE: State.AWAITING_HUMAN_APPROVAL,
        },
    )

    # ---- after the human approval gate: resume, deny, or exhaust corrections --
    b.add_conditional_edges(
        State.AWAITING_HUMAN_APPROVAL,
        routers.route_gate,
        {
            Route.PROCEED:   State.SAFETY_VALIDATING,
            # Not a terminal: the gate refuses the attempt (its own BLK row)
            # and re-pauses, so an unauthorized click from the board leaves
            # the case exactly as resolvable as before, with its reminder
            # timers still armed. Same reasoning as `awaiting_reassessment`'s
            # DENIED edge below. Ending the run here cleared the interrupt,
            # hid the resolve form, and cancelled both reminders.
            Route.DENIED:    State.AWAITING_HUMAN_APPROVAL,
            # Correction rounds spent, or "Escalate further": a shift lead
            # decides, and the case stays at the gate rather than ending (I8, I10).
            Route.EXHAUSTED: "escalate_to_senior",
            Route.RELEASED: END,
        },
    )
    b.add_edge("escalate_to_senior", State.AWAITING_HUMAN_APPROVAL)

    # ---- when a reassessment timer fires, the case re-enters intake from scratch --
    b.add_edge(State.MONITORING, "awaiting_reassessment")
    b.add_conditional_edges(
        "awaiting_reassessment",
        routers.route_wait_resume,
        {
            Route.PROCEED:  State.REASSESSMENT_REQUIRED,
            Route.MOVED:    "awaiting_reassessment",
            Route.RELEASED: END,
            # A mis-typed actor_role here is routine UI input, not a
            # resolved decision, so ending the run would permanently drop
            # a still-waiting case out of the reassessment safety net.
            # Loop back and stay parked instead — same choice the gate's
            # own DENIED edge above makes, for the same reason.
            Route.DENIED:   "awaiting_reassessment",
        },
    )
    b.add_edge(State.REASSESSMENT_REQUIRED, "awaiting_reassessment_submission")
    b.add_conditional_edges(
        "awaiting_reassessment_submission",
        routers.route_pause_exit,
        {Route.PROCEED: State.PARSING, Route.RELEASED: END,
         Route.DENIED: "awaiting_reassessment_submission"},
    )

    # ---- terminals --------------------------------------------------------------
    b.add_edge(State.AGENT_FAILED, "awaiting_recovery")
    b.add_conditional_edges(
        "awaiting_recovery",
        routers.route_after_recovery,
        {
            State.REDACTING_ROUTING: State.REDACTING_ROUTING,  # the only stage that halts today
            Route.RELEASED: END,
            Route.DENIED: "awaiting_recovery",
        },
    )

    return b.compile(checkpointer=checkpointer)
