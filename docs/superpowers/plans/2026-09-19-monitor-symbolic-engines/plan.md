# Monitor Symbolic Engines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monitor's Python guard stubs with real Prolog, Datalog, BPpy and OPA engines, each with one clear role, fail-closed, without changing the monitor's public API.

**Architecture:** New `app/symbolic/` package holds the engine wrappers and rule files (Prolog rules, Rego policy, Datalog rules). `app/monitor/bthreads.py` holds the BPpy b-threads. `fire.handle` becomes the single entry for a claimed timer: build one context dict → BPpy selects the event → Prolog must agree → OPA gates the side effect → existing executors run. The sweeper ends every tick with a Datalog pass over all timers/cases — the deadline arm of the spec's **temporal monitor** — that escalates invariant breaks to a technician.

**Tech Stack:** Python 3.12, `pyswip` (SWI-Prolog 9.0.4), `pyDatalog` 0.22.4, `bppy` 1.0.4, OPA 1.20.x static binary (`opa eval`), psycopg 3 + Postgres, LangGraph, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-monitor-symbolic-engines-design.md`, which implements the monitor's share of `docs/SPECIFICATION.md` § Safety invariants (I1–I18, finalized 2026-09-19).

**Revised 2026-09-20** against HEAD `c8a6d43`, 11 commits after the first draft. What changed and why is in `findings.md` § "Re-review 2026-09-20".

## Invariants this plan implements

| Invariant | Spec says checked by | This plan |
| --- | --- | --- |
| **I14 Authorization** — every human action permitted by the actor's role, *with an explanation* | Prolog (roles, permissions, explanation) | Task 1: `rules/monitor.pl` `charge_role/1`, `may_resolve_gate/2` (incl. the `senior_required` → `shift_lead` rule), `denial/3` for the BLK "why" |
| **I5 No bypass** — queue/treatment only after safety + approval | OPA | Task 2: `monitor.rego` `move` rules |
| **I9 Release** — releasable from any pause, valid reason only | OPA | Task 2: `monitor.rego` `release` rules, reached from every pause via `_shared.release_case` |
| **I15 Escalation** — waiting case escalates within T, widening | timers; temporal monitor | Task 4 (reminder decisions incl. `senior_reminder` → `any_shift_lead`), Task 6 (`unwatched_case`) |
| **I16 Reassessment** — reassessment required within T(acuity) | timers + sweeper; temporal monitor | Task 4 (`UNKNOWN` never redispatches blind), Task 6 (`unwatched_case` = a queued patient with no live timer) |
| **I18 Audit** — every change *and every refused attempt* recorded with a reason | checkpoint cross-check | Tasks 2/4: `audit_denial(layer=…)` names the engine; `timers.decision` records what was chosen and what was blocked |

Out of scope (named here so nobody thinks they were missed): I3/I11 Datalog provenance + information flow, I4/I13 Z3, I1/I2/I6/I7/I8/I10/I12/I17. The spec's Datalog paragraph describes provenance and information flow — a *different* Datalog use from Task 6's deadline queries, which are the temporal monitor's arm over the timer store.

Two of the spec's **forbidden sequences** (§ Transitions) get a detector here:
*"System fails to escalate a waiter — a waiting case passes T without the system
raising an escalation"* is what Task 6's `unwatched_case` finds, and
*"Unauthorized action — any human action by a person whose role does not permit
it"* is what Task 1's Prolog refuses and Task 2 records with its denying layer,
which is exactly the `ACTION_DENIED` event's declared payload ("denying layer +
reason", § Events).

This work also fills a literal blank in the spec: the **Waiting Room Monitor
Agent**'s Tech column (§ Actors) still reads *(to confirm)*. Prolog + BPpy + OPA
+ the Datalog pass is the answer; updating that cell is the user's call, not a
plan task.

## Global Constraints

- Never run `git add`/`commit`/`push` — the user commits manually (repo memory rule). Plan steps therefore have no commit steps.
- All commands run from `triage-guard/triage-app/` with `uv run …`. Tests need the Postgres container: `docker compose -f ../db/docker-compose.yml up -d postgres` (port 5434).
- Fail-closed: an engine that is missing or errors is a **deny**, recorded in `last_error` / the deny reason, never an exception out of the sweeper and never a silent Python fallback.
- Public signatures unchanged: `fire.fire_id/dispatch/reconcile/notify`, `fire.RECONCILE_BUDGET`, `fire.NOTIFICATION_BUDGET_PER_WINDOW`, `sweeper.run_once(conn, *, worker_id, graph=None)`, `deterministic.actor_is_charge/move_authorized/release_authorized`.
- **Baseline is red.** HEAD has two pre-existing failures unrelated to this work (see Task 0b and `findings.md`). Task 0b fixes the monitor one. The CRM one (`tests/test_failures.py::test_a_crm_outage_degrades_and_continues`, `tests/test_graph.py::test_clean_case_walks_the_documented_arrows_in_order`) is **out of scope** — until the user fixes it, "suite green" means: those two fail, nothing else does. Check with `uv run pytest -q`; the expected tail is `2 failed`.
- Timer columns are real types now: `due_at` and `lease_until` are `TIMESTAMPTZ`, SQL uses `now()`, and `timers.due_in` returns a `datetime`. Schema lives in `app/monitor/schema.sql`, not a Python string. Claim queries return `dict_row` dicts.
- Match the codebase's comment style: docstrings explain *why*, `# noqa: BLE001 — reason` on broad excepts, `# ponytail:` on deliberate shortcuts. The monitor's prose was tightened in `c8a6d43` — keep new comments equally short.

---

## File map

| File | Responsibility |
| --- | --- |
| `app/symbolic/__init__.py` | package docstring only |
| `app/symbolic/prolog.py` | pyswip wrapper: `charge_role`, `may_resolve_gate`, `timer_action`, `atom`; one global engine + lock |
| `app/symbolic/rules/monitor.pl` | Prolog rules: roles, per-timer action, denial explanations |
| `app/symbolic/opa.py` | `evaluate(input)` via `opa eval` subprocess, fail-closed |
| `app/symbolic/policy/monitor.rego` | OPA policy: dispatch / notify / move / release |
| `app/symbolic/datalog.py` | pyDatalog: `tick_invariants(timer_rows, case_rows)` |
| `app/monitor/bthreads.py` | BPpy b-threads: `select_action(ctx)` |
| `app/monitor/fire.py` | `_GATE_RUNG_RECIPIENTS` restored (Task 0b), `handle`, `_context`, OPA gates |
| `app/monitor/sweeper.py` | route every claimed timer through `fire.handle`; temporal-monitor pass per tick |
| `app/monitor/schema.sql` | `decision` column on `timers` |
| `app/monitor/timers.py` | `record_decision`, `escalation_exists`, `all_rows` |
| `app/deterministic.py` | guards delegate to engines; `audit_denial(..., layer=)` |
| `app/graph/nodes/terminal.py`, `_shared.py`, `gate.py` | denials name the real denying layer |
| `pyproject.toml`, `README.md`, `app/monitor/README.md` | deps, install, docs |
| `tests/symbolic/test_{prolog,opa,datalog}.py`, `tests/monitor/test_bthreads.py`, additions to `test_fire.py` / `test_sweeper.py` / `test_denial.py` | tests |

---

### Task 0: Toolchain — Python deps + OPA binary

**Files:**
- Modify: `pyproject.toml` (dependencies)
- Modify: `README.md` (new "Symbolic engines" install section)

**Interfaces:**
- Produces: importable `pyswip`, `pyDatalog`, `bppy`; `opa` on `PATH` (or `$OPA_BIN`).

- [ ] **Step 1: Add the three Python deps**

In `pyproject.toml` `dependencies`, after the last entry, add:

```toml
    # Symbolic governance layer for the monitor (docs/superpowers/specs/2026-09-19-monitor-symbolic-engines-design.md).
    "pyswip>=0.3.3",     # Prolog — needs SWI-Prolog (`swipl`) installed on the host
    "pyDatalog>=0.22",   # Datalog — pure Python
    "bppy>=1.0.4",       # Behavioral programming — pure Python
```

- [ ] **Step 2: Install**

Run: `uv sync`
Expected: resolves and installs the three packages (pyDatalog emits a `SyntaxWarning: invalid escape sequence` on import — harmless, upstream).

- [ ] **Step 3: Install the OPA binary (user-local, no sudo)**

Run:
```bash
mkdir -p ~/.local/bin
curl -sL -o ~/.local/bin/opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod +x ~/.local/bin/opa
~/.local/bin/opa version
```
Expected: `Version: 1.x.y`. If `~/.local/bin` is not on `PATH`, export `OPA_BIN=$HOME/.local/bin/opa` in the shell that runs tests/sweeper (and in `.env`).

- [ ] **Step 4: Verify everything imports**

Run: `uv run python -c "import pyswip, pyDatalog, bppy; from pyswip import Prolog; print(list(Prolog.query('X = 1')))"` and `swipl --version`
Expected: `[{'X': 1}]` and `SWI-Prolog version 9.…`.

- [ ] **Step 5: Document the host requirements in `README.md`**

Append after the existing `## Run` section:

```markdown
## Symbolic engines (monitor)

The waiting-room monitor's decisions are made by real engines, not Python
`if`s — see `app/monitor/README.md` § "The symbolic layers". Two host binaries
are required; without them every guarded action is **denied** (fail-closed),
the sweeper keeps running and records `engine_unavailable:<engine>`.

```bash
sudo apt-get install swi-prolog                       # Prolog (pyswip links to it)
curl -sL -o ~/.local/bin/opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod +x ~/.local/bin/opa                             # OPA; or point OPA_BIN at it
```
```

- [ ] **Step 6: Record the baseline**

Run: `uv run pytest -q 2>&1 | tail -8`
Expected: 6 failed — 4 × `NameError: _GATE_RUNG_RECIPIENTS` (Task 0b fixes them) and 2 × CRM degrade (out of scope). Write the exact list into `progress.md` so later tasks compare against it.

---

### Task 0b: Unbreak `fire.notify` — restore `_GATE_RUNG_RECIPIENTS`

