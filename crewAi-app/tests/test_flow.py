"""Skeleton smoke tests for TriageFlow.

These assert the *shape* of the pipeline (which arrows fire, where a case lands),
not clinical correctness — the producers are still mock. They lock in the
routing so a later refactor of the mock steps can't silently break the wiring.
"""

from app.flow import TriageFlow
from app.mock_cases import DEMO_CASES


def _run(case: dict) -> TriageFlow:
    flow = TriageFlow()
    flow.state.raw_payload = dict(case)
    flow.state.case_id = case["case_id"]
    flow.state.nurse_proposed_acuity = case.get("nurse_proposed_acuity")
    flow.kickoff()
    return flow


def _arrows(flow: TriageFlow) -> list[str]:
    return [r.get("arrow") for r in flow.state.audit_log]


def test_clean_case_reaches_monitoring():
    flow = _run(DEMO_CASES["clean"])
    assert flow.state.control_state == "monitoring"
    assert flow.state.intake_outcome == "DATA_PARSED"
    assert flow.state.safety_passed and flow.state.approved
    assert flow.state.clinical_status == "waiting"
    assert "11·pass" in _arrows(flow)


def test_missing_fields_stops_at_request():
    flow = _run(DEMO_CASES["missing"])
    assert flow.state.control_state == "missing_fields_requested"
    assert "16" in _arrows(flow)


def test_failed_submission_stops():
    flow = _run(DEMO_CASES["failed"])
    assert flow.state.control_state == "submission_failed"
    assert "17" in _arrows(flow)


def test_injection_is_rejected_before_classify():
    flow = _run(DEMO_CASES["injection"])
    assert flow.state.control_state == "input_rejected"
    assert "18" in _arrows(flow)
    # the injection-rejected invariant: never reaches the classifier
    assert "8" not in _arrows(flow)


def test_red_flag_forces_emergent():
    flow = _run(DEMO_CASES["clean"])   # chest tightness → red-flag
    assert flow.state.acuity_source in {"rule_forced", "auto_resolved", "human_confirmed"}
    assert flow.state.acuity_bucket == "emergent"
