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
from app.verification import check_trace
from tests.conftest import transitions

GAP_FREE_CASE = {
    "case_id": "case-plain",
    "channel": "website",
    "national_id": "300000010",
    "nurse_proposed_acuity": 2,
    "chief_complaint": "limb_injury",
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


# ---- PRIVACY_REFUSED: a refusal is a block, not a failure ----------------------


def _leak(monkeypatch, **smuggled):
    """Let one identifier survive the payload build. The builder is an allow-list
    now, so a leak cannot be made by dropping nothing; it has to be smuggled in
    after the build, which is what a bug in the builder would look like.
    Returns the real builder so a test can put it back (the technician's fix)."""
    real = normalizer.build_model_payload
    monkeypatch.setattr(normalizer, "build_model_payload",
                        lambda *a, **k: {**real(*a, **k), **(smuggled or {"name": "Ada L."})})
    return real


def _count_model_calls(monkeypatch):
    from app.actors import acuity_classifier
    calls = []
    monkeypatch.setattr(acuity_classifier, "classify", lambda payload: calls.append(payload))
    return calls


def _refusal(state):
    [record] = [r for r in state["audit_log"] if r["transition"] == Transition.PRIVACY_REFUSED]
    return record


def test_a_refused_payload_queues_the_case_on_the_nurses_acuity(run, monkeypatch):
    """The guard doing its job is not a failure: the case goes on without the
    model, and gets a reassessment timer like any queued patient."""
    _leak(monkeypatch)

    state, pending, _ = run(DEMO_CASES["clean"])

    assert state["control_state"] == State.MONITORING.value
    assert pending.get("waiting_room")
    assert state["acuity"] == DEMO_CASES["clean"]["nurse_proposed_acuity"]
    assert state["acuity_source"] == "nurse_fallback"
    assert state["gate_disabled"] is True
    assert "cross_check_off_review_later" in state["flags"]
    assert Transition.SAFETY_PASSED in transitions(state)
    assert Transition.TIMER_RUNNING in transitions(state)
    assert Transition.AF_RECOVER not in transitions(state)
    assert check_trace(state["audit_log"]).passed


def test_a_refused_payload_never_reaches_the_model(run, monkeypatch):
    calls = _count_model_calls(monkeypatch)
    _leak(monkeypatch)

    state, _, _ = run(DEMO_CASES["clean"])

    assert calls == []
    assert Transition.RUN_CLASSIFIER not in transitions(state)
    assert state["system_proposed_acuity"] is None


def test_the_refusal_names_opa_and_its_reasons(run, monkeypatch):
    _leak(monkeypatch)

    state, _, _ = run(DEMO_CASES["clean"])

    record = _refusal(state)
    assert record["denying_layer"] == "OPA (privacy)"
    assert record["engines"] == ["OPA"]
    assert 'identifier key "name"' in record["explanation"]
    assert 'field "name" is not approved' in record["explanation"]


def test_a_refusal_alerts_the_technician_and_marks_the_card(run, monkeypatch):
    """Redaction or the data upstream missed something: a bug or bad data,
    so the technician hears of it. Marked apart from an OPA outage."""
    _leak(monkeypatch)

    state, _, _ = run(DEMO_CASES["clean"])

    assert _refusal(state)["action"] == "alert_technician"
    assert "privacy_refused" in state["flags"]
    assert state["payload_refused"] is True
    assert "opa" not in state["degraded"]
    assert "acuity_classifier" not in state["degraded"]   # the model is fine, it was skipped


def test_a_regex_hit_is_refused_the_same_way(run, monkeypatch):
    """An identifier typed inside an allowed value: OPA's closed vocabulary
    refuses it too, and the regex scan names what it is."""
    calls = _count_model_calls(monkeypatch)
    _leak(monkeypatch, chief_complaint="id 123456789")

    state, _, _ = run(DEMO_CASES["clean"])

    record = _refusal(state)
    assert "regex scan" in record["denying_layer"]
    assert "national_id in chief_complaint" in record["explanation"]
    assert "123456789" not in str(state)    # the reason is kept, the identifier is not
    assert calls == []
    assert state["control_state"] == State.MONITORING.value
    assert "privacy_refused" in state["flags"]


def test_a_refused_payload_is_never_kept(run, monkeypatch):
    """'A malformed or unsafe output is never written to state': not for the
    model, and not for the board or the CRM write-back either."""
    from app.crm_client import visit_record

    _leak(monkeypatch)

    state, _, _ = run(DEMO_CASES["clean"])

    assert state["redacted_payload"] == {}
    assert "Ada L." not in str(state)
    assert visit_record(state)["chief_complaint"] is None


# ---- AF_PII: a crash of the redaction step halts ------------------------------


def _crash_builder(monkeypatch):
    real = normalizer.build_model_payload

    def broken(*a, **k):
        raise KeyError("vitals")
    monkeypatch.setattr(normalizer, "build_model_payload", broken)
    return real


def test_a_crash_of_the_payload_builder_halts_the_case(run, monkeypatch):
    """The one place the line still stops: the redaction step did not finish."""
    calls = _count_model_calls(monkeypatch)
    _crash_builder(monkeypatch)

    state, pending, _ = run(DEMO_CASES["clean"])

    assert state["control_state"] == State.AGENT_FAILED.value
    assert state["failed_stage"] == State.REDACTING_ROUTING.value
    assert pending == {"case_id": state["case_id"], "recovery_pending": True,
                       "halted_at": State.REDACTING_ROUTING.value}
    [crash] = [r for r in state["audit_log"] if r["transition"] == Transition.AF_PII]
    assert crash["action"] == "alert_technician"
    assert "KeyError" in crash["explanation"] or "vitals" in crash["explanation"]
    assert calls == []
    assert state["redacted_payload"] == {}
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


# ---- V_EXHAUSTED_SAFETY: a malformed verdict is a crash, not a dead end -----


def test_a_malformed_verdict_is_retried_then_reaches_the_validator_down_gate(run, monkeypatch):
    """`validate` always builds a `SafetyVerdict`, so this cannot happen today.
    If it ever did, the node raises: the retry policy runs it again, and once the
    budget is spent the error handler sends the case to `safety_fallback` and a
    charge nurse. The malformed answer is never written to state."""
    calls = []

    def malformed(case):
        calls.append(case)
        return {"verdict": "maybe"}

    monkeypatch.setattr(safety, "validate", malformed)

    state, pending, _ = run(GAP_FREE_CASE)

    assert len(calls) == RETRY_BUDGET["safety_validation"] + 1
    assert pending is not None
    assert pending["gate"] == "validator_down"
    assert "safety_validation" in state["degraded"]
    assert state["safety_verdict"] is None
    assert state["safety_passed"] is False
    assert Transition.V_EXHAUSTED_SAFETY in transitions(state)
    assert state["control_state"] != State.MONITORING.value


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
    """I10: agent_failed does not end the run. AGENT_RECOVERED re-runs the
    stage that halted; with the fault fixed, the case continues.
    """
    from langgraph.types import Command

    from app.runner import config_for, hydrate

    real_build = _crash_builder(monkeypatch)
    _, pending, thread = run(DEMO_CASES["clean"])
    assert pending["recovery_pending"] is True

    monkeypatch.setattr(normalizer, "build_model_payload", real_build)  # the technician's fix
    result = hydrate(graph.invoke(Command(resume={"event": "AGENT_RECOVERED"}),
                                  config_for(thread)))

    assert Transition.AF_RECOVER in transitions(result)
    assert result["redacted_payload"]
    assert graph.get_state(config_for(thread)).next == ("awaiting_reassessment",)


# ---- the classifier: a bad answer is asked again, a crash is retried ------------


def _count_classifier_calls(monkeypatch, answer):
    from app.actors import acuity_classifier
    calls = []

    def classify(payload):
        calls.append(payload)
        return answer()

    monkeypatch.setattr(acuity_classifier, "classify", classify)
    return calls


def _out_of_range():
    from app.schemas import AcuityProposal
    return AcuityProposal.model_validate({"system_proposed_acuity": 47, "confidence": 0.9,
                                          "acuity_source": "system", "rationale": "stub"})


def _transport_down():
    raise ConnectionError("classifier unreachable")


def _fell_back(state):
    seen = transitions(state)
    assert Transition.V_EXHAUSTED_CLASSIFIER in seen
    assert state["acuity_source"] == "nurse_fallback"
    assert state["system_proposed_acuity"] is None


def test_an_out_of_range_level_is_asked_again_then_falls_back(run, monkeypatch):
    """Level 47 fails while the proposal is built. That is a bad answer, not a
    crash: discarded and asked again on the classifier's budget, then the nurse's."""
    calls = _count_classifier_calls(monkeypatch, _out_of_range)
    state, _, _ = run(DEMO_CASES["clean"])
    assert len(calls) == RETRY_BUDGET["acuity_classifier"]
    assert transitions(state).count(Transition.V_RETRY_CLASSIFIER) == len(calls)
    _fell_back(state)


def test_no_answer_at_all_is_asked_again_then_falls_back(run, monkeypatch):
    calls = _count_classifier_calls(monkeypatch, lambda: None)
    state, _, _ = run(DEMO_CASES["clean"])
    assert len(calls) == RETRY_BUDGET["acuity_classifier"]
    assert transitions(state).count(Transition.V_RETRY_CLASSIFIER) == len(calls)
    _fell_back(state)


def test_a_transport_error_takes_the_crash_retries_then_falls_back(run, monkeypatch):
    """A dropped connection is not an answer: the node's RetryPolicy re-runs it
    (budget + 1 attempts), and no V_RETRY_CLASSIFIER is written."""
    calls = _count_classifier_calls(monkeypatch, _transport_down)
    state, _, _ = run(DEMO_CASES["clean"])
    assert len(calls) == RETRY_BUDGET["acuity_classifier"] + 1
    assert Transition.V_RETRY_CLASSIFIER not in transitions(state)
    _fell_back(state)