`c8a6d43` deleted the constant but kept the reference at `app/monitor/fire.py:130`, so every gate reminder raises `NameError`. Four tests fail. This is the monitor's own bug and must be fixed before anything is layered on top.

**Files:**
- Modify: `app/monitor/fire.py` (add the constant next to `_REMINDER_PAUSES`)
- Test: existing `tests/monitor/test_fire.py::test_notify_*`, `tests/test_denial.py::test_a_refused_gate_keeps_its_reminders_deliverable`

**Interfaces:**
- Produces: `fire._GATE_RUNG_RECIPIENTS: dict[int, str]`, indexed by `cycle % len(...)` (gate cycles are `visit + rung`, not 0/1 — see `gate.awaiting_human_approval`).

- [ ] **Step 1: Run the four failing tests and read the error**

Run: `uv run pytest tests/monitor/test_fire.py -q -k notify`
Expected: 3 failed, `NameError: name '_GATE_RUNG_RECIPIENTS' is not defined` at `app/monitor/fire.py:130`.

- [ ] **Step 2: Restore the constant**

In `app/monitor/fire.py`, directly above `_REMINDER_PAUSES`:

```python
# Gate reminder rungs, by `cycle % len(...)`: rung 0 nudges the nurse assigned
# to the case, rung 1 widens to any charge nurse (I15). Cycles are
# `visit + rung`, so each gate visit gets its own timer ids.
_GATE_RUNG_RECIPIENTS = {0: "assigned_nurse", 1: "any_charge_nurse"}
```

- [ ] **Step 3: Verify**

Run: `uv run pytest tests/monitor/test_fire.py tests/test_denial.py -q`
Expected: all pass.

- [ ] **Step 4: New baseline**

Run: `uv run pytest -q 2>&1 | tail -4`
Expected: `2 failed` — only the two CRM ones. That is the green bar for every later task.

---

### Task 1: Prolog engine + rules; `actor_is_charge` delegates

**Files:**
- Create: `app/symbolic/__init__.py`, `app/symbolic/prolog.py`, `app/symbolic/rules/monitor.pl`
- Modify: `app/deterministic.py` (`actor_is_charge`)
- Test: `tests/symbolic/__init__.py` (empty), `tests/symbolic/test_prolog.py`

**Interfaces:**
- Produces: `prolog.atom(value) -> str`; `prolog.charge_role(role) -> tuple[bool, str]`; `prolog.may_resolve_gate(role, *, senior_required: bool) -> tuple[bool, str]`; `prolog.timer_action(ctx) -> tuple[str, str]` where the action is one of `"dispatch" | "reconcile" | "notify" | "cancel" | "fail_budget" | "engine_unavailable"` and the second element is the explanation. `ctx` keys read: `timer_id, kind, fire_state, pause_active, notify_count, notify_budget`.

- [ ] **Step 1: Write the failing tests**

`tests/symbolic/__init__.py`: empty file.

`tests/symbolic/test_prolog.py`:

```python
"""The Prolog layer (`app.symbolic.prolog` + `rules/monitor.pl`): I14
authorization and per-timer classification, each with an explanation.
Runs the real SWI-Prolog engine through pyswip — no mocks.
"""

from __future__ import annotations

from app.deterministic import actor_is_charge
from app.symbolic import prolog


def _ctx(**over):
    base = {"timer_id": "c1:reassessment:0", "kind": "reassessment", "fire_state": "DUE",
            "pause_active": True, "notify_count": 0, "notify_budget": 5}
    return base | over


def test_charge_roles_are_the_two_the_spec_names():
    assert prolog.charge_role("charge_nurse") == (True, "charge_nurse holds charge role")
    assert prolog.charge_role("shift_lead")[0] is True
    assert prolog.charge_role("nurse")[0] is False
    assert prolog.charge_role("")[0] is False


def test_a_refusal_explains_itself():
    ok, why = prolog.charge_role("nurse")
    assert not ok and why == "gate refused: role 'nurse' is not a charge role"


def test_a_case_handed_to_a_senior_needs_a_shift_lead(): 
    """I14 + I8: once `senior_required` is set, a charge nurse is no longer enough."""
    assert prolog.may_resolve_gate("shift_lead", senior_required=True)[0] is True
    ok, why = prolog.may_resolve_gate("charge_nurse", senior_required=True)
    assert not ok and why == "gate refused: role 'charge_nurse'; a shift lead must decide"
    assert prolog.may_resolve_gate("charge_nurse", senior_required=False)[0] is True


def test_roles_from_http_input_are_quoted_not_interpolated():
    # A quote in the role must neither crash the engine nor be parsed as Prolog.
    assert prolog.charge_role("o'brien")[0] is False
    assert prolog.charge_role("x). charge_role(y")[0] is False
    assert prolog.atom("o'brien") == "'o\\'brien'"


def test_actor_is_charge_is_the_prolog_engine():
    assert actor_is_charge("charge_nurse") == prolog.charge_role("charge_nurse")
    assert actor_is_charge("nurse") == prolog.charge_role("nurse")


def test_due_reassessment_dispatches():
    assert prolog.timer_action(_ctx()) == ("dispatch", "")


def test_failed_reassessment_redispatches():
    assert prolog.timer_action(_ctx(fire_state="FAILED"))[0] == "dispatch"


def test_unknown_reassessment_must_reconcile_never_dispatch():
    action, why = prolog.timer_action(_ctx(fire_state="UNKNOWN"))
    assert action == "reconcile"
    assert why == "dispatch: blind_redispatch_from_unknown"


def test_lease_expired_dispatching_counts_as_unknown():
    assert prolog.timer_action(_ctx(fire_state="DISPATCHING"))[0] == "reconcile"


def test_every_reminder_kind_notifies_while_its_pause_is_open():
    for kind in ("gate_reminder", "reassessment_reminder", "senior_reminder"):
        assert prolog.timer_action(_ctx(timer_id=f"c1:{kind}:0", kind=kind)) == ("notify", "")


def test_reminder_whose_pause_resolved_is_cancelled():
    action, why = prolog.timer_action(_ctx(timer_id="c1:gate_reminder:0", kind="gate_reminder", pause_active=False))
    assert action == "cancel"
    assert why == "notify: reminder_pause_resolved"


def test_reminder_over_budget_fails_loudly():
    action, why = prolog.timer_action(
        _ctx(timer_id="c1:senior_reminder:0", kind="senior_reminder", notify_count=5, notify_budget=5))
    assert action == "fail_budget"
    assert why == "notify: notification_budget_exhausted"


def test_unknown_timer_kind_is_cancelled_not_delivered_blind():
    assert prolog.timer_action(_ctx(timer_id="c1:safety_park:0", kind="safety_park"))[0] == "cancel"


def test_facts_do_not_leak_between_queries():
    prolog.timer_action(_ctx(fire_state="UNKNOWN"))
    assert prolog.timer_action(_ctx())[0] == "dispatch"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/symbolic/test_prolog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.symbolic'`.

- [ ] **Step 3: Write the rules file**

`app/symbolic/rules/monitor.pl`:

```prolog
%% The monitor's reasoning layer (I14 authorization; the per-timer decision
%% cross-checked against app/monitor/bthreads.py).
%%
%% Facts about ONE timer are asserted by app/symbolic/prolog.py right before a
%% query and retracted right after, so the process-wide engine never mixes two
%% timers:
%%   timer(Id, Kind, FireState).   pause_active(Id).
%%   notify_count(Id, N).          notify_budget(Id, B).

:- dynamic timer/3, pause_active/1, notify_count/2, notify_budget/2.

%% I14: who may act. A charge role resolves the gate and signs a release;
%% once a case has been handed up (senior_required), only a shift lead may.
%%
%% Role only. The spec's `actor_authorized` guard is role ∧ jurisdiction ∧
%% data-class, and I14 says "according to the server's staff records" — so the
%% full rule needs staff facts (who is on shift, in which ward, at what
%% clearance) that no store holds yet. Modelling role alone here matches what
%% the code can actually check today; the other two conjuncts get their own
%% facts and an extra rule when that store exists.
charge_role(charge_nurse).
charge_role(shift_lead).
senior_role(shift_lead).

may_resolve_gate(Role, true)  :- senior_role(Role).
may_resolve_gate(Role, false) :- charge_role(Role).

reminder_kind(gate_reminder).
reminder_kind(reassessment_reminder).
reminder_kind(senior_reminder).

%% A fire whose acknowledgment was lost. Never re-dispatched blind: a second,
%% unnecessary delivery could re-notify a nurse twice, or worse.
unacknowledged('UNKNOWN').
unacknowledged('DISPATCHING').

budget_spent(T) :- notify_count(T, N), notify_budget(T, B), N >= B.

%% action(Timer, Action): exactly one action per timer.
action(T, reconcile)   :- timer(T, reassessment, S), unacknowledged(S).
action(T, dispatch)    :- timer(T, reassessment, S), \+ unacknowledged(S).
action(T, cancel)      :- timer(T, K, _), reminder_kind(K), \+ pause_active(T).
action(T, fail_budget) :- timer(T, K, _), reminder_kind(K), pause_active(T), budget_spent(T).
action(T, notify)      :- timer(T, K, _), reminder_kind(K), pause_active(T), \+ budget_spent(T).
%% An unrecognized kind has no pause to check, so it is cancelled rather than delivered blind.
action(T, cancel)      :- timer(T, K, _), K \== reassessment, \+ reminder_kind(K).

%% denial(Event, Timer, Why): the explanation behind every blocked event —
%% what the BLK row records as its "why" (I18).
denial(dispatch, T, blind_redispatch_from_unknown) :- timer(T, _, S), unacknowledged(S).
denial(notify, T, reminder_pause_resolved)         :- timer(T, K, _), reminder_kind(K), \+ pause_active(T).
denial(notify, T, notification_budget_exhausted)   :- timer(T, K, _), reminder_kind(K), budget_spent(T).
```

- [ ] **Step 4: Write the package + wrapper**

`app/symbolic/__init__.py`:

