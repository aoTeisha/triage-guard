"""Langfuse enrichment: what each case trace carries, and what it must never carry.

All offline. The Langfuse client is never built here; these are the pure
functions `case_trace` and `record_outcome` feed to it.
"""

from __future__ import annotations

import pytest

from app import observability as obs
from app.labels import Transition


def test_patient_ref_is_stable_and_hides_the_id(monkeypatch):
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "s3cret")
    ref = obs.patient_ref("300000001")
    assert ref == obs.patient_ref("300000001")
    assert ref.startswith("pt-") and len(ref) == 19
    assert "300000001" not in ref


def test_patient_ref_depends_on_the_salt(monkeypatch):
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "a")
    first = obs.patient_ref("300000001")
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "b")
    assert obs.patient_ref("300000001") != first


def test_patient_ref_without_salt_is_none(monkeypatch):
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "")
    assert obs.patient_ref("300000001") is None


def test_patient_ref_without_id_is_none(monkeypatch):
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "s3cret")
    assert obs.patient_ref(None) is None


def test_trace_attributes_for_a_new_case(monkeypatch):
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "s3cret")
    monkeypatch.setenv("TRIAGE_LLM", "mock")
    attrs = obs.trace_attributes("case-1", "case-start", {
        "stable_patient_id": "300000001", "channel": "website", "submission_type": "new"})
    assert attrs["session_id"] == "case-1"
    assert attrs["trace_name"] == "case-start"
    assert attrs["user_id"] == obs.patient_ref("300000001")
    assert set(attrs["tags"]) == {"case-start", "llm-mock", "channel-website", "new"}
    assert attrs["metadata"]["case_id"] == "case-1"
    assert attrs["metadata"]["operation"] == "case-start"
    assert attrs["metadata"]["llm_mode"] == "mock"


def test_trace_attributes_never_carry_the_raw_id(monkeypatch):
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "s3cret")
    attrs = obs.trace_attributes("case-1", "case-start", {"stable_patient_id": "300000001"})
    assert "300000001" not in repr(attrs)


def test_trace_attributes_values_fit_langfuse_limits():
    attrs = obs.trace_attributes("case-1", "timer-fire", {"channel": "x" * 500})
    for value in [attrs["session_id"], attrs["trace_name"], *attrs["tags"], *attrs["metadata"].values()]:
        assert len(value) <= 200 and value.isascii()


def test_mask_redacts_identifiers_in_nested_payloads():
    masked = obs._mask(data={"raw_payload": {"stable_patient_id": "300000001",
                                             "notes": ["call 0521234567"]}})
    assert "300000001" not in repr(masked)
    assert "0521234567" not in repr(masked)


def test_mask_leaves_case_ids_and_timestamps_alone():
    data = {"case_id": "case-3dbf3237", "at": "2026-09-25T13:48:54+00:00", "due": 1790000000}
    assert obs._mask(data=data) == data


def _rec(t):
    return {"at": "2026-09-25T00:00:00+00:00", "case_id": "c1", "control_state": "x",
            "action": "a", "explanation": "e", "transition": t}


def test_outcome_scores_full_state():
    after = {"control_state": "monitoring", "acuity": 3, "acuity_gap": 1,
             "audit_log": [_rec(None), _rec(Transition.BLK.value)]}
    scores = {name: (value, kind) for name, value, kind in obs.outcome_scores({}, after)}
    assert scores["control_state"] == ("monitoring", "CATEGORICAL")
    assert scores["acuity"] == (3.0, "NUMERIC")
    assert scores["acuity_gap"] == (1.0, "NUMERIC")
    assert scores["guardrail_blocks"] == (1.0, "NUMERIC")
    assert scores["trace_check"][1] == "BOOLEAN"


def test_outcome_scores_counts_only_new_blocks():
    blk = _rec(Transition.BLK.value)
    before = {"audit_log": [blk]}
    after = {"control_state": "monitoring", "audit_log": [blk, _rec(None)]}
    scores = {name: value for name, value, _ in obs.outcome_scores(before, after)}
    assert scores["guardrail_blocks"] == 0.0


