# Findings — monitor symbolic engines

Investigation record: what was checked, what was ruled out, why. Source
material: `מטלות/verification/governed_refund_agent/` (the course's
reference pipeline) and `triage-app/app/monitor/`.

The first pass was written 2026-09-19 against `a3363f3`. **Re-reviewed
2026-09-20 against `c8a6d43`** — 11 commits later, with the monitor rewritten
and `docs/SPECIFICATION.md` carrying a finalized I1–I18 invariant table. That
review is the last section; everything above it still holds unless that
section says otherwise.

## Where the engines belong

- **`app/deterministic.py`** already carries the heading
  `# ---- symbolic-layer predicates (SKELETON — swap for real engines)`, and
  `docs/SPECIFICATION.md` assigns a layer to every transition (`Prolog
  (authorization)` for the gate, `OPA (authorization)` for move/release).
  The app was designed for this swap; the monitor is a *consumer* of those
  guards, not their home.
- **Ruled out: `app/monitor/policy/`** (the user's first suggestion).
  `actor_is_charge` is also used by `app/graph/nodes/gate.py`; a graph node
  importing from `app.monitor.policy` inverts the dependency direction
  (monitor → graph today). Hence `app/symbolic/` at package level, with only
  the BPpy b-threads inside `app/monitor/` (they *are* monitor logic).
- `audit_denial` hard-codes `denying_layer="Prolog (authorization)"` even for
  move/release, which the spec says are OPA's. Fixed with a `layer` kwarg.

## Public surface that must not move (tests depend on it)

Grepped `tests/monitor/*.py`, `tests/test_denial.py`,
`tests/test_treatment_move_and_release.py`:

- `fire.dispatch/reconcile/notify(conn, timer, *, graph)`, `fire.fire_id`,
  `fire.RECONCILE_BUDGET`, and **`monkeypatch.setattr(fire,
  "NOTIFICATION_BUDGET_PER_WINDOW", 1)`** — so the budget must be read from
  `fire`'s module globals at call time (it is: `_context` builds
  `notify_budget` from the module-level name).
- `sweeper.run_once(conn, *, worker_id, graph=None)`.
- `test_denial.py` asserts substrings only (`"nurse" in why`, `"safety" in
  why`, `"approved" in why`, `"bogus" in why`) — the Rego deny strings were
  written to contain them.
- `test_run_once_redispatches_a_failed_timer_on_a_later_tick` expects a
  FAILED timer for a nonexistent case to **stay FAILED** ("refused again, not
  silently dropped"). This ruled out the first Datalog design (orphan →
  CANCELLED). Datalog now only *reports* (escalation to technician, once);
  it never mutates a timer.
- `claim_due` rows carry no `fire_state`; `claim_retryable` rows do. `_context`
  defaults to `"DUE"`.

## Engine behaviour verified in a scratch venv (bppy 1.0.4, pyswip 0.3.3, pyDatalog 0.22.4, opa 1.20.2)

- **BPpy `SimpleEventSelectionStrategy` is `random.choice`** among enabled
  events. With two guards active (stale + over budget) that produced
  `['CANCEL','FAIL_BUDGET']` in random order. Fix: `PriorityBasedEventSelectionStrategy`
  with `priority=` on the guard `sync`s (CANCEL 2 > FAIL_BUDGET/RECONCILE 1 >
  proposer 0).
- A guard that requests-and-blocks once then ends releases the block, so the
  proposer's event was selected as a *second* event (`['RECONCILE',
  'DISPATCH']`). Fix: guards keep blocking forever (`while True: yield
  bp.sync(block=…)`) — the refund project's `policy_guard` idiom — plus a
  `one_decision` thread that `waitFor=bp.All()` then blocks `bp.All()`.
  Result: exactly one event, deterministic over 20×8 runs.
- `bp.BProgramRunnerListener` is abstract on all ten hooks; the no-op
  overrides are mandatory.
- **pyswip** `Prolog` methods are classmethods on one process-global engine;
  `consult` works with a space in the path (`AI Architect course`);
  generators must be exhausted (`list(...)`). Quoted atoms `'…'` with `\'`
  escaping neutralise a role like `o'brien` or `x). charge_role(y`.
