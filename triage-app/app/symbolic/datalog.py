"""Datalog checks over the whole timer store at once (pyDatalog).

Prolog answers "what about *this* timer"; this answers "across *all* timers
and cases right now, is a deadline missing?" Findings are reported
(escalated), never acted on.
"""

from __future__ import annotations

from typing import Any

from pyDatalog import pyDatalog

from app.labels import Transition

# A timer that is still going to do something. Everything else is history.
LIVE_STATES = {"SCHEDULED", "DUE", "DISPATCHING", "FAILED", "UNKNOWN"}

# A FAILED timer whose last_error contains one of these isn't stuck on an
# ordinary retryable fault — it's being refused outright by an engine outage
# or a cross-layer disagreement (see `app.monitor.fire.handle`). `claim_retryable`
# still retries it forever, but for *this* pass it must not count as
# "coverage": a case whose only reassessment timer is wedged this way is
# exactly the silently-unwatched case this check exists to catch.
#
# Checked as a substring, not a prefix: the OPA gate wraps the same marker
# (`fire.dispatch`/`fire._send_reminder` write "opa denied dispatch:
# engine_unavailable:opa (...)"), so a plain startswith would miss it.
_ENGINE_REFUSAL_MARKERS = ("engine_unavailable:", "layer_disagreement")

RULES = """
unwatched(C) <= waiting(C) & ~live_timer(C, 'reassessment', T)
orphan(C, T) <= live_timer(C, K, T) & ~active_case(C) & ~outlives_case(K)
"""

# A timer kind that is *meant* to be live after its case closes: the CRM
# write-back (I17) runs for a released case by definition, so it is never an
# orphan.
OUTLIVES_CASE = ("crm_writeback",)


def _answers(query: str) -> list[tuple]:
    result = pyDatalog.ask(query)
    return [tuple(row) for row in result.answers] if result else []


def _is_engine_refused(timer: dict[str, Any]) -> bool:
    """A FAILED timer stuck on an engine refusal, not an ordinary fault."""
    if timer["fire_state"] != "FAILED":
        return False
    last_error = timer.get("last_error") or ""
    return any(marker in last_error for marker in _ENGINE_REFUSAL_MARKERS)


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
    for kind in OUTLIVES_CASE:
        pyDatalog.assert_fact("outlives_case", kind)
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


def find_duplicate_active_case(stable_patient_id: str | None, case_rows: list[dict[str, Any]]) -> str | None:
    """I19: the case_id of another active case for this patient, if one
    exists in `case_rows`. `case_rows` should already exclude the new case
    itself (see `app.runner.all_case_summaries(exclude_case_id=...)`) — every
    row here is a candidate for "the other" case.

    A plain scan, not pyDatalog: unlike `tick_invariants` (a real join over
    two collections), this is one linear pass with no rules to express.

    "Active" excludes both terminal control_states: `case_closed` (a
    released case) and `input_rejected` (build.py routes it straight to
    `END` — the run never resumes from there). Excluding only `case_closed`
    was a production bug: a duplicate-rejected case sat at `input_rejected`
    forever, so it kept counting as "active" and blocked every later
    resubmission for that patient, which piled up more `input_rejected`
    ghosts, each blocking the next — a self-perpetuating chain (seen live
    for patient P-1003: one correct rejection against a then-open case,
    followed by an unbounded string of wrong ones against the growing ghost
    trail, long after the real case had been released).

    Returns the lexicographically first match if more than one somehow
    exists, so the result is deterministic.
    """
    if not stable_patient_id:
        return None
    matches = sorted(
        row["case_id"] for row in case_rows
        if row["stable_patient_id"] == stable_patient_id
        and row["control_state"] not in (None, "case_closed", "input_rejected")
    )
    return matches[0] if matches else None


def order_key_follows_acuity(history: list[dict[str, Any]]) -> tuple[bool, str]:
    """I2: between one checkpoint and the next, `order_key` changes only when
    the acuity did — or the nurse's proposed level did, which is what a re-file
    is (SPECIFICATION.md § Queue ordering rule: a re-triage may re-key). The
    first key ever assigned is not a change.

    Read over the persisted history like `audit_log_is_monotonic`, because the
    audit log records that a level changed but not what the key became.
    """
    for i in range(len(history) - 1):
        before, after = history[i], history[i + 1]
        key_before, key_after = before.get("order_key"), after.get("order_key")
        if key_before is None or key_after is None or list(key_before) == list(key_after):
            continue
        acuity_changed = before.get("acuity") != after.get("acuity")
        nurse_changed = before.get("nurse_proposed_acuity") != after.get("nurse_proposed_acuity")
        if not (acuity_changed or nurse_changed):
            return False, (f"checkpoint {i + 1}: order_key moved from {list(key_before)} to "
                           f"{list(key_after)} with no change of acuity")
    return True, ""


