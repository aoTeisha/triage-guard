# Acuity Classifier Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ground the classifier in the ESI criteria, retire the brittle red-flag regex, change the gap-1 gate resolution to settle on the nurse's value, switch intake to structured-only fields, and add a deterministic ESI decision-point-D vitals annotation.

**Architecture:** All four ESI decision points live in the Acuity Classifier actor. The LLM performs A, B and C by judgment; `app/esi.py` holds decision point D as pure threshold data and `classify()` is its only caller, returning any breaches alongside the proposal as an **annotation that changes no acuity**. The graph node writes what the actor returned and computes nothing. The handbook treats D as a judgment applied in context, and its own worked examples rule out a mechanical threshold. `acuity_source = rule_forced` is removed entirely. The gate's gap-1 band settles to `nurse_proposed_acuity`.

**Tech Stack:** Python 3, Pydantic v2, LangGraph, pytest, uv.

**Design doc:** `docs/plans/2026-09-13-acuity-classifier-design.md`
**Source of truth for thresholds:** `docs/Emergency_Severity_Index_Handbook.pdf` (ESI v5, Figure 6-1)

---

## Sequencing

Tasks 1–4 change real behaviour and are unblocked. Do them first.

Task 5 is blocked on a clinician agreeing the intake field list.

Task 6 is last for two reasons. The `esi.py` module and its tests can be written and
passed today, but **wiring it into the classifier needs `age_band`, which Task 5
delivers** — decision point D cannot run without knowing the patient's age band. And
even once wired, the annotation changes no acuity and has no display surface until the
patient card exists, so it produces no visible effect.

| # | Task | Blocked? |
| --- | --- | --- |
| 1 | Gap of 1 settles to the nurse | no |
| 2 | Retire the red flags, ground the prompt | no |
| 3 | Remove `rule_forced` from the state model | no |
| 4 | Delete the dead urgency scorer | no |
| 5 | Structured intake | **yes** — needs the field list |
| 6 | The ESI vitals annotation | `esi.py` buildable now; **wiring it needs `age_band` from Task 5** |
| 7 | Reconcile the edge tests | after 1–3 |

---

## Task 1: Gap of 1 settles to the nurse

**Files:**
- Modify: `triage-app/app/deterministic.py:46-63` — `resolve_acuity`
- Test: `triage-app/tests/test_gates.py`

**Step 1: Write the failing test**

```python
def test_gap_of_one_settles_to_the_nurse():
    """Changed 2026-09-13. Was min(nurse, system); see the design doc.

    The classifier over-triages systematically (median ESI 2.0 vs experts' 3.0), so it
    should not silently win every close call.
    """
    from app.deterministic import resolve_acuity
    from app.states import AcuitySource

    final, source, _ = resolve_acuity(nurse=3, system=2)
    assert final == 3
    assert source is AcuitySource.AUTO_RESOLVED

    final, _, _ = resolve_acuity(nurse=2, system=3)
    assert final == 2
```

**Step 2: Run to verify it fails**

Run: `cd triage-app && uv run pytest tests/test_gates.py::test_gap_of_one_settles_to_the_nurse -v`
Expected: FAIL — `assert 2 == 3`

**Step 3: Change the band**

```python
    if gap == 1:
        # The nurse holds a gap of 1 (SPECIFICATION.md 9b). Was "take more acute" until
        # 2026-09-13; changed because the classifier over-triages systematically and
        # should not win close calls unseen. Both inputs are logged at 9b.
        return nurse, AcuitySource.AUTO_RESOLVED, Arrow.ACUITY_GAP_MINOR
```

Update the docstring band table: `gap 1 -> take the NURSE's value (9b, auto_resolved)`.

**Step 4: Run the full suite**

Run: `cd triage-app && uv run pytest tests/ -v`
Expected: PASS after updating any test asserting the old band.

**Step 5: Commit**

```bash
git add triage-app/app/deterministic.py triage-app/tests
git commit -m "feat: gap of 1 settles to the nurse's acuity"
```

---

## Task 2: Retire the red flags, ground the prompt