```python
"""The symbolic governance layer (docs/SPECIFICATION.md § Symbolic governance
layer): real Prolog, Datalog and OPA engines behind small Python wrappers.
The graph and the monitor *propose*; these decide, and every refusal comes
with a reason (I18). A wrapper that cannot reach its engine refuses — it
never falls back to a Python approximation.
"""
```

`app/symbolic/prolog.py`:

```python
"""Prolog over `rules/monitor.pl` (SWI-Prolog via pyswip): who may act (I14),
and what should happen to one claimed timer — with the explanation every BLK
row needs.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from pyswip import Prolog

RULES = Path(__file__).parent / "rules" / "monitor.pl"

# ponytail: one process-wide engine behind one lock. pyswip's engine is a
# global and is not thread-safe; the board's FastAPI threadpool reaches
# `actor_is_charge` from several threads. Per-thread engines if this lock
# ever shows up in a profile.
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _engine() -> Prolog:
    engine = Prolog()
    engine.consult(str(RULES))
    return engine


def atom(value: Any) -> str:
    """Quote a Python value as a Prolog atom. Roles and ids arrive from HTTP
    input, so they are never interpolated raw into a goal — a stray quote
    would otherwise be parsed as Prolog.
    """
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _holds(goal: str) -> bool:
    with _lock:
        return bool(list(_engine().query(goal)))


def charge_role(role: str) -> tuple[bool, str]:
    """A charge role resolves the gate and signs a release (I14)."""
    if _holds(f"charge_role({atom(role)})"):
        return True, f"{role} holds charge role"
    return False, f"gate refused: role {role!r} is not a charge role"


def may_resolve_gate(role: str, *, senior_required: bool) -> tuple[bool, str]:
    """Who may answer this gate: a charge role normally, only a shift lead
    once the correction loop handed the case up (I8/I14).
    """
    if _holds(f"may_resolve_gate({atom(role)}, {str(senior_required).lower()})"):
        return True, (f"{role} may decide" if not senior_required else "a shift lead must decide")
    if senior_required:
        return False, f"gate refused: role {role!r}; a shift lead must decide"
    return False, f"gate refused: role {role!r} is not a charge role"


def timer_action(ctx: dict[str, Any]) -> tuple[str, str]:
    """`(action, why)` for one claimed timer, from the same facts the BPpy
    b-threads select on — so `fire.handle` can insist the two layers agree.
    `why` lists every `denial/3` that holds, joined by `; `, or is empty.
    An engine failure returns `("engine_unavailable", <error>)` so the
    caller refuses rather than guesses.
    """
    tid = atom(ctx["timer_id"])
    facts = [
        f"timer({tid}, {atom(ctx['kind'])}, {atom(ctx['fire_state'])})",
        f"notify_count({tid}, {int(ctx['notify_count'])})",
        f"notify_budget({tid}, {int(ctx['notify_budget'])})",
    ]
    if ctx["pause_active"]:
        facts.append(f"pause_active({tid})")
    with _lock:
        engine = _engine()
        try:
            for fact in facts:
                engine.assertz(fact)
            actions = [s["A"] for s in engine.query(f"action({tid}, A)")]
            denials = [f"{s['E']}: {s['W']}" for s in engine.query(f"denial(E, {tid}, W)")]
        except Exception as exc:  # noqa: BLE001 — any engine fault is a refusal, never a guess
            return "engine_unavailable", f"prolog: {exc}"
        finally:
            for pred in (f"timer({tid}, _, _)", f"notify_count({tid}, _)",
                         f"notify_budget({tid}, _)", f"pause_active({tid})"):
                engine.retractall(pred)
    return (actions[0] if actions else "cancel"), "; ".join(denials)
```

- [ ] **Step 5: Delegate `actor_is_charge`**

In `app/deterministic.py`, add to the imports:

```python
from app.symbolic import prolog
```

Replace the body of `actor_is_charge` with:

```python
def actor_is_charge(actor_role: str) -> tuple[bool, str]:
    """Prolog authorization guard on the human gate (I14): only a charge-role
    nurse may resolve an acuity discrepancy or sign off a safety correction.
    Answered by the real engine over `app/symbolic/rules/monitor.pl`.
    """
    return prolog.charge_role(actor_role)
```

Leave `gate.awaiting_human_approval`'s `senior_required` branch as it is for now — Task 2 Step 6 moves it onto `prolog.may_resolve_gate`, once the `layer=` kwarg it needs exists.

- [ ] **Step 6: Run the new tests**

Run: `uv run pytest tests/symbolic/test_prolog.py -q`
Expected: 14 passed.

- [ ] **Step 7: Full suite**

Run: `uv run pytest -q`
Expected: the Task 0b baseline — 2 failed (CRM), nothing else.

---

### Task 2: OPA engine + policy; `move_authorized` / `release_authorized` delegate; denials name their layer