- **pyDatalog** `create_terms` injects names into the *caller's* namespace,
  which does not work inside a function on 3.12. The string API
  (`pyDatalog.load(rules)` + `pyDatalog.ask("q(C)")`) does; `ask` returns
  `None` for no answers. Negation with an unbound variable
  (`~live_timer(C, 'reassessment', T)`) evaluates correctly.
- **OPA** `opa eval -d policy -I --format=raw 'data.triage.monitor.decision'`
  reads input from stdin and prints the bare JSON value; ~12 ms per call
  (`time`), so a subprocess per gate is fine for a 5-second sweeper tick. A
  server sidecar is deferred (ponytail note in `opa.py`). `opa` was **not**
  installed on this machine; Task 0 fetches the static binary to
  `~/.local/bin`.
- `swipl` 9.0.4 is installed system-wide; `pyswip` links to it.

## Role decisions and what was ruled out

- **Prolog and BPpy both compute the per-timer action.** Kept on purpose:
  the refund project's principle that a critical rule is enforced by more
  than one layer. `fire.handle` refuses (`FAILED`, `layer_disagreement`) if
  they differ. Prolog additionally supplies the *explanation* (`denial/3`)
  that the BLK row wants and BPpy has no vocabulary for.
- **Reconcile budget stays imperative** (`_inconclusive`, one comparison,
  evaluated *after* the reconcile attempt). Modelling it as a b-thread would
  pre-empt the last reconcile attempt — a landed fire found on the final try
  would be escalated instead of DELIVERED. Not worth the regression.
- **Datalog `duplicate(C,K)` rule dropped.** A lingering FAILED cycle-0 row
  next to a SCHEDULED cycle-1 row is a normal transient (redispatch resolves
  it as DELIVERED), so the rule would false-positive.
- **Datalog case facts come from the timers table only**, not from
  `SELECT DISTINCT thread_id FROM checkpoints`: in tests the timer store and
  the checkpointer are separate databases (`conftest.conn` vs `graph`), and
  every case that reached `monitoring` has a timer row anyway.
- In tests, graph nodes schedule timers under `state.case_id` (e.g.
  `C-1001`) while tests key their rows by `thread`; the Datalog pass will
  therefore log an `orphan_timer` escalation for `C-1001` in a few existing
  tests. Harmless (no test asserts on it), and in production
  `thread_id == case_id`. Noted so nobody "fixes" it.
- `verify_no_identifiers` keeps its Python skeleton — redaction is outside
  the monitor. Z3/Alloy/SPIN (design-time) and the `awaiting_reassessment`
  pause-event arbitration are out of scope per the brainstorm.

## Fail-closed paths and where each lands

| Failure | Where caught | Outcome |
| --- | --- | --- |
| `opa` missing / timeout / bad output | `opa.evaluate` | `allow=False`, reason `engine_unavailable:opa (...)`; dispatch/notify → `FAILED` with `last_error="opa denied …"`; move/release → refused with that reason |
| Prolog exception during `timer_action` | `prolog.timer_action` | returns `("engine_unavailable", …)` → `fire.handle` sets `FAILED`, `last_error="engine_unavailable:prolog (...)"` |
| pyswip import fails (no `swipl`) | process start | app/sweeper won't start — by construction, not silently |
| graph store unreachable while building facts | `fire.handle` | existing `store_unreachable` path → `UNKNOWN`, then `ESCALATED_TO_HUMAN` after `RECONCILE_BUDGET` |
| graph store unreachable during Datalog pass | `sweeper._check_invariants` | pass skipped this tick; sweeper keeps ticking |
| BPpy and Prolog disagree | `fire.handle` | `FAILED`, `last_error="layer_disagreement: …"`, nothing executed |

---

## Re-review 2026-09-20 (HEAD `c8a6d43`)

Read in full: `app/monitor/{fire,timers,sweeper,__init__}.py`, `schema.sql`,
`app/monitor/README.md`, `app/graph/nodes/{terminal,gate,_shared}.py`,
`app/deterministic.py`, `app/budgets.py`, `docs/SPECIFICATION.md`
§ Safety invariants + § Symbolic governance layer, `tests/monitor/*`,
`tests/test_{gates,denial,release_everywhere}.py`. Ran `uv run pytest -q`.

### 1. HEAD is red — two unrelated bugs

`uv run pytest -q` → **6 failed**.