**Files:**
- Modify: `triage-app/app/actors/acuity_classifier.py` — delete `_RED_FLAGS`, `_SCANNED_FIELDS`, `red_flag()`, `_forced_emergent()`, `import re`
- Modify: `triage-app/app/actors/acuity_classifier.json` — add the ESI criteria
- Modify: `triage-app/app/actors/mocks/acuity_classifier.json` — `acuity_source` becomes `"system"`
- Test: `triage-app/tests/test_gates.py`

**Step 1: Write the failing test**

```python
def test_red_flag_machinery_is_gone():
    """Replaced by the classifier's own judgment at ESI decision point B."""
    from app.actors import acuity_classifier

    assert not hasattr(acuity_classifier, "red_flag")
    assert not hasattr(acuity_classifier, "_RED_FLAGS")
    assert not hasattr(acuity_classifier, "_forced_emergent")


def test_a_chest_pain_case_no_longer_bypasses_the_model(monkeypatch):
    from app.actors import acuity_classifier

    called = []
    monkeypatch.setattr(acuity_classifier, "live_mode", lambda: False)
    real = acuity_classifier.load_mock
    monkeypatch.setattr(
        acuity_classifier, "load_mock",
        lambda name: called.append(name) or real(name),
    )
    acuity_classifier.classify({"chief_complaint": "crushing chest pain"})
    assert "acuity_classifier" in called
```

**Step 2: Run to verify it fails**

Run: `cd triage-app && uv run pytest tests/test_gates.py -k red_flag -v`
Expected: FAIL — the attributes still exist

**Step 3: Simplify the classifier**

`classify` becomes the model call alone:

```python
def classify(payload: dict[str, Any]) -> AcuityProposal:
    """Propose an acuity for one redacted payload.

    The model performs ESI decision points A, B and C by judgment; all three need
    clinical reading, and ESI defines B by judgment with worked examples rather than a
    closed list. Decision point D is computed separately and only annotates the case.

    Raises on transport failure so the graph's RetryPolicy can see it — swallowing the
    exception here would make the retry budget unreachable.
    """
    if not live_mode():
        return AcuityProposal.model_validate(load_mock("acuity_classifier"))
    return _model().invoke(_prompt(payload))
```

**Step 4: Ground the persona**

In `acuity_classifier.json`, extend `backstory` with decision points A, B and C from the
handbook: the four level-2 questions, the high-risk examples, and the resource table
(the design doc has it transcribed). **Do not put the decision point D vitals table in
the prompt** — it is computed deterministically, and telling the model the answer
anchors it.

**Step 5: Run the suite**

Run: `cd triage-app && uv run pytest tests/ -v`
Expected: `rule_forced` assertions fail — that is Task 3.

**Step 6: Commit**

```bash
git add triage-app/app/actors triage-app/tests
git commit -m "feat: ground the classifier in ESI criteria, retire the red-flag regex"
```

---

## Task 3: Remove `rule_forced` from the state model

**Files:**
- Modify: `triage-app/app/states.py` — drop `RULE_FORCED` from `AcuitySource`
- Modify: `triage-app/app/graph/state.py` — drop `red_flag_fired`
- Modify: `triage-app/app/graph/nodes/classify.py:43` — drop the `red_flag_fired` write
- Modify: `triage-app/app/schemas/acuity_proposal.py` — narrow the `acuity_source` literal

**Step 1: Find every reference**

Run: `cd triage-app && grep -rn "rule_forced\|RULE_FORCED\|red_flag_fired" app/ tests/`

**Step 2: Remove them, then run the suite**

Run: `cd triage-app && uv run pytest tests/ -v`
Expected: failures only in tests asserting red-flag behaviour. Delete those tests — the
behaviour is gone by design, not broken.

**Step 3: Commit**

```bash
git add -A triage-app/app triage-app/tests
git commit -m "refactor: remove rule_forced acuity provenance"
```

---

## Task 4: Delete the dead urgency scorer

**Files:**
- Modify: `triage-app/app/actors/normalizer.py` — delete `score_urgency`, `ScorerUnavailable`, `FREE_TEXT_KEYS`, the `drop_free_text` parameter
- Modify: `triage-app/app/graph/nodes/redaction.py` — delete the commented-out block and `scorer_update`
- Modify: `triage-app/app/graph/state.py` — delete `UrgencyScores` and the field
- Modify: `triage-app/app/graph/__init__.py` — drop the export
- Modify: `triage-app/app/budgets.py` — drop the `pii_bert_ner` budget
- Delete: `triage-app/app/actors/mocks/input_normalizer.json`
- Delete: the skipped test in `triage-app/tests/test_failures.py`