**Files:**
- Create: `app/symbolic/opa.py`, `app/symbolic/policy/monitor.rego`
- Modify: `app/deterministic.py` (`audit_denial`, `move_authorized`, `release_authorized`)
- Modify: `app/graph/nodes/_shared.py` (`release_case`), `app/graph/nodes/terminal.py` (`denied`), `app/graph/nodes/gate.py` (senior branch), `app/graph/routers.py` (`route_gate`'s duplicate of the same rule)
- Test: `tests/symbolic/test_opa.py`, `tests/test_denial.py` (one new test), `tests/test_routers.py` (one new test)

**Interfaces:**
- Produces: `opa.evaluate(input: dict) -> dict` returning `{"allow": bool, "deny_reasons": list[str]}`. Actions and their inputs:
  - `{"action": "dispatch", "case": {"control_state": str|None}, "timer": {"fire_state": str}}`
  - `{"action": "notify", "pause_active": bool, "notify_count": int, "notify_budget": int}`
  - `{"action": "move", "case": {"safety_passed": bool, "approved": bool}, "actor_role": str}`
  - `{"action": "release", "reason": str, "actor_role": str}`
- Produces: `audit_denial(case_id, control_state, why, layer: str = "Prolog (authorization)")`.

- [ ] **Step 1: Write the failing tests**

`tests/symbolic/test_opa.py`:

```python
"""The OPA gate (`app.symbolic.opa` + `policy/monitor.rego`), evaluated by the
real `opa` binary: I5 no bypass, I9 release, and the monitor's own dispatch
and notify gates. Default deny; every deny names its reason; a missing or
broken engine is a deny too.
"""

from __future__ import annotations

from app.symbolic import opa


def test_dispatch_is_allowed_from_the_monitoring_pause():
    assert opa.evaluate({"action": "dispatch", "case": {"control_state": "monitoring"},
                         "timer": {"fire_state": "DUE"}}) == {"allow": True, "deny_reasons": []}


def test_dispatch_from_unknown_is_denied_with_the_reason():
    decision = opa.evaluate({"action": "dispatch", "case": {"control_state": "monitoring"},
                             "timer": {"fire_state": "UNKNOWN"}})
    assert decision == {"allow": False, "deny_reasons": ["blind_redispatch_from_unknown"]}


def test_dispatch_off_the_pause_is_denied():
    decision = opa.evaluate({"action": "dispatch", "case": {"control_state": "reassessment_required"},
                             "timer": {"fire_state": "DUE"}})
    assert decision["allow"] is False
    assert "case_not_at_monitoring_pause" in decision["deny_reasons"]


def test_notify_needs_an_open_pause_and_budget():
    assert opa.evaluate({"action": "notify", "pause_active": True, "notify_count": 0, "notify_budget": 5})["allow"]
    assert opa.evaluate({"action": "notify", "pause_active": False, "notify_count": 0, "notify_budget": 5}) == \
        {"allow": False, "deny_reasons": ["reminder_pause_resolved"]}
    assert opa.evaluate({"action": "notify", "pause_active": True, "notify_count": 5, "notify_budget": 5}) == \
        {"allow": False, "deny_reasons": ["notification_budget_exhausted"]}


def test_move_needs_safety_approval_and_role():
    """I5: no bypass into treatment."""
    ok = {"action": "move", "case": {"safety_passed": True, "approved": True}, "actor_role": "nurse"}
    assert opa.evaluate(ok)["allow"] is True
    assert opa.evaluate(ok | {"actor_role": "porter"})["deny_reasons"] == ['move refused: role "porter" not authorized']
    assert opa.evaluate({**ok, "case": {"safety_passed": False, "approved": True}})["deny_reasons"] == \
        ["move refused: safety not passed"]
    assert opa.evaluate({**ok, "case": {"safety_passed": True, "approved": False}})["deny_reasons"] == \
        ["move refused: not approved"]


def test_release_needs_a_valid_reason_and_a_charge_role():
    """I9: releasable from any pause, but only with a valid reason and signer."""
    assert opa.evaluate({"action": "release", "reason": "discharge", "actor_role": "charge_nurse"})["allow"]
    assert opa.evaluate({"action": "release", "reason": "discharge", "actor_role": "nurse"})["deny_reasons"] == \
        ['release refused: gate refused: role "nurse" is not a charge role']
    assert opa.evaluate({"action": "release", "reason": "bogus", "actor_role": "charge_nurse"})["deny_reasons"] == \
        ['release refused: invalid reason "bogus"']


def test_unknown_action_is_denied_by_default():
    assert opa.evaluate({"action": "launch_missiles"}) == {"allow": False, "deny_reasons": ["unknown_action"]}


def test_a_missing_engine_is_a_deny_not_an_exception(monkeypatch):
    monkeypatch.setenv("OPA_BIN", "/nonexistent/opa")
    decision = opa.evaluate({"action": "release", "reason": "discharge", "actor_role": "charge_nurse"})
    assert decision["allow"] is False
    assert decision["deny_reasons"][0].startswith("engine_unavailable:opa")
```

Append to `tests/test_denial.py`:

```python
def test_a_denial_names_the_layer_that_refused():
    """I18: a BLK row that says 'denied' without saying who denied it is not
    an explanation. Move and release are OPA's; the gate is Prolog's."""
    from app.deterministic import audit_denial
    from app.states import State

    row = audit_denial("c1", State.MONITORING, "move refused: not approved", layer="OPA (authorization)")
    assert row["denying_layer"] == "OPA (authorization)"
    assert audit_denial("c1", State.AWAITING_HUMAN_APPROVAL, "x")["denying_layer"] == "Prolog (authorization)"
    # The arrow stays BLK whatever the layer — `routers.release_route` routes
    # off that arrow, so a new kwarg must not change it.
    assert row["arrow"] == "BLK"
```

Append to `tests/test_routers.py`, in the gate section:

```python
def test_only_a_shift_lead_routes_on_once_the_case_was_handed_up():
    """I14, the same rule the gate node applies — one engine, not two literals."""
    assert routers.route_gate(s(resolver_role="shift_lead", senior_required=True)) is Route.PROCEED
    assert routers.route_gate(s(resolver_role="charge_nurse", senior_required=True)) is Route.DENIED
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/symbolic/test_opa.py tests/test_denial.py -q`
Expected: FAIL — `ImportError: cannot import name 'opa'` and `TypeError: audit_denial() got an unexpected keyword argument 'layer'`.

- [ ] **Step 3: Write the policy**

`app/symbolic/policy/monitor.rego`:

```rego
# The monitor's runtime gate, evaluated by the real OPA engine
# (app/symbolic/opa.py) immediately before a side effect happens.
# Backs I5 (no bypass) and I9 (release). Whitelist: default deny; every deny
# carries a reason (I18).
package triage.monitor

import rego.v1

default allow := false

# --- dispatch: resume a paused case with REASSESSMENT_TIMEOUT (I16) --------
allow if {
	input.action == "dispatch"
	input.case.control_state == "monitoring"
	not input.timer.fire_state in {"UNKNOWN", "DISPATCHING"}
}

deny_reasons contains "case_not_at_monitoring_pause" if {
	input.action == "dispatch"
	input.case.control_state != "monitoring"
}

deny_reasons contains "blind_redispatch_from_unknown" if {
	input.action == "dispatch"
	input.timer.fire_state in {"UNKNOWN", "DISPATCHING"}
}

# --- notify: gate / re-filing / senior reminder (I15) -----------------------
allow if {
	input.action == "notify"
	input.pause_active == true
	input.notify_count < input.notify_budget
}

deny_reasons contains "reminder_pause_resolved" if {
	input.action == "notify"
	input.pause_active != true
}

deny_reasons contains "notification_budget_exhausted" if {
	input.action == "notify"
	input.notify_count >= input.notify_budget
}

# --- move: waiting room -> treatment (I5) ----------------------------------
move_roles := {"nurse", "charge_nurse", "shift_lead"}

allow if {
	input.action == "move"
	input.case.safety_passed == true
	input.case.approved == true
	input.actor_role in move_roles
}

deny_reasons contains "move refused: safety not passed" if {
	input.action == "move"
	input.case.safety_passed != true
}

deny_reasons contains "move refused: not approved" if {
	input.action == "move"
	input.case.approved != true
}

deny_reasons contains sprintf("move refused: role %q not authorized", [input.actor_role]) if {
	input.action == "move"
	not input.actor_role in move_roles
}

# --- release: sign the patient out, from any pause (I9) ---------------------
release_reasons := {"discharge", "ama", "transfer", "admit"}
charge_roles := {"charge_nurse", "shift_lead"}

allow if {
	input.action == "release"
	input.reason in release_reasons
	input.actor_role in charge_roles
}

deny_reasons contains sprintf("release refused: invalid reason %q", [input.reason]) if {
	input.action == "release"
	not input.reason in release_reasons
}

deny_reasons contains sprintf("release refused: gate refused: role %q is not a charge role", [input.actor_role]) if {
	input.action == "release"
	not input.actor_role in charge_roles
}

# --- anything else ----------------------------------------------------------
deny_reasons contains "unknown_action" if {
	not input.action in {"dispatch", "notify", "move", "release"}
}

decision := {"allow": allow, "deny_reasons": deny_reasons}
```

- [ ] **Step 4: Write the wrapper**

`app/symbolic/opa.py`:

```python
"""The OPA runtime gate: `opa eval` over `policy/monitor.rego`, called right
before a side effect (resuming a case, sending a reminder, moving or
releasing a patient). Fail-closed by construction: anything that stops the
engine from proving `allow` is a deny with a reason, never an exception.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

POLICY = Path(__file__).parent / "policy" / "monitor.rego"
QUERY = "data.triage.monitor.decision"


def evaluate(input: dict[str, Any]) -> dict[str, Any]:
    """`{"allow": bool, "deny_reasons": [str]}` for one proposed action.

    The binary is `opa` on PATH, or whatever `OPA_BIN` points at. Read per
    call, not at import, so a test can point it at nothing and prove the
    deny path.
    """
    # ponytail: one subprocess per call (~12 ms measured). `opa run --server`
    # sidecar + httpx if the sweeper ever gates hundreds of actions a second.
    try:
        completed = subprocess.run(
            [os.environ.get("OPA_BIN", "opa"), "eval", "-d", str(POLICY), "-I", "--format=raw", QUERY],
            input=json.dumps(input), capture_output=True, text=True, timeout=5, check=True,
        )
        decision = json.loads(completed.stdout)
        return {"allow": decision["allow"] is True, "deny_reasons": list(decision["deny_reasons"])}
    except Exception as exc:  # noqa: BLE001 — missing binary, timeout, policy error, bad JSON: all mean "cannot prove allowed"
        return {"allow": False, "deny_reasons": [f"engine_unavailable:opa ({type(exc).__name__}: {exc})"]}
```

- [ ] **Step 5: Delegate the two guards and add `layer` to `audit_denial`**

In `app/deterministic.py`, change the Task 1 import to:

```python
from app.symbolic import opa, prolog
```

Replace `audit_denial` with:

```python
def audit_denial(case_id: str, control_state: State, why: str,
                 layer: str = "Prolog (authorization)") -> dict[str, Any]:
    """The BLK row every refused attempt writes (I18). `layer` names which
    engine refused — the gate is Prolog's (I14), move and release are OPA's
    (I5/I9).
    """
    return audit(case_id, control_state, "explain_denial", why, Arrow.BLK, denying_layer=layer)
```

Replace `move_authorized` and `release_authorized` with:

```python
def move_authorized(
    safety_passed: bool, approved: bool, actor_role: str
) -> tuple[bool, str]:
    """OPA authorization for the treatment move (I5 no bypass), evaluated by
    the real engine over `app/symbolic/policy/monitor.rego`. A refusal is the
    BLK row: the attempt dies, the case does not move.
    """
    gate = opa.evaluate({"action": "move",
                         "case": {"safety_passed": bool(safety_passed), "approved": bool(approved)},
                         "actor_role": actor_role})
    return gate["allow"], ("move authorized" if gate["allow"] else "; ".join(gate["deny_reasons"]))


def release_authorized(reason: str, actor_role: str) -> tuple[bool, str]:
    """OPA authorization for release (I9): a valid reason plus an authorized
    signer. State-independent — release can happen from any pause, so this
    takes no source-state argument.
    """
    gate = opa.evaluate({"action": "release", "reason": reason, "actor_role": actor_role})
    return gate["allow"], ("release authorized" if gate["allow"] else "; ".join(gate["deny_reasons"]))
```

Replace the heading comment above `verify_no_identifiers` with:

```python
# ---- symbolic-layer predicates ---------------------------------------------
# `verify_no_identifiers` is still the Python skeleton (redaction/I11 is
# outside the monitor's scope); the three guards below it are answered by the
# real engines in `app.symbolic`.
```

- [ ] **Step 6: Name the layer at the three call sites**

`app/graph/nodes/_shared.py`, in `release_case`, the refusal branch:

```python
    if not authorized:
        return {"actor_role": actor_role,
                "audit_log": [audit_denial(state.case_id, at, why, layer="OPA (authorization)")]}
```

`app/graph/nodes/terminal.py`, the inner `denied` helper and the in-treatment refusal:

```python
    def denied(why: str, layer: str = "OPA (authorization)") -> dict[str, Any]:
        # Denial stays parked, not ended: a mis-typed actor_role is routine
        # input from a UI button. The case must stay retriable by a
        # legitimate follow-up, not fall out of the reassessment safety net.
        # The approval gate treats its own refusals the same way.
        return {
            "actor_role": actor_role,
            "audit_log": [audit_denial(state.case_id, State.MONITORING, why, layer=layer)],
        }
```

```python
            return denied("move refused: already in treatment", layer="monitor (idempotency)")
```

`app/graph/nodes/gate.py`, the resolver-authorization branch — one call into Prolog instead of two code paths:

```python
    resolver = (response or {}).get("resolver_role", "")
    decision = (response or {}).get("decision")
    authorized, why = prolog.may_resolve_gate(resolver, senior_required=state.senior_required)
```

with `from app.symbolic import prolog` added to its imports. The correction-required refusal lower down keeps the default layer (it is a Prolog-side rule about what the gate requires).

- [ ] **Step 7: Run the new tests**

Run: `uv run pytest tests/symbolic/test_opa.py tests/test_denial.py tests/test_gates.py tests/test_release_everywhere.py tests/test_treatment_move_and_release.py -q`
Expected: all pass.

- [ ] **Step 8: Full suite**

Run: `uv run pytest -q`
Expected: 2 failed (CRM baseline), nothing else.

---

### Task 3: BPpy b-threads — `select_action`

**Files:**
- Create: `app/monitor/bthreads.py`
- Test: `tests/monitor/test_bthreads.py`

**Interfaces:**
- Produces: `bthreads.select_action(ctx: dict) -> tuple[str, str]` = `(selected, proposed)`, both in `{"DISPATCH", "RECONCILE", "NOTIFY", "CANCEL", "FAIL_BUDGET"}`. `ctx` keys read: `kind, fire_state, pause_active, notify_count, notify_budget`. Deterministic; exactly one event per call.

- [ ] **Step 1: Write the failing tests**

`tests/monitor/test_bthreads.py`:

```python
"""The BPpy layer (`app.monitor.bthreads`): for one claimed timer, which
event the b-threads let through. Pure Python, no database, no graph.
"""

from __future__ import annotations

import pytest

from app.monitor import bthreads


def _ctx(**over):
    base = {"kind": "reassessment", "fire_state": "DUE", "pause_active": True,
            "notify_count": 0, "notify_budget": 5}
    return base | over


@pytest.mark.parametrize("ctx, selected, proposed", [
    (_ctx(), "DISPATCH", "DISPATCH"),
    (_ctx(fire_state="FAILED"), "DISPATCH", "DISPATCH"),
    (_ctx(fire_state="UNKNOWN"), "RECONCILE", "DISPATCH"),
    (_ctx(fire_state="DISPATCHING"), "RECONCILE", "DISPATCH"),
    (_ctx(kind="gate_reminder"), "NOTIFY", "NOTIFY"),
    (_ctx(kind="reassessment_reminder"), "NOTIFY", "NOTIFY"),
    (_ctx(kind="senior_reminder"), "NOTIFY", "NOTIFY"),
    (_ctx(kind="gate_reminder", pause_active=False), "CANCEL", "NOTIFY"),
    (_ctx(kind="senior_reminder", notify_count=5), "FAIL_BUDGET", "NOTIFY"),
    # A stale reminder is pointless whether or not the budget is spent: CANCEL wins.
    (_ctx(kind="gate_reminder", pause_active=False, notify_count=5), "CANCEL", "NOTIFY"),
    (_ctx(kind="safety_park"), "CANCEL", "CANCEL"),
])
def test_exactly_one_event_and_it_is_deterministic(ctx, selected, proposed):
    for _ in range(10):  # the arbiter is random among equal priorities; ours must never tie
        assert bthreads.select_action(ctx) == (selected, proposed)


def test_unknown_never_jumps_straight_back_to_dispatching():
    """The rule that matters most (app/monitor/README.md), and I16's teeth."""
    for state in ("UNKNOWN", "DISPATCHING"):
        assert bthreads.select_action(_ctx(fire_state=state))[0] != "DISPATCH"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/monitor/test_bthreads.py -q`
Expected: FAIL — `ImportError: cannot import name 'bthreads'`.

- [ ] **Step 3: Write the b-threads**

`app/monitor/bthreads.py`:

```python
"""Behavioral orchestration (BPpy) for the fire state machine: which event
one claimed timer gets.

The `proposer` asks for the naive thing — dispatch a reassessment, send a
reminder. Every safety rule is its own b-thread that, when its condition
holds, blocks that naive event and requests the safe one instead. The
arbiter picks a requested, unblocked event with the highest priority;
`one_decision` then blocks everything, so a run yields exactly one event.

Requirements don't check the decision after the fact here — they *are* the
decision. The same facts also go to Prolog (`app.symbolic.prolog`), and
`fire.handle` refuses to act if the two layers disagree.
"""

from __future__ import annotations

from typing import Any

import bppy as bp

DISPATCH = bp.BEvent("DISPATCH")
RECONCILE = bp.BEvent("RECONCILE")
NOTIFY = bp.BEvent("NOTIFY")
CANCEL = bp.BEvent("CANCEL")
FAIL_BUDGET = bp.BEvent("FAIL_BUDGET")

# Every notify-only kind. Mirrors `fire._REMINDER_PAUSES`.
REMINDER_KINDS = {"gate_reminder", "reassessment_reminder", "senior_reminder"}
# A fire whose acknowledgment was lost (or whose worker died mid-call).
UNACKNOWLEDGED = {"UNKNOWN", "DISPATCHING"}


def _proposed(ctx: dict[str, Any]) -> bp.BEvent:
    if ctx["kind"] == "reassessment":
        return DISPATCH
    if ctx["kind"] in REMINDER_KINDS:
        return NOTIFY
    return CANCEL  # unrecognized kind: no pause to check, never delivered blind


@bp.thread
def proposer(ctx):
    yield bp.sync(request=_proposed(ctx))


@bp.thread
def no_blind_redispatch(ctx):
    """UNKNOWN never jumps straight back to DISPATCHING: reconcile first."""
    if ctx["kind"] == "reassessment" and ctx["fire_state"] in UNACKNOWLEDGED:
        yield bp.sync(request=RECONCILE, block=DISPATCH, priority=1)
        while True:
            yield bp.sync(block=DISPATCH)


@bp.thread
def stale_reminder(ctx):
    """A reminder about a pause that's already resolved is cancelled, not sent.
    Highest priority: stale beats over-budget, since sending is off the table either way."""
    if ctx["kind"] in REMINDER_KINDS and not ctx["pause_active"]:
        yield bp.sync(request=CANCEL, block=NOTIFY, priority=2)
        while True:
            yield bp.sync(block=NOTIFY)


@bp.thread
def notify_budget(ctx):
    """Hitting the per-recipient cap is a recorded FAILED, never a silent drop."""
    if ctx["kind"] in REMINDER_KINDS and ctx["notify_count"] >= ctx["notify_budget"]:
        yield bp.sync(request=FAIL_BUDGET, block=NOTIFY, priority=1)
        while True:
            yield bp.sync(block=NOTIFY)


@bp.thread
def one_decision(ctx):
    """After the first event, block everything: one claimed timer, one decision."""
    yield bp.sync(waitFor=bp.All())
    while True:
        yield bp.sync(block=bp.All())


class _Selected(bp.BProgramRunnerListener):
    """Collects selected events. bppy's listener base is abstract on every
    hook, so the no-ops below are required, not decorative."""

    def __init__(self):
        self.events: list[str] = []

    def event_selected(self, b_program, event):
        self.events.append(event.name)

    def starting(self, *a, **k): pass
    def started(self, *a, **k): pass
    def super_step_done(self, *a, **k): pass
    def ended(self, *a, **k): pass
    def assertion_failed(self, *a, **k): pass
    def b_thread_added(self, *a, **k): pass
    def b_thread_removed(self, *a, **k): pass
    def b_thread_done(self, *a, **k): pass
    def halted(self, *a, **k): pass


def select_action(ctx: dict[str, Any]) -> tuple[str, str]:
    """`(selected, proposed)` for one timer. Equal means no rule intervened;
    different means `selected` is what a guard substituted for `proposed`.
    """
    listener = _Selected()
    bp.BProgram(
        bthreads=[proposer(ctx), no_blind_redispatch(ctx), stale_reminder(ctx),
                  notify_budget(ctx), one_decision(ctx)],
        event_selection_strategy=bp.PriorityBasedEventSelectionStrategy(default_priority=0),
        listener=listener,
    ).run()
    assert len(listener.events) == 1, listener.events  # one_decision guarantees this
    return listener.events[0], _proposed(ctx).name
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/monitor/test_bthreads.py -q`
Expected: 12 passed.

---

### Task 4: `fire.handle` — one context, BPpy selects, Prolog agrees, OPA gates; `decision` column

**Files:**
- Modify: `app/monitor/schema.sql` (add `decision`)
- Modify: `app/monitor/timers.py` (add `record_decision`)
- Modify: `app/monitor/fire.py` (add `_recipient_class`, `_context`, `handle`, `_send_reminder`; OPA gate inside `dispatch`; `notify` delegates)
- Test: `tests/monitor/test_fire.py` (additions)

**Interfaces:**
- Consumes: `bthreads.select_action`, `prolog.timer_action`, `opa.evaluate` (Tasks 1-3).
- Produces: `fire.handle(conn, timer: dict, *, graph) -> str` (a `fire_state`); `fire._recipient_class(timer) -> str`; `timers.record_decision(conn, timer_id: str, decision: str) -> None`; `timers.decision` column (TEXT, nullable).

- [ ] **Step 1: Write the failing tests**

Append to `tests/monitor/test_fire.py`:

```python
# ---- the symbolic layers on the fire path --------------------------------------
from app.symbolic import prolog


def _gate_timer(conn, graph, run, cycle=0):
    """A case parked at the human gate, plus a gate reminder row for it."""
    state, pending, thread = run(GAP_CASE)
    assert pending is not None and pending.get("gate") is not None
    timer_id = timers.schedule(conn, case_id=thread, kind="gate_reminder", cycle=cycle,
                                due_at="2000-01-01T00:00:00Z")
    return {"timer_id": timer_id, "case_id": thread, "kind": "gate_reminder", "cycle": cycle,
            "due_at": "2000-01-01T00:00:00Z"}


def test_handle_records_the_decision_and_dispatches(conn, graph, run):
    timer = _reach_monitoring(conn, graph, run)

    assert fire.handle(conn, timer, graph=graph) == "DELIVERED"

    row = conn.execute("SELECT decision FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row == ("DISPATCH",)


def test_handle_reconciles_an_unknown_timer_instead_of_redispatching(conn, graph, run):
    """I16 has teeth only if a lost ack is proved, not guessed."""
    timer = _reach_monitoring(conn, graph, run)
    fid = fire.fire_id(timer["case_id"], "reassessment", 0, timer["due_at"])
    timers.set_state(conn, timer["timer_id"], "UNKNOWN", fire_id=fid)

    outcome = fire.handle(conn, {**timer, "fire_state": "UNKNOWN", "fire_id": fid, "attempts": 0}, graph=graph)

    # reconcile proves the fire never applied: FAILED, retryable — and the case was NOT resumed
    assert outcome == "FAILED"
    assert hydrate(graph.get_state(config_for(timer["case_id"])).values)["reassessment_cycle"] == 0
    decision = conn.execute("SELECT decision FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()[0]
    assert decision == "RECONCILE (proposed DISPATCH: dispatch: blind_redispatch_from_unknown)"


def test_handle_refuses_when_prolog_and_bppy_disagree(conn, graph, run, monkeypatch):
    """Two layers derive the action from the same facts. If they ever differ,
    nothing executes — that's the point of enforcing a rule twice."""
    timer = _reach_monitoring(conn, graph, run)
    monkeypatch.setattr(prolog, "timer_action", lambda ctx: ("cancel", ""))

    assert fire.handle(conn, timer, graph=graph) == "FAILED"

    row = conn.execute("SELECT fire_state, last_error FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row[0] == "FAILED" and row[1] == "layer_disagreement: bppy=DISPATCH prolog=cancel"
    assert hydrate(graph.get_state(config_for(timer["case_id"])).values)["reassessment_cycle"] == 0


def test_handle_refuses_when_prolog_is_unavailable(conn, graph, run, monkeypatch):
    timer = _reach_monitoring(conn, graph, run)
    monkeypatch.setattr(prolog, "timer_action", lambda ctx: ("engine_unavailable", "prolog: boom"))

    assert fire.handle(conn, timer, graph=graph) == "FAILED"

    row = conn.execute("SELECT last_error FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row == ("engine_unavailable:prolog (prolog: boom)",)


def test_dispatch_is_denied_when_opa_is_unavailable(conn, graph, run, monkeypatch):
    """Fail-closed: no engine, no resume. The case stays exactly where it was."""
    timer = _reach_monitoring(conn, graph, run)
    monkeypatch.setenv("OPA_BIN", "/nonexistent/opa")

    assert fire.dispatch(conn, timer, graph=graph) == "FAILED"

    row = conn.execute("SELECT fire_state, last_error FROM timers WHERE timer_id=%s", (timer["timer_id"],)).fetchone()
    assert row[0] == "FAILED" and row[1].startswith("opa denied dispatch: engine_unavailable:opa")
    result = hydrate(graph.get_state(config_for(timer["case_id"])).values)
    assert result["control_state"] == State.MONITORING.value
    assert result["reassessment_cycle"] == 0


def test_notify_is_denied_when_opa_is_unavailable(conn, graph, run, monkeypatch):
    timer = _gate_timer(conn, graph, run)
    monkeypatch.setenv("OPA_BIN", "/nonexistent/opa")

    assert fire.notify(conn, timer, graph=graph) == "FAILED"

    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone() == (0,)


def test_handle_treats_an_unreachable_graph_as_store_unreachable(conn):
    class UnreachableGraph:
        def get_state(self, config):
            raise ConnectionError("graph unreachable")

    timers.schedule(conn, case_id="c9", kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timer = {"timer_id": "c9:reassessment:0", "case_id": "c9", "kind": "reassessment", "cycle": 0,
             "due_at": "2000-01-01T00:00:00Z"}

    assert fire.handle(conn, timer, graph=UnreachableGraph()) == "UNKNOWN"

    row = conn.execute("SELECT fire_state, attempts, last_error FROM timers WHERE timer_id=%s",
                       (timer["timer_id"],)).fetchone()
    assert row == ("UNKNOWN", 1, "graph unreachable")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/monitor/test_fire.py -q -k "handle or opa_is_unavailable"`
Expected: FAIL — `AttributeError: module 'app.monitor.fire' has no attribute 'handle'`, `column "decision" does not exist`.

- [ ] **Step 3: Add the `decision` column**

In `app/monitor/schema.sql`, after the `timers` table's `CREATE INDEX` line:

```sql
-- What the symbolic layers chose for this timer's latest claim, e.g.
-- "RECONCILE (proposed DISPATCH: dispatch: blind_redispatch_from_unknown)".
ALTER TABLE timers ADD COLUMN IF NOT EXISTS decision TEXT;
```

- [ ] **Step 4: Add `record_decision` to `timers.py`**

After `set_state`:

```python
def record_decision(conn: psycopg.Connection, timer_id: str, decision: str) -> None:
    """Record what the symbolic layers chose for this claim, and what they
    overrode (I18). Separate from `set_state`: a decision is written *before*
    the action it names runs, `fire_state` after.
    """
    conn.execute(
        "UPDATE timers SET decision = %s, updated_at = now() WHERE timer_id = %s",
        (decision, timer_id),
    )
```

- [ ] **Step 5: Add the decision path to `fire.py`**

Add to the imports:

```python
from app.monitor import bthreads, timers
from app.symbolic import opa, prolog
```

Replace the recipient expression inside `notify` with a named helper, placed above `dispatch`:

```python
def _recipient_class(timer: dict[str, Any]) -> str:
    """Who this reminder goes to (I15). Gate rungs widen with the cycle; a
    senior reminder always goes to a shift lead.
    """
    if timer["kind"] == "gate_reminder":
        return _GATE_RUNG_RECIPIENTS[timer["cycle"] % len(_GATE_RUNG_RECIPIENTS)]
    if timer["kind"] == "senior_reminder":
        return "any_shift_lead"
    return "any_charge_nurse"
```

Add `_context` and `handle` above `dispatch`:

```python
def _context(conn, timer: dict[str, Any], *, graph) -> dict[str, Any]:
    """The facts every symbolic layer reasons over for one claimed timer,
    built once so BPpy, Prolog and OPA all see the same snapshot. Raises if
    the graph is unreachable — `handle` turns that into the
    `store_unreachable` path rather than deciding on missing facts.
    """
    snapshot = graph.get_state(config_for(timer["case_id"]))
    pause = _REMINDER_PAUSES.get(timer["kind"])
    recipient = _recipient_class(timer)
    return {
        "timer_id": timer["timer_id"],
        "kind": timer["kind"],
        "fire_state": timer.get("fire_state") or "DUE",  # claim_due rows carry no fire_state
        "control_state": snapshot.values.get("control_state"),
        "case_exists": bool(snapshot.values),
        # `snapshot.next` is the only reliable "paused here" signal: the gate
        # pauses via `interrupt()` before it finishes, so while paused
        # `control_state` still shows the previous node.
        "pause_active": pause is not None and pause in snapshot.next,
        "notify_count": timers.notification_count_in_window(
            conn, recipient_class=recipient, window_minutes=NOTIFICATION_WINDOW_MINUTES),
        "notify_budget": NOTIFICATION_BUDGET_PER_WINDOW,
        "recipient_class": recipient,
    }


def handle(conn, timer: dict[str, Any], *, graph) -> str:
    """One claimed timer, start to finish: gather the facts, let the b-threads
    choose the event, insist Prolog reaches the same conclusion, record the
    decision (I18), then execute it. Returns the resulting fire_state.
    """
    try:
        ctx = _context(conn, timer, graph=graph)
    except Exception as exc:  # noqa: BLE001 — graph/store unreachable: no facts to decide on, so don't
        fid = timer.get("fire_id") or fire_id(timer["case_id"], timer["kind"], timer["cycle"], timer["due_at"])
        return _inconclusive(conn, timer, fid, timer.get("attempts", 0) + 1,
                             recipient_class="technician", reason="store_unreachable", last_error=str(exc))

    selected, proposed = bthreads.select_action(ctx)
    expected, why = prolog.timer_action(ctx)
    if expected == "engine_unavailable":
        timers.set_state(conn, timer["timer_id"], "FAILED", last_error=f"engine_unavailable:prolog ({why})")
        return "FAILED"
    if selected.lower() != expected:
        timers.set_state(conn, timer["timer_id"], "FAILED",
                         last_error=f"layer_disagreement: bppy={selected} prolog={expected}")
        return "FAILED"
    timers.record_decision(conn, timer["timer_id"],
                           selected if selected == proposed else f"{selected} (proposed {proposed}: {why})")

    if selected == "DISPATCH":
        return dispatch(conn, timer, graph=graph)
    if selected == "RECONCILE":
        return reconcile(conn, timer, graph=graph)
    if selected == "NOTIFY":
        return _send_reminder(conn, timer, ctx)
    if selected == "FAIL_BUDGET":
        timers.set_state(conn, timer["timer_id"], "FAILED", last_error="notification budget exhausted")
        return "FAILED"
    timers.set_state(conn, timer["timer_id"], "CANCELLED")
    return "CANCELLED"
```

In `dispatch`, insert the OPA gate between the idempotency check and the resume (after the `if current.get("control_state") != State.MONITORING.value:` block, before `resume = {...}`):

```python
    # The last line before the side effect: the check above reconciles what
    # already happened, this decides whether the resume may happen now (I16).
    gate = opa.evaluate({"action": "dispatch",
                         "case": {"control_state": current.get("control_state")},
                         "timer": {"fire_state": timer.get("fire_state") or "DUE"}})
    if not gate["allow"]:
        timers.set_state(conn, timer["timer_id"], "FAILED",
                         last_error="opa denied dispatch: " + "; ".join(gate["deny_reasons"]))
        return "FAILED"
```

Replace the whole body of `notify` with a delegation, and add `_send_reminder` after `reconcile`:

```python
def notify(conn, timer: dict[str, Any], *, graph) -> str:
    """Send a gate, re-filing or senior reminder (I15). Notify-only: no case
    state change, no ack, so the b-threads never select DISPATCHING/UNKNOWN
    for one. Kept as a named entry point because callers and tests reach for
    it by name; the decision itself is `handle`'s.
    """
    return handle(conn, timer, graph=graph)


def _send_reminder(conn, timer: dict[str, Any], ctx: dict[str, Any]) -> str:
    """The NOTIFY executor: OPA gate, then the notification record.
    CANCELLED / over-budget were already chosen upstream by the b-threads;
    this gate is the last line, immediately before the send.
    """
    gate = opa.evaluate({"action": "notify", "pause_active": ctx["pause_active"],
                         "notify_count": ctx["notify_count"], "notify_budget": ctx["notify_budget"]})
    if not gate["allow"]:
        timers.set_state(conn, timer["timer_id"], "FAILED",
                         last_error="opa denied notify: " + "; ".join(gate["deny_reasons"]))
        return "FAILED"

    reason = f"{timer['kind']}_{timer['cycle']}"
    timers.record_notification(conn, case_id=timer["case_id"], reason=reason, channel="notification_strip",
                                recipient_class=ctx["recipient_class"])
    timers.set_state(conn, timer["timer_id"], "DELIVERED")
    return "DELIVERED"
```

Update the module docstring's first paragraph to:

```
"""Fire state machine: deciding what a claimed timer gets (`handle`), then
dispatch, reconcile, or a reminder.

The imperative code here only proposes. Which event a timer gets is chosen by
the b-threads in `app.monitor.bthreads`, cross-checked against
`app.symbolic.prolog`, and every side effect passes the OPA gate in
`app.symbolic.opa` immediately before it happens. A missing or failing engine
is a refusal, never a fallback.

`dispatch`/`reconcile`/`notify` take `graph` so the sweeper passes the real
cached graph and tests pass an isolated one. Only this module writes `fire_state`.
"""
```

- [ ] **Step 6: Run the fire tests**

Run: `uv run pytest tests/monitor/test_fire.py tests/test_gates.py -q`
Expected: all pass — the existing notify tests (`…while_the_gate_is_still_open`, `…widens_to_any_charge_nurse…`, `…budget_is_spent`, `…senior_reminder_goes_to_a_shift_lead`) now run through `handle` and must still pass unchanged.

- [ ] **Step 7: Full suite**

Run: `uv run pytest -q`
Expected: 2 failed (CRM baseline), nothing else.

---

### Task 5: Sweeper routes every claimed timer through `fire.handle`

**Files:**
- Modify: `app/monitor/sweeper.py`
- Test: `tests/monitor/test_sweeper.py` (one addition)

**Interfaces:**
- Consumes: `fire.handle` (Task 4).
- Produces: `sweeper._handle(conn, timer, graph)` (private, replaces `_dispatch` / `_handle_due` / `_handle_retry`); `run_once` signature unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/monitor/test_sweeper.py`:

```python
def test_run_once_never_redispatches_an_unknown_timer_blind(conn, graph, run):
    """A retryable UNKNOWN row must go to reconcile, not dispatch — and the
    decision column has to show the b-threads made that call (I18)."""
    case = DEMO_CASES["clean"]
    state, pending, thread = run(case)
    fid = fire.fire_id(thread, "reassessment", 0, "2000-01-01T00:00:00Z")
    timer_id = timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at="2000-01-01T00:00:00Z")
    timers.set_state(conn, timer_id, "UNKNOWN", fire_id=fid, lease_until=None, worker_id=None)

    sweeper.run_once(conn, worker_id="w1", graph=graph)

    row = conn.execute("SELECT fire_state, decision FROM timers WHERE timer_id=%s", (timer_id,)).fetchone()
    assert row[0] == "FAILED"
    assert row[1].startswith("RECONCILE (proposed DISPATCH")
    assert hydrate(graph.get_state(config_for(thread)).values)["reassessment_cycle"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/monitor/test_sweeper.py -q -k blind`
Expected: FAIL — `decision` is `None` (the old `_handle_retry` bypasses `handle`).

- [ ] **Step 3: Replace the three dispatch helpers**

In `app/monitor/sweeper.py`, replace `_dispatch`, `_handle_due` and `_handle_retry` with:

```python
def _handle(conn: psycopg.Connection, timer: dict, graph) -> None:
    """Every claimed timer — newly due or left over — goes through the same
    decision in `fire.handle`; the sweeper no longer decides anything itself.
    `timer_gap` is computed here because it needs the clock and the acuity,
    which are the sweeper's business, not the rules'.
    """
    if timer["kind"] == "reassessment":
        timer = {**timer, "timer_gap": _is_timer_gap(graph, timer)}
    fire.handle(conn, timer, graph=graph)
```

and in `run_once` call `_handle(conn, timer, g)` in both loops.

Update the module docstring's second paragraph to:

```
Each tick: heartbeat, claim newly due timers, then leftovers from earlier ticks,
and hand every one to `fire.handle`, where the symbolic layers decide what it
gets. Then one Datalog pass over the whole store (I15/I16).
`graph=None` only in `main()`; tests always pass their own.
```

- [ ] **Step 4: Run the sweeper tests**

Run: `uv run pytest tests/monitor/test_sweeper.py -q`
Expected: all pass, including `test_run_once_redispatches_a_failed_timer_on_a_later_tick` (still `FAILED`, "case not found").

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: 2 failed (CRM baseline), nothing else.

---

### Task 6: The temporal monitor's deadline pass — Datalog `unwatched` / `orphan`

The spec's temporal monitor "reads each case's audit log in order and flags the exact record where any rule breaks, including the deadlines (I15–I17)". This task builds its timer-store arm: the two questions a per-row decision cannot see.

**Files:**
- Create: `app/symbolic/datalog.py`
- Modify: `app/monitor/timers.py` (`all_rows`, `escalation_exists`)
- Modify: `app/monitor/sweeper.py` (`_escalate_once`, `_check_invariants`, called at the end of `run_once`)
- Test: `tests/symbolic/test_datalog.py`, `tests/monitor/test_sweeper.py` (two additions)

**Interfaces:**
- Produces: `datalog.tick_invariants(timer_rows: list[dict], case_rows: list[dict]) -> dict[str, list]` = `{"unwatched": [case_id, …], "orphan": [(case_id, timer_id), …]}`, both sorted. `timer_rows` items: `{timer_id, case_id, kind, fire_state}`; `case_rows` items: `{case_id, control_state, clinical_status}` (`None`s for a case with no state).
- Produces: `timers.all_rows(conn) -> list[dict]`; `timers.escalation_exists(conn, *, case_id, reason) -> bool`.

- [ ] **Step 1: Write the failing tests**

`tests/symbolic/test_datalog.py`:

```python
"""The Datalog layer (`app.symbolic.datalog`): invariants over *all* timers and
cases at once — the deadline arm of the temporal monitor (I15/I16). Pure:
rows in, findings out.
"""

from __future__ import annotations

from app.symbolic import datalog


def _timer(case_id, fire_state, kind="reassessment", cycle=0):
    return {"timer_id": f"{case_id}:{kind}:{cycle}", "case_id": case_id, "kind": kind, "fire_state": fire_state}


def _case(case_id, control_state="monitoring", clinical_status="waiting"):
    return {"case_id": case_id, "control_state": control_state, "clinical_status": clinical_status}


def test_nothing_to_report_on_empty_input():
    assert datalog.tick_invariants([], []) == {"unwatched": [], "orphan": []}


def test_a_waiting_case_with_a_live_timer_is_watched():
    for state in ("SCHEDULED", "DUE", "DISPATCHING", "FAILED", "UNKNOWN"):
        assert datalog.tick_invariants([_timer("a", state)], [_case("a")])["unwatched"] == []


def test_a_waiting_case_whose_timers_are_all_finished_is_unwatched():
    """I16: a queued patient with no live reassessment timer has no deadline."""
    for state in ("DELIVERED", "CANCELLED", "ESCALATED_TO_HUMAN"):
        assert datalog.tick_invariants([_timer("a", state)], [_case("a")])["unwatched"] == ["a"]


def test_only_the_waiting_room_needs_a_watcher():
    rows = [_timer("a", "DELIVERED")]
    assert datalog.tick_invariants(rows, [_case("a", clinical_status="treatment_started")])["unwatched"] == []
    assert datalog.tick_invariants(rows, [_case("a", control_state="reassessment_required",
                                                 clinical_status="reassessment_required")])["unwatched"] == []


def test_a_reminder_does_not_count_as_watching():
    rows = [_timer("a", "SCHEDULED", kind="gate_reminder")]
    assert datalog.tick_invariants(rows, [_case("a")])["unwatched"] == ["a"]


def test_a_live_timer_for_a_missing_or_closed_case_is_an_orphan():
    rows = [_timer("ghost", "FAILED"), _timer("closed", "SCHEDULED"), _timer("ok", "SCHEDULED")]
    cases = [_case("ghost", control_state=None, clinical_status=None),
             _case("closed", control_state="case_closed", clinical_status="patient_released"),
             _case("ok")]
    assert datalog.tick_invariants(rows, cases)["orphan"] == [("closed", "closed:reassessment:0"),
                                                             ("ghost", "ghost:reassessment:0")]


def test_a_finished_timer_for_a_missing_case_is_not_an_orphan():
    assert datalog.tick_invariants([_timer("ghost", "CANCELLED")],
                                   [_case("ghost", control_state=None, clinical_status=None)])["orphan"] == []
```

Append to `tests/monitor/test_sweeper.py`:

```python
def test_an_orphan_timer_is_escalated_to_a_technician_once(conn, graph):
    timer_id = timers.schedule(conn, case_id="no-such-case", kind="reassessment", cycle=0,
                                due_at="2000-01-01T00:00:00Z")

    sweeper.run_once(conn, worker_id="w1", graph=graph)
    sweeper.run_once(conn, worker_id="w1", graph=graph)

    # The timer itself is left alone (still FAILED, still retryable): this
    # layer reports, it never mutates.
    assert conn.execute("SELECT fire_state FROM timers WHERE timer_id=%s", (timer_id,)).fetchone() == ("FAILED",)
    rows = conn.execute("SELECT recipient_class, reason FROM escalations WHERE case_id='no-such-case'").fetchall()
    assert rows == [("technician", "orphan_timer")]


def test_a_waiting_case_nobody_is_watching_is_escalated(conn, graph, run):
    """I16: the queue's deadline disappeared, so somebody has to hear about it."""
    case = DEMO_CASES["clean"]
    state, pending, thread = run(case)
    timer_id = timers.schedule(conn, case_id=thread, kind="reassessment", cycle=0, due_at="2999-01-01T00:00:00Z")
    sweeper.run_once(conn, worker_id="w1", graph=graph)
    assert conn.execute("SELECT COUNT(*) FROM escalations WHERE case_id=%s", (thread,)).fetchone() == (0,)

    timers.set_state(conn, timer_id, "CANCELLED")  # the watcher was pulled out from under a waiting patient
    sweeper.run_once(conn, worker_id="w1", graph=graph)

    rows = conn.execute("SELECT recipient_class, reason FROM escalations WHERE case_id=%s", (thread,)).fetchall()
    assert rows == [("technician", "unwatched_case")]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/symbolic/test_datalog.py tests/monitor/test_sweeper.py -q -k "datalog or orphan or nobody"`
Expected: FAIL — `ImportError: cannot import name 'datalog'`; the sweeper tests find no escalation rows.

- [ ] **Step 3: Write the Datalog module**

`app/symbolic/datalog.py`:

```python
"""Datalog invariants over the whole timer store at once (pyDatalog).

Prolog answers "what about *this* timer"; this answers "across *all* timers
and cases right now, is a deadline missing?" — the temporal monitor's I15/I16
arm. Findings are reported (escalated), never acted on.
"""

from __future__ import annotations

from typing import Any

from pyDatalog import pyDatalog

# A timer that is still going to do something. Everything else is history.
LIVE_STATES = {"SCHEDULED", "DUE", "DISPATCHING", "FAILED", "UNKNOWN"}

RULES = """
unwatched(C) <= waiting(C) & ~live_timer(C, 'reassessment', T)
orphan(C, T) <= live_timer(C, K, T) & ~active_case(C)
"""


def _answers(query: str) -> list[tuple]:
    result = pyDatalog.ask(query)
    return [tuple(row) for row in result.answers] if result else []


def tick_invariants(timer_rows: list[dict[str, Any]], case_rows: list[dict[str, Any]]) -> dict[str, list]:
    """`{"unwatched": [case_id], "orphan": [(case_id, timer_id)]}`, sorted.

    unwatched: a case in the waiting room with no live reassessment timer —
               the I16 deadline silently gone.
    orphan:    a live timer for a case that has no state or is closed — work
               the sweeper would retry forever with nobody to tell.

    Facts are rebuilt on every call: pyDatalog's knowledge base is
    process-global, and a tick must never see a previous tick's rows.
    """
    pyDatalog.clear()
    pyDatalog.load(RULES)
    for case in case_rows:
        if case["control_state"] not in (None, "case_closed"):
            pyDatalog.assert_fact("active_case", case["case_id"])
        if case["control_state"] == "monitoring" and case["clinical_status"] == "waiting":
            pyDatalog.assert_fact("waiting", case["case_id"])
    for timer in timer_rows:
        if timer["fire_state"] in LIVE_STATES:
            pyDatalog.assert_fact("live_timer", timer["case_id"], timer["kind"], timer["timer_id"])
    return {
        "unwatched": sorted(row[0] for row in _answers("unwatched(C)")),
        "orphan": sorted(_answers("orphan(C, T)")),
    }
```

- [ ] **Step 4: Add the two store helpers to `timers.py`**

After `record_escalation`:

```python
def escalation_exists(conn: psycopg.Connection, *, case_id: str, reason: str) -> bool:
    """Whether this case already has an escalation for this reason — so an
    invariant found on every tick is raised once, not every five seconds.
    """
    return conn.execute(
        "SELECT 1 FROM escalations WHERE case_id = %s AND reason = %s LIMIT 1",
        (case_id, reason),
    ).fetchone() is not None


def all_rows(conn: psycopg.Connection) -> list[dict]:
    """Every timer, live or finished — the temporal-monitor pass reasons over
    the whole store, not just what is claimable.
    """
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT timer_id, case_id, kind, fire_state FROM timers"
    ).fetchall()
