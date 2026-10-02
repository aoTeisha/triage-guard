"""Audit rows name the symbolic engine that decided them — the safety verdict
is Prolog, the payload check is OPA — so the board panel can show
which engine ran the safety test.
"""

from __future__ import annotations

from langgraph.errors import NodeError

from app.graph.build import _crash
from app.graph.nodes.redaction import redacting_routing
from app.graph.nodes.safety import safety_validating
from app.graph.state import TriageState
from app.states import State


def test_safety_verdict_row_names_prolog():
    state = TriageState(case_id="c1", acuity=3, acuity_source="auto_resolved",
                        acuity_gap=1, nurse_proposed_acuity=3, system_proposed_acuity=4)
    row = safety_validating(state)["audit_log"][0]
    assert row["engines"] == ["Prolog"]


def test_payload_clean_row_names_opa():
    state = TriageState(case_id="c1", parsed_fields={"chief_complaint": "chest_pain"})
    records = redacting_routing(state)["audit_log"]
    clean = next(r for r in records if r["action"] == "emit_event_log")
    assert clean["engines"] == ["OPA"]


def test_a_safety_validator_crash_still_names_its_engines():
    """A Prolog fault fails the verdict closed (tested in
    test_safety_validation.py) rather than raising, but any other crash in
    `safety_validating` still reaches this row — and it's still Prolog's
    stage, so it still gets its chip.
    """
    state = TriageState(case_id="c1")
    error = NodeError("safety_validating", RuntimeError("boom"))
    row = _crash(state, State.SAFETY_VALIDATING, "safety_validation", error,
                 engines=["Prolog"])["audit_log"][0]
    assert row["engines"] == ["Prolog"]
    assert "boom" in row["explanation"]


def test_a_classifier_crash_names_no_engine():
    """The acuity classifier is the LLM step, not a symbolic engine — its
    crash row gets no chip.
    """
    state = TriageState(case_id="c1")
    error = NodeError("classifying", RuntimeError("boom"))
    row = _crash(state, State.CLASSIFYING, "acuity_classifier", error)["audit_log"][0]
    assert "engines" not in row