Switched off on 2026-09-13; its output was never read by anything. This removes it
properly rather than leaving commented code.

Run: `cd triage-app && uv run pytest tests/ -v`
Expected: PASS.

```bash
git add -A triage-app
git commit -m "refactor: remove the unused BERT urgency scorer"
```

---

## Task 5: Structured intake — BLOCKED

**Blocked on:** the field list, which needs a clinician (spec Open decisions #5).

**Files:**
- Modify: `triage-app/app/guards/fields.py` — drop `free_text`, add the new fields, `rr` and `age_band`
- Modify: `triage-app/app/mock_cases.py` — rewrite the four demo cases
- Modify: `triage-app/app/guards/injection.py`, `validity.py:18`, `actors/intake.py:28`
- Modify: `triage-app/app/actors/normalizer.py` — `age_band` must survive `drop_identifiers`
- Test: `triage-app/tests/test_guards.py`

**Two fields are non-negotiable**, because decision point D cannot run without them:

- `rr` — respiratory rate. Not currently collected.
- `age_band` — one of `<1mo`, `1-12mo`, `1-3y`, `3-5y`, `5-12y`, `12-18y`, `>18y`.
  Comes from the CRM record. **Pass the band, not the date of birth** — DOB is a direct
  identifier and `drop_identifiers` strips it, so the clinical rule would lose the input
  the privacy rule removes.

**Keep the injection guard.** Dropdown values still need validating: an "Other" box, an
unexpected enum value, or anything reaching the prompt is still an injection surface.
Point `detect_injection` at the structured fields rather than deleting the call.

---

## Task 6: The ESI vitals annotation

**Buildable now, but inert** — it changes no acuity and its display surface (the patient
card) does not exist. Do it after 1–4, and expect no visible effect until the board is
built.

**Files:**
- Create: `triage-app/app/esi.py`
- Modify: `triage-app/app/actors/acuity_classifier.py` — `classify()` calls `esi.breaches`
- Modify: `triage-app/app/schemas/acuity_proposal.py` — add `danger_zone_vitals`
- Modify: `triage-app/app/graph/nodes/classify.py` — write the field through to `flags`
- Test: `triage-app/tests/test_esi.py`

**Step 1: Write the failing tests**

```python
"""ESI decision point D — the age-banded danger-zone vitals check.

Thresholds from the ESI v5 handbook, Figure 6-1. The check ANNOTATES; it never
changes an acuity. See the design doc for why (handbook ch. 6, Examples Four and Five).
"""

import pytest

from app import esi

ADULT_NORMAL = {"hr": 78, "rr": 16, "spo2": 99}


def test_normal_adult_vitals_breach_nothing():
    assert esi.breaches(ADULT_NORMAL, ">18y") == []


@pytest.mark.parametrize("age_band,hr", [
    ("<1mo", 195), ("1-12mo", 185), ("1-3y", 145),
    ("3-5y", 125), ("5-12y", 125), ("12-18y", 105), (">18y", 105),
])
def test_heart_rate_thresholds_are_age_banded(age_band, hr):
    assert "hr" in esi.breaches({"hr": hr}, age_band)


def test_a_toddler_heart_rate_is_normal_for_a_toddler_and_high_for_an_adult():
    """130 is inside the 1-3y band and outside the adult band."""
    assert esi.breaches({"hr": 130}, "1-3y") == []
    assert esi.breaches({"hr": 130}, ">18y") == ["hr"]


def test_spo2_threshold_is_the_same_at_every_age():
    for band in esi.THRESHOLDS:
        assert "spo2" in esi.breaches({"spo2": 88}, band)


def test_a_missing_vital_is_not_a_breach():
    """Absent is not abnormal. Field presence is the intake guard's job."""
    assert esi.breaches({"hr": 78}, ">18y") == []


def test_an_unknown_age_band_raises():
    """Fail loudly rather than silently applying adult limits to an infant."""
    with pytest.raises(KeyError):
        esi.breaches(ADULT_NORMAL, "grown-up")
```

**Step 2: Run to verify it fails**

Run: `cd triage-app && uv run pytest tests/test_esi.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.esi'`

**Step 3: Write the implementation**

```python
"""ESI decision point D — the age-banded danger-zone vitals check.

Transcribed from the ESI v5 handbook, Figure 6-1
(docs/Emergency_Severity_Index_Handbook.pdf, ch. 6).

This ANNOTATES a case. It never changes an acuity, because the handbook treats D as a
judgment applied in clinical context and its own worked examples prove a mechanical
threshold wrong: Example Four leaves a patient at level 3 with HR 102 against a limit
of 100, and Example Five uptriages on SpO2 91% only because of an infected wound and
steroid immunosuppression. Code cannot tell those apart.

This is ONE rule from ESI, not ESI. Decision points A, B and C are the classifier's
judgment and are not encoded.

Pure functions over plain values. No graph, no state, no I/O.
"""

from __future__ import annotations

from typing import Any

# (hr_max, rr_max) per age band. A vital strictly above the limit is a breach.
THRESHOLDS: dict[str, tuple[int, int]] = {
    "<1mo":   (190, 60),
    "1-12mo": (180, 55),
    "1-3y":   (140, 40),
    "3-5y":   (120, 35),
    "5-12y":  (120, 30),
    "12-18y": (100, 20),
    ">18y":   (100, 20),
}

SPO2_MIN = 92   # same at every age


def breaches(vitals: dict[str, Any], age_band: str) -> list[str]:
    """Which collected vitals sit outside the ESI danger-zone limits for this age.

    A vital that was not collected is not a breach — absence is the field-presence
    guard's problem. An unknown age band raises rather than defaulting, so an infant is
    never silently measured against adult limits.
    """
    hr_max, rr_max = THRESHOLDS[age_band]
    hr, rr, spo2 = vitals.get("hr"), vitals.get("rr"), vitals.get("spo2")
    out = []
    if hr is not None and hr > hr_max:
        out.append("hr")
    if rr is not None and rr > rr_max:
        out.append("rr")
    if spo2 is not None and spo2 < SPO2_MIN:
        out.append("spo2")
    return out
```

**Step 4: Run to verify it passes**

Run: `cd triage-app && uv run pytest tests/test_esi.py -v`
Expected: all PASS

**Step 5: Compute it in the classifier, not the node**

Add to `AcuityProposal`:

```python
    danger_zone_vitals: list[str] = Field(
        default_factory=list,
        description="ESI decision point D breaches. Annotation only — never alters the level.",
    )
```

Then in `classify()`, after the model returns:

```python
    proposal = _model().invoke(_prompt(payload))
    return proposal.model_copy(update={
        "danger_zone_vitals": esi.breaches(
            payload.get("vitals") or {}, payload["age_band"],
        ),
    })
```

The node then only writes it through — no computation:

```python
    if checked.danger_zone_vitals:
        update["flags"] = [f"danger_zone_vitals:{','.join(checked.danger_zone_vitals)}"]
```

**Step 6: Commit**

```bash
git add triage-app/app/esi.py triage-app/app/actors/acuity_classifier.py triage-app/app/schemas/acuity_proposal.py triage-app/app/graph/nodes/classify.py triage-app/tests/test_esi.py
git commit -m "feat: annotate cases with ESI decision point D vitals breaches"
```

---

## Task 7: Reconcile the edge tests with the spec

**Files:**
- Modify: `triage-app/tests/test_edges.py`

`test_edges.py` asserts the graph wiring against the spec's transition table. Arrow 9b
changed name (`auto_resolve_acuity_upward` → `auto_resolve_acuity_to_nurse`) and the
classifier's action note changed. Check for assertions on those strings.

Run: `cd triage-app && uv run pytest tests/ -v`
Expected: full suite PASS.

```bash
git add triage-app/tests/test_edges.py
git commit -m "test: reconcile edge assertions with the updated spec"
```

---

## Not in this plan

**The eval set.** Labelled cases with known-correct ESI levels are the only way to tell
whether any of this works, and none exists. Largest remaining gap; its own piece of work.

**The patient card.** Task 6's annotation has nowhere to display until the board is
built. Until then the check runs and writes to the audit log and nobody reads it.

**The safety actor's rules.** Still empty, deliberately deferred. The open question when
it is picked up: whether anything re-checks the *settled* acuity, since the nurse-wins
rule can land on a number no ESI computation produced.