- **`_GATE_RUNG_RECIPIENTS` (4 failures).** `c8a6d43` deleted the constant
  from `fire.py` but kept the reference at `fire.py:130`, so every
  `gate_reminder` raises `NameError: name '_GATE_RUNG_RECIPIENTS' is not
  defined`. It is the monitor's own bug, one line, and nothing can be layered
  on a module that raises — hence **Task 0b**. The restored constant must be
  indexed `cycle % len(...)`, not `.get(cycle)`: gate cycles are now
  `visit + rung` (`gate.py:41`), so rung lookup by raw cycle would silently
  fall through to the default from the second gate visit onwards.
- **CRM degrade (2 failures).** `test_a_crm_outage_degrades_and_continues`
  sees `state["degraded"] == []`, and `test_clean_case_walks_the_documented_arrows_in_order`
  cannot find `AF·db`. Nothing to do with the monitor or this plan — left for
  the user, and recorded in `progress.md` so no task pretends to fix it.
  The plan's green bar is therefore `2 failed`, not zero.

### 2. Spec now names the invariants — the plan maps onto them

`docs/SPECIFICATION.md` § Safety invariants (I1–I18, "Reviewed and finalized
2026-09-19") gives every property a **Checked by** column. The monitor's share
is I14 (Prolog, with explanation), I5/I9 (OPA), I15/I16 (timers + sweeper +
temporal monitor), I18 (audit of every change *and every refused attempt*).
The plan now opens with that mapping so each task says which property it buys.

Two consequences:

- **The Datalog pass is the temporal monitor's arm, not "the Datalog layer"
  the spec describes.** The spec assigns Datalog to provenance (I3) and
  information flow (I11) — neither is in monitor scope. The `unwatched` /
  `orphan` queries are the deadline half of "the temporal monitor reads each
  case's audit log in order and flags the exact record where any rule breaks,
  including the deadlines (I15–I17)". Task 6 is renamed and documented as
  that, so nobody later reads it as the spec's Datalog paragraph.
- **I15 says the guarantee is escalation, not service** ("under overload the
  guarantee is escalation, not service"). That is exactly what
  `_escalate_once` does and why the pass never mutates a timer.

### 3. New authorization rule the first draft missed

`gate.awaiting_human_approval` now branches on `state.senior_required`: once
the correction loop hands a case up (I8), a `charge_nurse` is no longer
enough — only `shift_lead` may answer, with its own refusal string. That is a
role rule, so it belongs in Prolog (I14), not in an `if` in the node.
Added `may_resolve_gate/2` to `rules/monitor.pl` and
`prolog.may_resolve_gate(role, *, senior_required)`; Task 2 Step 6 moves the
node onto it. The refusal wording is copied verbatim from `gate.py:66` so the
existing gate tests keep passing.

### 4. Code drift that invalidated the first draft's code blocks

| Was (2026-09-19) | Is now | Plan change |
| --- | --- | --- |
| `due_at`/`lease_until` TEXT ISO strings, `_now_expr()` helper | `TIMESTAMPTZ`, plain `now()`, `due_in` returns `datetime` | `record_decision` uses `now()`; no `_now_expr` |
| `SCHEMA` string constant in `timers.py` | `app/monitor/schema.sql`, read by `init_schema` | the `decision` column goes in the .sql file |
| `_rows(cur, *columns)` hand-zipped dicts | `conn.cursor(row_factory=dict_row)` | `all_rows` uses `dict_row` |
| two reminder kinds | three: `gate_reminder`, `reassessment_reminder`, **`senior_reminder`** (→ `any_shift_lead`) | `REMINDER_KINDS` and `reminder_kind/1` carry all three; `_recipient_class` extracted from `notify` |
| gate cycles 0/1 | `visit + rung`, recipient by `cycle % len(...)` | Task 0b constant + `_recipient_class` |
| `action_denied` node; release handled inline in `terminal.py` | node gone; release is `_shared.release_case`, reachable from **every** pause (I9) | `layer=` kwarg applied in `_shared.release_case` too, not just `terminal.denied` |
| `dispatch` passed `fire_id=` on the final DELIVERED write | it does not (the id was already written at DISPATCHING) | plan's `dispatch` keeps HEAD's version; only the OPA gate is inserted |
| `sweeper` imported the real graph inside `run_once` | imported at module top | `_handle` replaces the three helpers; import left alone |

### 5. Re-checked against the new tests

- `tests/test_gates.py::test_the_senior_reminder_goes_to_a_shift_lead` calls
  `fire.notify` directly with a `senior_reminder` row and expects
  `any_shift_lead` — so `_context`/`_recipient_class` must handle that kind, and
  Task 4's rewrite of `notify` into a `handle` delegation must not change it.
- `monkeypatch.setattr(fire, "NOTIFICATION_BUDGET_PER_WINDOW", 1)` is still
  used, so the budget must stay a module global read at call time. `_context`
  reads it by name — correct.
- `test_run_once_redispatches_a_failed_timer_on_a_later_tick` still requires a
  FAILED timer for a nonexistent case to **stay FAILED**. The first draft's
  conclusion holds: the Datalog pass reports, never cancels.
- Tests still pass ISO strings for `due_at` (Postgres casts them on insert),
  so the plan's new tests do the same. But `claim_due` returns a real
  `datetime`, and `fire_id` stringifies whatever it is given — which is why
  `fire_id` already takes `datetime | str` and why no test compares an id
  built from a claimed row against one built from a literal.

### 6. Files read on the second sweep (2026-09-20, after "did you go over all code files?")

The first sweep covered the monitor and the guards it calls. The second covered
every remaining file in `git diff a3363f3..HEAD` plus all of
`docs/SPECIFICATION.md` (745 lines, not only the invariants table). What that
added:

**`routers.route_gate` duplicates the I14 rule.** It carries its own
`allowed = {"shift_lead"} if state.senior_required else {"charge_nurse", "shift_lead"}`,
the same rule as the gate node's. Moving only the node onto Prolog would leave
the rule in an engine *and* in a router set literal — the Policy Hell the whole
exercise exists to avoid. Task 2 Step 6 now changes both, with a router test.

**`release_route` routes off the last audit row's arrow.** `Arrow.RELEASE` →
RELEASED, `Arrow.BLK` → DENIED. So `audit_denial`'s new `layer=` kwarg must not
touch the arrow — it doesn't, and `tests/test_denial.py` now asserts that
explicitly, because a change there would silently break routing out of every
pause, not just the audit text.

**`build.py`: `ACTION_DENIED` is deliberately not a node.** A refusal is a BLK
row written by the node that refused, which then re-pauses. `Route.DENIED` at
the gate now loops back to `AWAITING_HUMAN_APPROVAL` so the reminders stay
armed. Confirms the plan's stance: refusals are recorded, never routed to a
terminal.

**`assign_order_key` changed shape** — `(acuity, arrival_time)`, and it now
*raises* `ValueError` on a missing arrival time (I2). Nothing in this plan calls
it, but `gate.py` does, so a Task 2 edit that touches the gate must not move it
above the arrival-time check.

**`reassessment.awaiting_reassessment_submission` resets `senior_required`** on
a re-file (a new triage starts clean) and can now be answered with a release.
Both are consistent with the plan; no change needed.

**`intake-channel` now requires `resolver_role`** ("a missing role must not
default to charge nurse (I14)") and carries `corrections` for I7. The role that
reaches Prolog is therefore always explicit — no default to strip.

**Spec gaps found, for the user, not tasks:**

- § Actors still lists the Waiting Room Monitor Agent's Tech as *(to confirm)*.
  This plan is that answer.
- § Context / State variables lists only `reassessment_timer` and `gate_timer`.
  The code has a third kind, `senior_reminder`, and `reassessment_reminder` is
  not listed either. The variable table is behind the code.
- § Guards defines `actor_authorized` as role ∧ jurisdiction ∧ data-class, and
  I14 says "according to the server's staff records". No store holds shift,
  ward or clearance facts, so `rules/monitor.pl` models role only and says so in
  a header comment rather than pretending the other two conjuncts exist.
- The resolved safety-fail branch says a correction must change one of `acuity`,
  `clinical_status` or `safety_verdict`; `gate.CORRECTABLE_FIELDS` is `{"acuity"}`
  with a comment acknowledging the narrowing. Out of scope here (I7), noted so
  it is not mistaken for something this plan broke.
- § Transitions keeps arrows `20a` / `20b` for the two gate reminder rungs. The
  code expresses those as timer kinds and `_GATE_RUNG_RECIPIENTS`, not as new
  arrow codes — which matches the standing instruction not to add numeric arrow
  codes for new events. No task adds one.