def history_invariants(history: list[dict[str, Any]]) -> dict[str, tuple[bool, str]]:
    """The two invariants only a case's checkpoint history can answer, for a
    detail view to show beside the trace check: I21 (the audit log only ever
    grows) and I2 (the queue key follows the acuity)."""
    return {
        "audit_log_append_only": audit_log_is_monotonic(history),
        "order_key_follows_acuity": order_key_follows_acuity(history),
    }


def audit_log_is_monotonic(history: list[list[dict[str, Any]]]) -> tuple[bool, str]:
    """I21: true if every checkpoint's audit_log is an exact prefix of the
    next checkpoint's — the append-only guarantee, checked over a case's
    *entire* persisted history rather than trusted from the in-process
    reducer alone (see docs/superpowers/plans/2026-09-22-case-management-invariants.md
    for why this is the provenance check, not a DB trigger).

    `history` is `app.runner.history(case_id)` — oldest first, one entry per
    checkpoint, already includes checkpoints where `audit_log` didn't grow
    (a node that didn't add a record) — those are fine; the rule is
    "never shrinks or changes," not "grows every step."
    """
    for i in range(len(history) - 1):
        before = history[i].get("audit_log") or []
        after = history[i + 1].get("audit_log") or []
        if len(after) < len(before):
            return False, f"checkpoint {i + 1}: audit_log shrank from {len(before)} to {len(after)} records"
        if after[:len(before)] != before:
            return False, f"checkpoint {i + 1}: an earlier audit_log record changed"
    return True, ""


# Provenance of one case's acuity, and the clinical fields the classifier was
# supposed to have. Both are joins over facts, which is what Datalog is for:
# "who wrote this, and were they allowed to" rather than "is this number right".
SAFETY_RULES = """
authorized_write(R) <= acuity_write(R) & allowed_role(R)
unauthorized_write(R) <= acuity_write(R) & ~allowed_role(R)
missing_field(F) <= required_field(F) & ~present_field(F)
"""

# What the classifier needs to have judged on. Absent by now means the acuity
# was decided over data the case does not hold.
CLINICAL_FIELDS = ("chief_complaint", "vitals")


def acuity_provenance(
    triage_records: list[dict[str, Any]],
    payload: dict[str, Any],
    allowed_roles: frozenset[str] | set[str],
) -> dict[str, list]:
    """`{"writers": [...], "unauthorized": [...], "missing_fields": [...]}` for one triage.

    `triage_records` is this triage's slice of the audit log — a re-file starts a
    new triage, and last triage's charge-nurse decision does not vouch for this
    one's acuity (I3). `allowed_roles` comes from Prolog rather than a fourth
    hand-written copy of the role set.

    Facts are rebuilt per call: pyDatalog's knowledge base is process-global.
    """
    pyDatalog.clear()
    pyDatalog.load(SAFETY_RULES)
    # Negated predicates need at least one fact to exist at all (see tick_invariants).
    pyDatalog.assert_fact("allowed_role", "__never__")
    pyDatalog.assert_fact("present_field", "__never__")

    for role in allowed_roles:
        pyDatalog.assert_fact("allowed_role", role)
    for record in triage_records:
        if record.get("action") in ("apply_human_acuity", "apply_correction"):
            pyDatalog.assert_fact("acuity_write", record.get("resolver_role") or "unrecorded")
    for field in CLINICAL_FIELDS:
        pyDatalog.assert_fact("required_field", field)
        if (payload or {}).get(field) is not None:
            pyDatalog.assert_fact("present_field", field)

    return {
        "writers": sorted(row[0] for row in _answers("authorized_write(R)")),
        "unauthorized": sorted(row[0] for row in _answers("unauthorized_write(R)")),
        "missing_fields": sorted(row[0] for row in _answers("missing_field(F)")),
    }


def current_triage(audit_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records belonging to the triage running now.

    A `front_door_rerun` record is a re-file: everything before it belongs to a
    finished triage. Same boundary `app.verification.check_trace` uses.
    """
    last_rerun = -1
    for i, record in enumerate(audit_log or []):
        if record.get("transition") == Transition.FRONT_DOOR_RERUN:
            last_rerun = i
    return list(audit_log or [])[last_rerun + 1:]
