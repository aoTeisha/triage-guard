"""I2 and I21 over a case's checkpoint history (`datalog.history_invariants`),
and the runtime path that now runs them (`runner.case_history_check`, behind
both case endpoints). Before this, I21's checker was written and tested but
called by nothing; I2 was checked only by unit tests of the key function.
"""

from __future__ import annotations

from app import runner
from app.mock_cases import DEMO_CASES
from app.symbolic import datalog


def _cp(order_key, acuity=None, nurse=3, log=()):
    return {"order_key": order_key, "acuity": acuity, "nurse_proposed_acuity": nurse, "audit_log": list(log)}


# ---- I2: the key follows the acuity ------------------------------------------


def test_a_key_that_moves_with_the_acuity_is_fine():
    ok, why = datalog.order_key_follows_acuity([_cp((3, 1.0)), _cp((2, 1.0), acuity=2)])
    assert (ok, why) == (True, "")


def test_a_key_that_moves_with_a_refile_is_fine():
    """A re-file resets the acuity and re-keys on the nurse's new number — the
    spec allows it because the patient's acuity really changed."""
    ok, _ = datalog.order_key_follows_acuity([_cp((3, 1.0), acuity=3, nurse=3),
                                              _cp((4, 1.0), acuity=None, nurse=4)])
    assert ok


def test_the_first_key_is_not_a_change():
    ok, _ = datalog.order_key_follows_acuity([_cp(None), _cp((3, 1.0))])
    assert ok


def test_a_key_that_moves_on_its_own_is_caught():
    """The clock re-sorting a patient, or any write that is not an acuity
    change: the forbidden sequence I2 exists for."""
    ok, why = datalog.order_key_follows_acuity([_cp((3, 1.0), acuity=3), _cp((3, 99.0), acuity=3)])
    assert not ok
    assert "checkpoint 1" in why and "no change of acuity" in why


# ---- I21 rides along -----------------------------------------------------------


def test_history_invariants_reports_both():
    log = [{"action": "a"}]
    verdicts = datalog.history_invariants([_cp((3, 1.0), log=log), _cp((3, 1.0), log=log + [{"action": "b"}])])
    assert verdicts == {"audit_log_append_only": (True, ""), "order_key_follows_acuity": (True, "")}


def test_a_shrunken_audit_log_is_caught_through_the_same_call():
    verdicts = datalog.history_invariants([_cp((3, 1.0), log=[{"action": "a"}, {"action": "b"}]),
                                           _cp((3, 1.0), log=[{"action": "a"}])])
    ok, why = verdicts["audit_log_append_only"]
    assert not ok and "shrank" in why


# ---- and it actually runs ---------------------------------------------------------


def test_a_real_case_passes_both_over_its_persisted_history(checkpoint_db):
    case = dict(DEMO_CASES["clean"], case_id="case-history-check")
    runner.start_case(case)

    verdicts = runner.case_history_check("case-history-check")

    assert verdicts["audit_log_append_only"] == [True, ""]
    assert verdicts["order_key_follows_acuity"] == [True, ""]
    assert len(runner.history("case-history-check")) > 1      # it read a real history, not one snapshot