```

- [ ] **Step 5: Run the pass at the end of every tick**

In `app/monitor/sweeper.py`, add `from app.symbolic import datalog` to the imports, and after `_handle`:

```python
def _escalate_once(conn: psycopg.Connection, *, case_id: str, reason: str) -> None:
    if timers.escalation_exists(conn, case_id=case_id, reason=reason):
        return
    timers.record_escalation(conn, case_id=case_id, fire_id=f"invariant:{reason}", channel="notification_strip",
                              recipient_class="technician", reason=reason)


def _check_invariants(conn: psycopg.Connection, graph) -> None:
    """The temporal monitor's deadline pass (I15/I16), after the tick's own
    work so it sees this tick's outcomes: a waiting case nobody is watching,
    or a live timer for a case that is gone. Findings go to a technician, once
    per (case, reason). It never touches timer state — a timer the sweeper
    keeps refusing is evidence, not a mess to tidy away.
    """
    timer_rows = timers.all_rows(conn)
    case_rows = []
    # ponytail: one graph.get_state per distinct case per tick. Fine for a
    # ward's worth of cases; cache by checkpoint id if it ever isn't.
    try:
        for case_id in sorted({row["case_id"] for row in timer_rows}):
            values = graph.get_state(config_for(case_id)).values
            case_rows.append({"case_id": case_id, "control_state": values.get("control_state"),
                              "clinical_status": values.get("clinical_status")})
    except Exception:  # noqa: BLE001 — graph store unreachable: no facts, no findings this tick; the per-timer path already escalates store_unreachable
        return
    findings = datalog.tick_invariants(timer_rows, case_rows)
    for case_id in findings["unwatched"]:
        _escalate_once(conn, case_id=case_id, reason="unwatched_case")
    for case_id, _timer_id in findings["orphan"]:
        _escalate_once(conn, case_id=case_id, reason="orphan_timer")
