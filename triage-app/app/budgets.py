"""Per-agent retry budgets and the human correction-round limit.

The project's design requires that every agent (LLM call, validator, external
service) have a finite, per-agent retry budget before it gives up and
degrades — but it deliberately does not fix what that number should be for
each agent; that's left to be tuned from real timeout data later.

So the *mechanism* here is required and the *values* below are working
defaults, chosen to let the system run today and meant to be replaced once
real measurements exist. They're kept in one table rather than scattered
through the graph code, so replacing them later is a single edit and so
nobody mistakes today's placeholder numbers for settled requirements.

Rationale: a retry fixes a transient fault (network blip, rate limit,
malformed model output). It cannot fix a deterministic code bug — the same
input will just fail the same way again — which is why the pure
schema-drop step below gets a retry budget of zero.
"""

from __future__ import annotations

# Keys here match the agent names used in `retry_count[agent]` elsewhere in
# the graph state.
RETRY_BUDGET: dict[str, int] = {
    # LLM call. Transient timeouts, rate limits, and malformed JSON are all
    # real failure modes and all worth retrying. Capped at 2 retries (3
    # attempts total) to keep this step from dominating overall response
    # time, since it's the slowest step in the whole pipeline.
    "acuity_classifier": 2,
    # Symbolic safety-validation engine. One retry covers a transient process
    # blip; past that, the failure is treated as real and every case is
    # routed to a charge nurse instead — safer than looping against a
    # validator that might be genuinely broken.
    "safety_validation": 1,
    "crm": 2,  # ordinary network call to a non-critical, fail-open store
    # Deliberately zero retries. This step just deterministically strips
    # identifying fields from the payload — if that failed, the code itself
    # is broken, and retrying would fail identically every time. Failing
    # closed (halting immediately) is safer here than retrying into a
    # possible identifier leak.
    "pii_schema_drop": 0,
    # Network call to notify a human reviewer; on exhaustion, falls back to
    # a manual notification instead.
    "human_bridge": 2,
}

# Minimum classifier confidence needed to skip sending a case to a charge
# nurse for confirmation. Like the retry budgets above, the requirement that
# such a threshold exist is fixed but the actual number is a working default.
# Below this threshold, a case that already passed safety validation still
# goes to a charge nurse for a second opinion. Doesn't apply during a
# classifier outage: with no system proposal there's no confidence value to
# test in the first place.
CONFIDENCE_THRESHOLD: float = 0.70


def confidence_ok(confidence: float | None, gate_disabled: bool = False) -> bool:
    """True when the classifier was sure enough to skip the confirmation gate.

    A missing confidence is not treated as "low" — it means no model
    proposal was made at all (the classifier is down), which is a separate
    case this guard doesn't apply to.
    """
    if gate_disabled or confidence is None:
        return True
    return confidence >= CONFIDENCE_THRESHOLD


# Caps how many times a case can bounce between the human approval gate and
# safety re-validation before it's forced to escalate instead of looping
# forever. Same status as RETRY_BUDGET: a bound is required, the exact number
# is a working default.
MAX_CORRECTION_ROUNDS: int = 3


def retry_budget_left(retry_count: dict[str, int], agent: str) -> bool:
    """True if `agent` still has retries left, i.e. `retry_count[agent] < RETRY_BUDGET[agent]`.

    An agent with no entry in RETRY_BUDGET has no budget: unknown agents fail
    closed rather than retrying forever.
    """
    return retry_count.get(agent, 0) < RETRY_BUDGET.get(agent, 0)


def correction_rounds_left(rounds_used: int) -> bool:
    """The loop guard on the safety-fail correction cycle."""
    return rounds_used < MAX_CORRECTION_ROUNDS


# ---- waiting-room monitor -------------------------------------------------
# The background process that fires reassessment timers and reminds staff
# about overdue approvals while a case sits in the queue.

# How many times the monitor will retry confirming whether a timer's fire
# actually reached the case (via store/graph reads) before giving up and
# escalating to a human. Same status as RETRY_BUDGET — a placeholder pending
# real latency measurements — kept low enough that the sweeper isn't blocked
# waiting on it.
RECONCILE_BUDGET: int = 3

# Needs sign-off from clinical staff (a Medical Director), not just an
# engineering decision — these are patient-safety timings, not a technical
# setting. The values below are working placeholders so the system runs
# today; the relative ordering (more urgent acuity bands get shorter
# intervals) is intentional, but the exact minutes are provisional.
# Keyed on the acuity band (1 = most urgent, 5 = least urgent).
REASSESSMENT_INTERVAL_MINUTES: dict[int, int] = {
    1: 10,
    2: 15,
    3: 30,
    4: 60,
    5: 120,
}

# How overdue a reassessment timer has to be before it's flagged as a
# `timer_gap` (a window where nobody was watching this case), scaled by
# acuity: a delay that's negligible for a low-urgency (band 5) patient is a
# real gap in observation for a high-urgency (band 1-2) one.
TIMER_GAP_GRACE_MINUTES: dict[int, int] = {
    1: 1,
    2: 1,
    3: 3,
    4: 5,
    5: 10,
}

# How long to wait before reminding staff about an unanswered approval
# request, at each reminder step. Step 0 reminds just the nurse assigned to
# the case; step 1 (if still unanswered) widens the reminder to any charge
# nurse. Values are placeholders until real approval-latency data exists.
GATE_REMINDER_DELAY_MINUTES: dict[int, int] = {
    0: 10,
    1: 20,
}

# A worker's heartbeat is considered stale once it's older than this many
# sweep intervals (`app.monitor.sweeper.SWEEP_INTERVAL_SECONDS`). The sweep
# interval itself is a known, measured constant; this multiplier is the only
# undefined number in that formula.
HEARTBEAT_STALE_MULTIPLIER: int = 3

# Caps how many notifications a given recipient class can receive in one
# time window, to prevent a bug in a timer loop from flooding staff with
# alerts (alarm fatigue). Hitting this cap itself becomes a "notification
# budget exhausted" event, so it's never a silent drop.
NOTIFICATION_BUDGET_PER_WINDOW: int = 5
NOTIFICATION_WINDOW_MINUTES: int = 60
