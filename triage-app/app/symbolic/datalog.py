"""Datalog checks over the whole timer store at once (pyDatalog).

Prolog answers "what about *this* timer"; this answers "across *all* timers
and cases right now, is a deadline missing?" Findings are reported
(escalated), never acted on.
"""

from __future__ import annotations

from typing import Any

from pyDatalog import pyDatalog

# A timer that is still going to do something. Everything else is history.
LIVE_STATES = {"SCHEDULED", "DUE", "DISPATCHING", "FAILED", "UNKNOWN"}

# A FAILED timer whose last_error starts with one of these isn't stuck on an
# ordinary retryable fault — it's being refused outright by an engine outage
# or a cross-layer disagreement (see `app.monitor.fire.handle`). `claim_retryable`
# still retries it forever, but for *this* pass it must not count as
# "coverage": a case whose only reassessment timer is wedged this way is
# exactly the silently-unwatched case this check exists to catch.
_ENGINE_REFUSAL_PREFIXES = ("engine_unavailable:", "layer_disagreement")

RULES = """
unwatched(C) <= waiting(C) & ~live_timer(C, 'reassessment', T)
orphan(C, T) <= live_timer(C, K, T) & ~active_case(C)
"""


def _answers(query: str) -> list[tuple]:
    result = pyDatalog.ask(query)
    return [tuple(row) for row in result.answers] if result else []


def _is_engine_refused(timer: dict[str, Any]) -> bool:
    """A FAILED timer stuck on an engine refusal, not an ordinary fault."""
    if timer["fire_state"] != "FAILED":
        return False
    last_error = timer.get("last_error") or ""
    return last_error.startswith(_ENGINE_REFUSAL_PREFIXES)


def tick_invariants(timer_rows: list[dict[str, Any]], case_rows: list[dict[str, Any]]) -> dict[str, list]:
    """`{"unwatched": [case_id], "orphan": [(case_id, timer_id)]}`, sorted.

    unwatched: a case in the waiting room with no live reassessment timer —
               its deadline silently gone.
    orphan:    a live timer for a case that has no state or is closed — work
               the sweeper would retry forever with nobody to tell.

    Facts are rebuilt on every call: pyDatalog's knowledge base is
    process-global, and a tick must never see a previous tick's rows.
    """
    pyDatalog.clear()
    pyDatalog.load(RULES)
    # Both predicates are negated in RULES (`~active_case`, `~live_timer`);
    # pyDatalog raises "Predicate without definition" for a negated predicate
    # that was never asserted at all, so give each one a sentinel fact that
    # can never match a real id/kind/timer_id.
    pyDatalog.assert_fact("active_case", "__never__")
    pyDatalog.assert_fact("live_timer", "__never__", "__never__", "__never__")
    for case in case_rows:
        if case["control_state"] not in (None, "case_closed"):
            pyDatalog.assert_fact("active_case", case["case_id"])
        if case["control_state"] == "monitoring" and case["clinical_status"] == "waiting":
            pyDatalog.assert_fact("waiting", case["case_id"])
    for timer in timer_rows:
        if timer["fire_state"] in LIVE_STATES and not _is_engine_refused(timer):
            pyDatalog.assert_fact("live_timer", timer["case_id"], timer["kind"], timer["timer_id"])
    return {
        "unwatched": sorted(row[0] for row in _answers("unwatched(C)")),
        "orphan": sorted(_answers("orphan(C, T)")),
    }