```

In `run_once`, before `return claimed`:

```python
    _check_invariants(conn, g)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/symbolic/test_datalog.py tests/monitor/test_sweeper.py -q`
Expected: all pass.

- [ ] **Step 7: Full suite**

Run: `uv run pytest -q`
Expected: 2 failed (CRM baseline), nothing else.

---

### Task 7: Docs — `app/monitor/README.md`

**Files:**
- Modify: `app/monitor/README.md` (file list, mermaid, new section)

- [ ] **Step 1: Update the file list**

Replace the code block under "## The three files" (and its heading, which is now wrong) with:

```markdown
## The files

```
app/monitor/timers.py    the table + reads/writes; claims rows without two processes taking the same one
app/monitor/fire.py      "a timer is due - now what?": gathers the facts, executes the decision
app/monitor/bthreads.py  the decision itself: BPpy b-threads pick the one allowed event
app/monitor/sweeper.py   the loop: every 5s ask timers.py what's due, hand it to fire.py (`uv run sweeper`)
app/symbolic/            the engines fire.py asks: Prolog rules, OPA policy, Datalog rules
```
```

- [ ] **Step 2: Update the mermaid flowchart**

Replace the `flowchart LR` block with:

```mermaid
flowchart LR
  TS["timers.py<br/>schema · schedule · claim/lease · heartbeat · decision"]
  FI["fire.py<br/>handle · dispatch · reconcile · reminders"]
  BT["bthreads.py<br/>BPpy: pick the one allowed event"]
  SY["app/symbolic<br/>Prolog · OPA · Datalog"]
  SW["sweeper.py<br/>the loop (uv run sweeper)"]
  GR["graph nodes<br/>monitoring · awaiting_human_approval · reassessment_required"]

  GR -- "timers.schedule(...)" --> TS
  SW -- "claim_due / claim_retryable" --> TS
  SW -- "fire.handle(row)" --> FI
  FI -- "select_action(ctx)" --> BT
  FI -- "timer_action(ctx) must agree · opa gate before side effect" --> SY
  SW -- "tick_invariants → escalations" --> SY
  FI -- "timers.set_state / record_decision" --> TS
  FI -- "graph.invoke(Command(resume=...))<br/>wakes the paused case" --> GR
