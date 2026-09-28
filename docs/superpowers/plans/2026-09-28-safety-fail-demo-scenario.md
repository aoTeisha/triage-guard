# Safety-Fail Demo Scenario Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a board demo scenario that genuinely pauses a case at the human-approval
gate with `escalation_reason == "safety_fail"`, so the "Corrected" / "Escalate
further" buttons and the `senior_reminder` escalation path — both real, already-tested
code — are actually reachable and visible from the board UI, not just from
`triage-app`'s own test suite.

**Architecture:** None of the six existing board demo scenarios (`clean`, `missing`,
`failed`, `gap`, `injection`, `trace_violation`) ever produce a real safety-validator
failure: `app/symbolic/rules/safety.pl`'s five rules all check for internal
self-contradictions the graph's own code is built never to produce (a level with no
source, an unconfirmed level claimed as human-confirmed, a number nobody proposed).
There is no well-formed intake payload that trips them, by design — that is exactly
what makes the validator worth having.

`triage-app/tests/test_gates.py` already has the answer for how to *demo* this
honestly: `_failing_safety(monkeypatch)` patches `app.actors.safety.validate` to
return a fixed `SafetyVerdict(verdict="fail", ...)`, then runs the real clean demo
case through the real graph. Every node downstream of that point — `safety_validating`
writing the verdict into state and the audit log, `awaiting_human_approval` pausing
with `escalation_reason="safety_fail"`, `route_gate` sending "escalate further" or an
exhausted correction loop to `escalate_to_senior`, the real `senior_reminder` timer —
runs unmodified and for real. Only the *input* to the validator is faked, exactly the
same shape of fakery the `trace_violation` demo already uses for the post-run trace
check (plant a fact, let the real checker react to it).

This plan reuses that exact technique as a new board submission type, `safety_fail`,
scoped to one `unittest.mock.patch` around a single `runner.start_case()` call in
`board/board/intake.py`'s `/api/submit` handler. No change to `triage-app`'s graph,
nodes, or Prolog/Datalog rules. The frontend already renders everything this needs —
`GATE_OPTIONS["safety_fail"]`, `GATE_HEADINGS["safety_fail"]`, the "severe" red
styling, and the `escalate_further`/`corrected` buttons are already wired in
`board/board/static/labels.js` and `case-actions.js` for a reason string that has
simply never been produced live before. The only frontend gap is the demo-scenario
radio button to launch it.

**Tech Stack:** Python (FastAPI board service, LangGraph, pytest, `unittest.mock`),
vanilla JS/HTML (board static frontend, no build step).

**Spec:** No separate design doc — this plan is scoped entirely from
`triage-app/app/symbolic/rules/safety.pl`, `triage-app/app/actors/safety.py`,
`triage-app/app/graph/nodes/gate.py`, `triage-app/app/graph/routers.py:route_gate`,
`triage-app/tests/test_gates.py` (the existing `_failing_safety` precedent), and
`board/board/mock_cases.py` / `board/board/intake.py` (the existing `trace_violation`
demo precedent this plan mirrors).

## Global Constraints

