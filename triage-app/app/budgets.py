"""Per-agent retry budgets `N` and the human correction-round limit.

docs/SYSTEM_MODELING.md § Unknowns is explicit that these numbers are NOT in the
spec:

    "Per-agent retry budgets are undefined in the spec. We do not invent a number.
     What we do fix is that a retry budget exists, that it must be finite [...] and
     that it differs per agent."

So the *mechanism* is real and the *values* below are working defaults, chosen to
let the skeleton run and to be replaced from measured timeout data. They are
deliberately visible in one table rather than scattered through the graph, so
replacing them is a single edit and so nobody mistakes them for spec facts.

Rationale: a retry fixes a transient fault (network blip, rate limit, malformed
model output). It cannot fix a deterministic code fault, which is why the pure
schema-drop gets zero.
"""

from __future__ import annotations

# Agent keys match `retry_count[agent]` in the Data-plane table and the rows of
# § Per-agent failure model (whose own `Retry N` column reads "(to confirm)").
RETRY_BUDGET: dict[str, int] = {
    # LLM: transient timeouts, rate limits and malformed JSON are all real and all
    # retryable. Capped at 2 (3 attempts total) because § Latency budget targets
    # P95 <= 8s and the classifier is the fattest tail in that budget.
    "acuity_classifier": 2,
    # Symbolic engine. One retry covers a process blip; past that the failure is
    # real, and the spec's answer (AF·safety) is to route every case to a charge
    # nurse, which is safer than looping on a broken validator.
    "safety_validation": 1,
    # Ordinary network call to a non-critical, fail-open store.
    "crm": 2,
    # Deliberately zero. Pure deterministic key removal — if it faulted, the code is
    # broken and the next attempt breaks identically. Critical-closed per the failure
    # model: halt immediately rather than retry into a possible identifier leak.
    "pii_schema_drop": 0,
    # A model service, so transient failure is plausible. On exhaustion the payload
    # drops free text and continues (degrade, safe).
    "pii_bert_ner": 1,
    # Network call to a UI/queue; on exhaustion fall back to manual notification.
    "human_bridge": 2,
}

# docs/SYSTEM_MODELING.md § 188 / § 229: "There is a maximum number of correction
# rounds. When exhausted, the case escalates." Same status as RETRY_BUDGET — the
# bound is required by the spec, the number is not given. Bounds the
# awaiting_human_approval -> safety_validating -> awaiting_human_approval loop
# (arrow 1b.z·safety), which is otherwise unbounded.
MAX_CORRECTION_ROUNDS: int = 3


def retry_budget_left(retry_count: dict[str, int], agent: str) -> bool:
    """The `retry_budget_left(agent)` guard: `retry_count[agent] < N[agent]`.

    An agent with no entry in RETRY_BUDGET has no budget: unknown agents fail
    closed rather than retrying forever.
    """
    return retry_count.get(agent, 0) < RETRY_BUDGET.get(agent, 0)


def correction_rounds_left(rounds_used: int) -> bool:
    """The loop guard on the safety-fail correction cycle."""
    return rounds_used < MAX_CORRECTION_ROUNDS