```

- [ ] **Step 3: Add the section**

Insert after the mermaid block, before "## Key idea: the case is paused, not finished":

```markdown
## The symbolic layers: who decides what

`fire.py` decides nothing on its own. For every claimed timer it builds one
bag of facts (`_context`: kind, fire state, is the case still at the pause,
how many reminders went out this hour) and asks:

| Layer | Question | Where | If it can't answer |
| --- | --- | --- | --- |
| **BPpy** `bthreads.py` | Which event is allowed *now*? A `proposer` asks for the naive thing; each rule is a b-thread that blocks it and requests the safe one (`UNKNOWN` → `RECONCILE`, pause resolved → `CANCEL`, budget spent → `FAIL_BUDGET`). | `fire.handle`, before anything runs | n/a — pure Python |
| **Prolog** `app/symbolic/rules/monitor.pl` | Same question, derived independently, plus *why* anything is refused (`denial/3`). Also I14: may this role resolve this gate? | `fire.handle` (must agree with BPpy or the timer is `FAILED` with `layer_disagreement`); the human gate | timer `FAILED`, `engine_unavailable:prolog` |
| **OPA** `app/symbolic/policy/monitor.rego` | May this exact side effect happen right now? (I5, I9) | immediately before `graph.invoke` (dispatch), before the notification record (notify), and for move/release | action denied, `engine_unavailable:opa` |
| **Datalog** `app/symbolic/datalog.py` | Across *all* timers and cases this tick: is a waiting patient unwatched (I16)? Is a live timer pointing at a case that's gone? | end of every sweeper tick | pass skipped, sweeper keeps ticking |