- Never fake the *verdict a nurse sees* directly (no writing `escalation_reason` or
  `safety_verdict` straight into state) — only fake the validator's *input*
  (`app.actors.safety.validate`'s return value), the same boundary
  `_failing_safety` already patches in `triage-app/tests/test_gates.py:129`.
- The patch must be scoped to exactly one `runner.start_case()` call (a `with`
  block), never left active past that call — a global unscoped patch would corrupt
  every other case's safety validation for as long as it stayed applied.
- No new dependency: `unittest.mock.patch` is stdlib, already imported the same way
  in `triage-app/tests/test_gates.py`.
- Reuse the existing `SubmissionType` / `build_case` / `/api/demo-cases` machinery in
  `board/board/mock_cases.py` — a new literal member is enough for
  `GET /api/demo-cases` to list it automatically (`get_args(SubmissionType)` in
  `board/board/intake.py:demo_cases`).
- No changes to `board/board/static/labels.js` — `GATE_OPTIONS["safety_fail"]` and
  `GATE_HEADINGS["safety_fail"]` already exist and are already correct.

## Review Focus

- **A concurrent real submission during the patch window.** The patch replaces
  `app.actors.safety.validate` process-wide for the duration of one `start_case()`
  call. A second nurse's real case classified in a different thread during that
  same window would also get the faked "fail" verdict. This is a real, accepted
  limitation (see Task 2's `ponytail:` comment) — the test in Task 2 pins the
  *single-call* behavior; it does not attempt to prove thread-safety, which would
  be over-engineering for a demo tool one person drives at a time.
- **The patched verdict must reach the real audit log and the real gate, not a
  short-circuit.** A wrong patch target (e.g. patching `safety.facts_from` instead
  of `safety.validate`, or patching the wrong module path so the patch silently
  does nothing) would make the demo case complete normally with no visible
  difference. Task 2's test asserts on the actual HTTP response shape
  (`status == "awaiting_human_approval"`, `gate.gate == "safety_fail"`), not just
  that the patch was called.
- **The scenario must show up in the demo listing before it's wired to `/submit`.**
  `GET /api/demo-cases` iterates `get_args(SubmissionType)` and calls `build_case`
  for each — a new literal with no `build_case` branch would 500 that endpoint for
  every scenario, not just the new one. Task 1's test checks the listing endpoint
  specifically.
- **The corrected/escalate-further buttons must work against a plain HTTP resume,
  not just the graph-level `Command(resume=...)` the triage-app tests use.** Task 3
  drives it through `POST /api/case/{case_id}/resume`, the same door the board UI
  uses, not `graph.invoke` directly.
- **Resuming with a role the safety-fail gate refuses.** `may_resolve_gate` refuses
  `nurse` for any gate, and refuses even `charge_nurse` once `senior_required` is
  true. Task 3 checks both: a `nurse` resolving the initial safety-fail gate is
  denied, and a `charge_nurse` resolving it after escalation to a senior is denied
  too, only `shift_lead` succeeding.

---

## File Structure

- Modify: `board/board/mock_cases.py` — add `"safety_fail"` to the `SubmissionType`
  literal and to `build_case`'s clean-payload branch (it needs no special fields;
  the fakery lives entirely in `intake.py`, not in the payload).
- Modify: `board/board/intake.py` — patch `app.actors.safety.validate` around the
  `runner.start_case(case)` call when `submission_type == "safety_fail"`.
- Modify: `board/board/static/index.html` — one new radio option in the demo
  scenario list.
- Modify: `board/tests/intake/test_case_lifecycle.py` — three new tests (listing,
  the genuine pause, the escalate-further/senior-reminder path through HTTP).

## Task 1: List the new scenario

**Files:**
- Modify: `board/board/mock_cases.py:23` (the `SubmissionType` literal),
  `board/board/mock_cases.py:56-64` (the clean-payload branch in `build_case`)
- Test: `board/tests/intake/test_case_lifecycle.py`

**Interfaces:**
- Consumes: `board.mock_cases.build_case(lookup_result, national_id, submission_type)`
  — existing signature, unchanged.
- Produces: `SubmissionType` now includes `"safety_fail"`; `build_case(..., "safety_fail")`
  returns the same shape as `build_case(..., "clean")` minus `case_id`.

- [ ] **Step 1: Write the failing test**

```python
# board/tests/intake/test_case_lifecycle.py

def test_safety_fail_scenario_is_listed_and_looks_like_a_clean_case():
    listing = client.get("/api/demo-cases").json()["cases"]

    assert "safety_fail" in listing
    case = listing["safety_fail"]
    assert case["chief_complaint"] == "chest_pain"
    assert case["nurse_proposed_acuity"] == 3
    assert "vitals" in case
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd board && uv run pytest tests/intake/test_case_lifecycle.py::test_safety_fail_scenario_is_listed_and_looks_like_a_clean_case -v`
Expected: FAIL — `KeyError: 'safety_fail'`, or a 500 from `/api/demo-cases` if the
literal was added without a `build_case` branch (see Review Focus).

- [ ] **Step 3: Add the literal and the payload branch**

In `board/board/mock_cases.py`, change:

```python
SubmissionType = Literal["clean", "missing", "failed", "injection", "gap", "trace_violation"]
```

to:

```python
SubmissionType = Literal["clean", "missing", "failed", "injection", "gap",
                          "trace_violation", "safety_fail"]
```

And change the existing clean-payload branch:

```python
    if submission_type in ("clean", "trace_violation"):
```

to:

```python
    # safety_fail also starts as a clean case; app/board/intake.py is what
    # makes its safety verdict fail, not this payload.
    if submission_type in ("clean", "trace_violation", "safety_fail"):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd board && uv run pytest tests/intake/test_case_lifecycle.py::test_safety_fail_scenario_is_listed_and_looks_like_a_clean_case -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add board/board/mock_cases.py board/tests/intake/test_case_lifecycle.py
git commit -m "feat: list a safety_fail demo scenario (payload only, not yet wired)"
```

## Task 2: Make the scenario genuinely fail safety validation

**Files:**
- Modify: `board/board/intake.py` (imports and `submit()`)
- Test: `board/tests/intake/test_case_lifecycle.py`

**Interfaces:**
- Consumes: `app.actors.safety.validate(case: dict) -> SafetyVerdict` (existing,
  `triage-app/app/actors/safety.py:78`); `app.schemas.SafetyVerdict` (existing,
  `verdict: Literal["pass","fail"]`, `reasons: list[str]`); `runner.start_case(case)`
  (existing, `board/board/intake.py:212` already calls it).
- Produces: `POST /api/submit` with `submission_type: "safety_fail"` now returns a
  case paused with `status == "awaiting_human_approval"`, `gate.gate ==
  "safety_fail"`, `gate.required_role == "charge_nurse"`, `safety_reasons`
  non-empty.

- [ ] **Step 1: Write the failing test**

```python
# board/tests/intake/test_case_lifecycle.py

@respx.mock
def test_the_safety_fail_scenario_genuinely_pauses_at_the_safety_gate():
    _crm()

    body = _submit("safety_fail")

    assert body["status"] == "awaiting_human_approval"
    assert body["gate"]["gate"] == "safety_fail"
    assert body["gate"]["required_role"] == "charge_nurse"
    assert body["safety_reasons"] == ["planted by the safety-fail demo scenario"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd board && uv run pytest tests/intake/test_case_lifecycle.py::test_the_safety_fail_scenario_genuinely_pauses_at_the_safety_gate -v`
Expected: FAIL — `body["status"] == "settled"` (or whatever a clean case ordinarily
settles to), since nothing yet makes the validator fail for this submission type.

- [ ] **Step 3: Wire the patch into `submit()`**

In `board/board/intake.py`, add to the imports:

```python
from unittest.mock import patch

from app.schemas import SafetyVerdict
```

Then change the body of `submit()` where it currently calls `runner.start_case`:

```python
    try:
        state, pending = runner.start_case(case)
    except runner.CaseClosedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
```

to:

```python
    try:
        if body.submission_type == "safety_fail":
            # ponytail: patches the safety validator's input for exactly this
            # one call — every other concurrent submission during the same
            # window would also see this faked verdict. A single-operator
            # demo tool; add per-call isolation (e.g. threading a fake
            # validator through the request instead of a global patch) if
            # this ever needs to run with more than one person driving it.
            with patch("app.actors.safety.validate",
                       return_value=SafetyVerdict(
                           verdict="fail",
                           reasons=["planted by the safety-fail demo scenario"])):
                state, pending = runner.start_case(case)
        else:
            state, pending = runner.start_case(case)
    except runner.CaseClosedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd board && uv run pytest tests/intake/test_case_lifecycle.py::test_the_safety_fail_scenario_genuinely_pauses_at_the_safety_gate -v`
Expected: PASS

- [ ] **Step 5: Run the full board test suite to confirm no regression**

Run: `cd board && uv run pytest -q`
Expected: PASS, same collected count as before plus the two new tests so far.

- [ ] **Step 6: Commit**

```bash
git add board/board/intake.py board/tests/intake/test_case_lifecycle.py
git commit -m "feat: safety_fail demo scenario genuinely fails the safety gate"
```

## Task 3: Resolve the gate through the real HTTP endpoints

**Files:**
- Test: `board/tests/intake/test_case_lifecycle.py` (no production code change —
  this task proves the existing `/api/case/{case_id}/resume` endpoint,
  `app/graph/nodes/gate.py`, and `app/graph/routers.py:route_gate` already handle
  this correctly; it is the missing coverage, not a fix)

**Interfaces:**
- Consumes: `POST /api/case/{case_id}/resume` with body
  `{"decision": str, "resolver_role": str, "corrections": dict | None}` (existing,
  `board/board/intake.py:ResumeRequest`).
- Produces: nothing new — this task only adds assertions.

- [ ] **Step 1: Write the failing test for an unauthorized resolver**

```python
# board/tests/intake/test_case_lifecycle.py

@respx.mock
def test_a_nurse_cannot_resolve_the_safety_fail_gate():
    _crm()
    paused = _submit("safety_fail")

    denied = client.post(
        f"/api/case/{paused['case_id']}/resume",
        json={"decision": "escalate_further", "resolver_role": "nurse"},
    ).json()

    assert denied["status"] == "awaiting_human_approval"
    assert denied["gate"]["gate"] == "safety_fail"
```

Run: `cd board && uv run pytest tests/intake/test_case_lifecycle.py::test_a_nurse_cannot_resolve_the_safety_fail_gate -v`
Expected: this one should already PASS if Task 2 is correct — `may_resolve_gate`
already refuses `nurse` for any gate (`app/symbolic/prolog.py:94`). If it fails,
Task 2's wiring is not producing a real gate; stop and re-check Task 2 before
continuing.

- [ ] **Step 2: Write the failing test for escalate-further and the senior reminder**

```python
# board/tests/intake/test_case_lifecycle.py

@respx.mock
def test_escalate_further_hands_the_safety_fail_case_to_a_shift_lead():
    _crm()
    paused = _submit("safety_fail")

    escalated = client.post(
        f"/api/case/{paused['case_id']}/resume",
        json={"decision": "escalate_further", "resolver_role": "charge_nurse"},
    ).json()

    # Still open — a senior now has to decide, the case is not resolved.
    assert escalated["status"] == "awaiting_human_approval"
    assert escalated["gate"]["gate"] == "safety_fail"

    # A charge nurse is no longer enough once a senior is required (I8).
    still_charge_nurse = client.post(
        f"/api/case/{paused['case_id']}/resume",
        json={"decision": "escalate_further", "resolver_role": "charge_nurse"},
    ).json()
    assert still_charge_nurse["status"] == "awaiting_human_approval"

    from app.monitor.timers import connection

    conn = connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM timers WHERE case_id=%s AND kind='senior_reminder'",
        (paused["case_id"],),
    ).fetchone()[0]
    assert count == 1
```

Run: `cd board && uv run pytest tests/intake/test_case_lifecycle.py::test_escalate_further_hands_the_safety_fail_case_to_a_shift_lead -v`
Expected: FAIL only if the timer table query or connection helper import is wrong
for this codebase's actual `app.monitor.timers` API — check
`triage-app/app/monitor/timers.py` for the real `connection()` signature before
running; if it takes no arguments and returns a raw DB-API connection with
`.execute`, the test above is correct as written. If it instead requires a
context manager, adjust to match (e.g. `with connection() as conn:`).

- [ ] **Step 3: Run both new tests and confirm they pass against the existing code**

Run: `cd board && uv run pytest tests/intake/test_case_lifecycle.py -k "safety_fail or escalate_further" -v`
Expected: PASS for all four tests added across Tasks 1-3. No production code
changes are expected in this task — if any assertion fails, the bug is in
`app/graph/nodes/gate.py` or `app/graph/routers.py:route_gate`, not in anything
this plan added; stop and report the discrepancy rather than patching around it.

- [ ] **Step 4: Commit**

```bash
git add board/tests/intake/test_case_lifecycle.py
git commit -m "test: cover the safety_fail gate through the board's HTTP endpoints"
```

## Task 4: Add the demo scenario to the board UI

**Files:**
- Modify: `board/board/static/index.html:361-366`

**Interfaces:**
- Consumes: nothing new — the existing `submission_type` radio group.
- Produces: a nurse can select and submit the scenario from the board page.

- [ ] **Step 1: Add the radio option**

In `board/board/static/index.html`, change:

```html
        <label><input type="radio" name="submission_type" value="trace_violation" /> Trace violation — tamper with the log</label>
```

to:

```html
        <label><input type="radio" name="submission_type" value="trace_violation" /> Trace violation — tamper with the log</label>
        <label><input type="radio" name="submission_type" value="safety_fail" /> Safety validation failure — pauses for a charge nurse to correct or escalate</label>
```

- [ ] **Step 2: Manual check in the browser**

Run: `cd board && uv run board` (or however this project's `run` skill starts it —
check `.vscode/launch.json` / project README for the exact command if unsure).

Open the board, choose "Demo case scenarios", select "Safety validation failure",
submit, and confirm:
- The case panel shows a red "Resolve gate" section headed "Safety validation
  failed — correct and revalidate".
- The reason list shows "planted by the safety-fail demo scenario".
- Selecting role "charge_nurse" and clicking "Escalate further" leaves the panel
  open and adds a notification once the timer is manually fired, or at minimum
  does not error.
- Selecting "corrected", picking a different ESI level, and submitting clears the
  case to the queue.

- [ ] **Step 3: Commit**

```bash
git add board/board/static/index.html
git commit -m "feat: add the safety_fail demo scenario to the board UI"
```

---

## Self-Review

**1. Spec coverage.** The plan's own "spec" is the union of the safety-validator
code, the gate/router code, and the two existing demo-scenario precedents
(`trace_violation` in board, `_failing_safety` in triage-app tests). All three are
covered: Task 1 mirrors `trace_violation`'s payload-listing shape, Task 2 mirrors
`_failing_safety`'s patch technique but scoped to a live HTTP call instead of a
pytest fixture, Task 3 proves the existing gate/router/timer code (untouched)
behaves correctly when driven from real HTTP, Task 4 closes the frontend gap that
started this whole investigation.

**2. Placeholder scan.** No TBD/TODO/"add error handling" phrases; every step has
runnable code or an exact shell command.

**3. Type consistency.** `SafetyVerdict(verdict="fail", reasons=[...])` matches the
constructor already used in `triage-app/tests/test_gates.py:132`. `ResumeRequest`'s
fields (`decision`, `resolver_role`, `corrections`) match `board/board/intake.py`'s
existing model. `SubmissionType` literal addition matches the existing
`Literal[...]` style in `board/board/mock_cases.py:23`.

**4. Review Focus.** Five items listed above, each with its owning task: the
concurrency ceiling (Task 2, documented not built-around), a silently-wrong patch
target (Task 2's test asserts the real HTTP shape), the demo-listing endpoint
breaking for every scenario if `build_case` isn't updated with the literal (Task
1's test hits the listing endpoint, not just `build_case` directly), driving the
gate through real HTTP rather than `graph.invoke` (Task 3), and the two
authorization edges — `nurse` always refused, `charge_nurse` refused once a senior
is required (Task 3, both tested).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-28-safety-fail-demo-scenario.md`. Please review the plan. Which execution approach would you prefer?

- **Subagent-driven** — a fresh subagent implements each task and a fresh reviewer checks it before the next one starts, then a whole-branch review at the end. Most thorough; costs a fresh context per task and per review.
- **Native** — I implement every task myself in this session, then one fresh reviewer on the most capable model checks the whole branch. Cheapest and fastest; no independent review until the end.

For this plan I recommend **Native**: the four tasks are small, strictly sequential (each depends on the previous one's literal/endpoint existing), touch at most two files each, and the riskiest part — whether the patch genuinely reaches the real gate — is caught immediately by Task 2's own test rather than surfacing later. A per-task subagent handoff would mostly add overhead here. Does the plan capture what you want, and which approach should we use?
