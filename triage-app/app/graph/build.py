"""The graph — a direct transcription of docs/SPECIFICATION.md § Transitions.

This module is the transition table. Every `add_conditional_edges` call below is a
block of rows from that table, and the path map is the set of legal moves: a
destination that is not in the map cannot be reached, and LangGraph validates the
map against the declared nodes at compile time.

That is the property the old CrewAI Flow could not provide. A `@router` there
returned an arbitrary string and whichever listener matched, ran; the set of
allowed edges existed only as a convention spread across decorators. Here it is a
declared object the framework holds, checks, and can draw.

`tests/test_edges.py` asserts this wiring against the spec table, and
`UNIMPLEMENTED_STATES` keeps the gap between spec and skeleton explicit rather than
invisible.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.budgets import RETRY_BUDGET
from app.events import Event
from app.graph import nodes, routers
from app.graph.state import TriageState
from app.labels import Route
from app.states import State

# Control states the spec names that this slice does not wire. Listed, not
# forgotten: the World-plane slice (monitoring timers, treatment move, release) is
# out of scope for the skeleton, and `tests/test_edges.py` fails if a state is
# neither a node here nor in this set.
UNIMPLEMENTED_STATES: frozenset[State] = frozenset(
    {State.REASSESSMENT_REQUIRED, State.CASE_CLOSED}
)

# Nodes whose actor performs I/O and can therefore raise. RetryPolicy covers the
# transport crash; the V-retry self-loops in the edge maps below cover malformed
# output. See the migration notes on why these two counters are separate.
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


def build_graph(checkpointer=None):
    """Compile the control plane.

    `checkpointer` is required for the human gates — `interrupt()` needs somewhere
    to suspend to. Tests that only exercise routing may compile without one.
    """
    b = StateGraph(TriageState)

    # ---- nodes ---------------------------------------------------------------
    b.add_node(State.INTAKE_RECEIVED, nodes.intake_received)
    b.add_node(State.PARSING, nodes.parsing)
    b.add_node(State.MISSING_FIELDS_REQUESTED, nodes.missing_fields_requested)
    b.add_node(State.SUBMISSION_FAILED, nodes.submission_failed)
    b.add_node(State.INPUT_REJECTED, nodes.input_rejected)
    b.add_node(State.DATA_PARSED, nodes.data_parsed)
    b.add_node(State.RESOLVING_IDENTITY, nodes.resolving_identity,
               retry_policy=_retry_policy(State.RESOLVING_IDENTITY))
    b.add_node(State.REDACTING_ROUTING, nodes.redacting_routing)
    b.add_node(State.CLASSIFYING, nodes.classifying,
               retry_policy=_retry_policy(State.CLASSIFYING))
    b.add_node(State.ACUITY_PROPOSED, nodes.acuity_proposed)
    b.add_node(State.SAFETY_VALIDATING, nodes.safety_validating,
               retry_policy=_retry_policy(State.SAFETY_VALIDATING))
    b.add_node(State.VERDICT_PROPOSED, nodes.verdict_proposed)
    b.add_node(State.AWAITING_HUMAN_APPROVAL, nodes.awaiting_human_approval)
    b.add_node(State.MONITORING, nodes.monitoring)
    b.add_node(State.AGENT_FAILED, nodes.agent_failed)
    b.add_node(State.ACTION_DENIED, nodes.action_denied)
    # Degrade handlers. Separate nodes rather than branches inside their step, so
    # the AF-* rows are visible in the rendered graph instead of buried in an if.
    b.add_node("classifier_fallback", nodes.classifier_fallback)
    b.add_node("safety_fallback", nodes.safety_fallback)

    # ---- arrow 1a / 2: entry -> parsing ---------------------------------------
    b.add_edge(START, State.INTAKE_RECEIVED)
    b.add_edge(State.INTAKE_RECEIVED, State.PARSING)

    # ---- arrows 4 / 16 / 17 / 18: the four intake outcomes --------------------
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

    # The three non-happy branches end the run. Their spec continuations
    # (1b.x FIELDS_SUBMITTED, 1a·resubmit, 1a·rejected) are new submissions, i.e.
    # a fresh invocation of the graph, not an edge inside this one.
    b.add_edge(State.MISSING_FIELDS_REQUESTED, END)
    b.add_edge(State.SUBMISSION_FAILED, END)
    b.add_edge(State.INPUT_REJECTED, END)

    # ---- arrow 4b: data_parsed -> identity lookup -----------------------------
    b.add_edge(State.DATA_PARSED, State.RESOLVING_IDENTITY)

    # ---- arrows 4b·found / 4b·new / AF·db -------------------------------------
    b.add_conditional_edges(
        State.RESOLVING_IDENTITY,
        routers.route_after_identity,
        {
            Route.PROCEED: State.REDACTING_ROUTING,
            Route.RETRY:   State.RESOLVING_IDENTITY,
        },
    )

    # ---- arrows 5 / 6 / V·halt·PII / AF·PII -----------------------------------
    b.add_conditional_edges(
        State.REDACTING_ROUTING,
        routers.route_after_redaction,
        {
            Route.PROCEED: State.CLASSIFYING,
            Route.RETRY:   State.REDACTING_ROUTING,
            Route.HALT:    State.AGENT_FAILED,
        },
    )

    # ---- arrows 7 / 8 / V·retry·classifier / V·exhausted·classifier -----------
    b.add_conditional_edges(
        State.CLASSIFYING,
        routers.route_after_classify,
        {
            Route.PROCEED:   State.ACUITY_PROPOSED,
            Route.RETRY:     State.CLASSIFYING,
            Route.EXHAUSTED: "classifier_fallback",
        },
    )
    # AF·classifier skips the gap resolution entirely: with no system acuity there
    # is no discrepancy to resolve, which is why the gate is disabled.
    b.add_edge("classifier_fallback", State.SAFETY_VALIDATING)

    # ---- arrows 9a / 9b / 9c ---------------------------------------------------
    b.add_conditional_edges(
        State.ACUITY_PROPOSED,
        routers.route_acuity_gap,
        {
            Route.PROCEED:  State.SAFETY_VALIDATING,
            Route.ESCALATE: State.AWAITING_HUMAN_APPROVAL,
        },
    )

    # ---- arrows 10 / 10·fail / V·retry·safety / AF·safety ---------------------
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

    # ---- arrows 11 / 11·pass ---------------------------------------------------
    b.add_conditional_edges(
        State.VERDICT_PROPOSED,
        routers.route_verdict,
        {
            Route.CLEARED:  State.MONITORING,
            Route.ESCALATE: State.AWAITING_HUMAN_APPROVAL,
        },
    )

    # ---- arrows 1b.z·acuity / 1b.z·safety / BLK -------------------------------
    b.add_conditional_edges(
        State.AWAITING_HUMAN_APPROVAL,
        routers.route_gate,
        {
            Route.PROCEED:   State.SAFETY_VALIDATING,
            Route.DENIED:    State.ACTION_DENIED,
            Route.EXHAUSTED: END,      # correction rounds spent; case held at gate
        },
    )

    # ---- terminals --------------------------------------------------------------
    b.add_edge(State.MONITORING, END)
    b.add_edge(State.AGENT_FAILED, END)
    b.add_edge(State.ACTION_DENIED, END)

    return b.compile(checkpointer=checkpointer)