What each decision left behind is on the row: `timers.decision` holds the
selected event and, when a rule overrode the proposal, which one and why
(`RECONCILE (proposed DISPATCH: dispatch: blind_redispatch_from_unknown)`);
`timers.last_error` holds any refusal. Datalog findings land in `escalations`
(`unwatched_case`, `orphan_timer`), once per case, addressed to a technician;
that layer never changes a timer.

Two of these are real engines outside Python: SWI-Prolog (`swipl`) and the
`opa` binary. Without them every guarded action is refused — the sweeper keeps
ticking and the rows say `engine_unavailable`. Install notes are in the
top-level README.
```

- [ ] **Step 4: Update the reminder section**

In "## Notify-only reminders", replace the first paragraph with:

```markdown
A second kind of timer, three flavours. At the human-approval gate, two
`gate_reminder` timers are scheduled at once: ping the assigned nurse after
10 min, widen to any charge nurse after 20 (`_GATE_RUNG_RECIPIENTS`, indexed
by `cycle % 2`, because each gate visit gets its own cycle numbers). A case
handed up to a senior schedules one `senior_reminder` to any shift lead; the
re-filing pause schedules one `reassessment_reminder` to any charge nurse.
All three are I15: the system keeps escalating, widening who is alerted.
```

- [ ] **Step 5: Final full suite + a real sweeper tick**

Run: `uv run pytest -q`
Expected: 2 failed (CRM baseline), nothing else.

Run (with Postgres up): `timeout 12 uv run sweeper; echo "exit $?"`
Expected: no traceback; exits 124 (killed by timeout, i.e. it was looping). Then `psql postgresql://triage:triage@localhost:5434/triage -c "SELECT worker_id FROM sweeper_heartbeats"` shows a row.