def test_outcome_scores_minimal_state():
    assert obs.outcome_scores({}, None) == []
    scores = {name for name, _, _ in obs.outcome_scores({}, {"control_state": "intake_received"})}
    assert scores == {"control_state", "guardrail_blocks"}


def test_case_trace_is_a_no_op_when_tracing_is_off():
    with obs.case_trace("case-1", "case-start", {}) as span:
        obs.record_outcome(span, {}, {"control_state": "monitoring"})
        span.update(output={"x": 1})


def test_case_trace_reraises():
    with pytest.raises(ValueError):
        with obs.case_trace("case-1", "timer-fire", {}):
            raise ValueError("graph failed")


def test_patient_lookup_cli_prints_the_ref(monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_TRACE_SALT", "s3cret")
    obs.main(["300000001"])
    assert capsys.readouterr().out.strip() == obs.patient_ref("300000001")


def test_mask_redacts_inside_models_and_resume_commands():
    from langgraph.types import Command

    from app.graph.state import TriageState

    state = TriageState(case_id="case-1", stable_patient_id="300000001",
                        raw_payload={"free_text": "ID 123456789"})
    resume = Command(resume={"chief_complaint": "call 0521234567"})
    masked = repr(obs._mask(data={"input": state, "resume": resume}))
    for raw in ("300000001", "123456789", "0521234567"):
        assert raw not in masked


class _FakeSpanCM:
    def __init__(self):
        self.exited = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = exc
        return False


class _FakeClient:
    def __init__(self):
        self.cm = _FakeSpanCM()

    def start_as_current_observation(self, **_):
        return self.cm


def _tracing_on(monkeypatch, fake):
    monkeypatch.setattr(obs, "tracing_enabled", lambda: True)
    monkeypatch.setattr(obs, "client", lambda: fake)


def test_case_trace_closes_the_span_when_propagation_fails(monkeypatch):
    fake = _FakeClient()
    _tracing_on(monkeypatch, fake)

    def boom(**_):
        raise RuntimeError("propagation broke")

    monkeypatch.setattr("langfuse.propagate_attributes", boom)
    with obs.case_trace("case-1", "case-start", {}) as span:
        assert isinstance(span, obs._NullSpan)
    assert fake.cm.exited is not None


def test_case_trace_keeps_the_graph_error_when_closing_fails(monkeypatch):
    fake = _FakeClient()
    _tracing_on(monkeypatch, fake)

    class BadExit(_FakeSpanCM):
        def __exit__(self, *exc):
            raise RuntimeError("langfuse exit broke")

    monkeypatch.setattr("langfuse.propagate_attributes", lambda **_: BadExit())
    with pytest.raises(ValueError, match="graph failed"):
        with obs.case_trace("case-1", "case-start", {}):
            raise ValueError("graph failed")
    assert fake.cm.exited is not None   # span closed even though the inner exit broke


def test_node_error_messages_are_masked():
    handler_cls = obs._masking_handler_class()
    handler = object.__new__(handler_cls)   # the method reads no handler state
    level, message = handler._get_error_level_and_status_message(
        ValueError("bad value for 300000001, call 0521234567"))
    assert level == "ERROR"
    assert "300000001" not in message and "0521234567" not in message


def test_case_trace_masks_the_error_the_span_records(monkeypatch):
    fake = _FakeClient()
    _tracing_on(monkeypatch, fake)
    monkeypatch.setattr("langfuse.propagate_attributes", lambda **_: _FakeSpanCM())
    with pytest.raises(ValueError, match="300000001"):   # the caller still gets the real error
        with obs.case_trace("case-1", "case-start", {}):
            raise ValueError("no patient 300000001")
    recorded = fake.cm.exited[1]
    assert "300000001" not in str(recorded)
    assert "ValueError" in str(recorded)
