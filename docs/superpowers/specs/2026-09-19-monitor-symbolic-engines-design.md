# Monitor symbolic engines — design

Replace the `SKELETON — swap for real engines` Python stubs that govern the
waiting-room monitor with the real engines the spec names: Prolog, Datalog,
BPpy, OPA. Scope is the monitor and the guards it exercises; the rest of the
graph keeps its skeleton.

Principle (from the governed-refund-agent project): the imperative code
*proposes* an action; the symbolic layers decide whether it happens, and every
refusal carries an explanation.

Revised 2026-09-20 against HEAD `c8a6d43` and the finalized I1–I18 invariant
table in `docs/SPECIFICATION.md` § Safety invariants.

## Roles

| Engine | Question it answers | Backs | Where it runs | Writes |
| --- | --- | --- | --- | --- |
| **Prolog** (`app/symbolic/prolog.py` + `rules/monitor.pl`, pyswip) | "What should happen to *this* timer, and why is anything refused?" / "May this role resolve this gate?" | **I14** | `fire.handle` (cross-check), `deterministic.actor_is_charge`, `gate` (incl. the `senior_required` → `shift_lead` rule) | action + explanation |
| **Datalog** (`app/symbolic/datalog.py`, pyDatalog) | "Across *all* timers and cases this tick, has a deadline gone missing?" — the temporal monitor's I15–I16 arm, *not* the spec's provenance/information-flow use | **I15, I16** | end of every `sweeper.run_once` tick | `escalations` rows (deduped), never timer state |
| **BPpy** (`app/monitor/bthreads.py`) | "Which event is allowed *now* for this timer?" | I15, I16 | `fire.handle`, before any side effect | selected event (exactly one) |
| **OPA** (`app/symbolic/opa.py` + `policy/monitor.rego`, `opa eval` subprocess) | "Is this concrete side effect permitted right now?" | **I5, I9** | `fire.dispatch` before `graph.invoke`; `fire` before `record_notification`; `deterministic.move_authorized` / `release_authorized` (reached from every pause via `_shared.release_case`) | allow + deny_reasons |

Every refusal is recorded with the layer that made it (`audit_denial(layer=…)`,
`timers.decision`, `timers.last_error`) — that is **I18**, which asks for every
change *and every refused attempt* to carry a reason.

Prolog and BPpy both derive the per-timer action from the same facts. That is
deliberate double enforcement: if they disagree the timer is marked `FAILED`
with `last_error = "layer_disagreement: ..."` and nothing executes.

## Data flow per claimed timer

```
sweeper.run_once
  └─ fire.handle(conn, timer, graph)
       ├─ _context(): timer row + graph snapshot + notification count → ctx dict
       ├─ bthreads.select_action(ctx)  → (selected, proposed)      BPpy
       ├─ prolog.timer_action(ctx)     → (expected, why)           Prolog
       ├─ disagree? → FAILED "layer_disagreement", stop
       ├─ timers.record_decision(...)  (new `decision` column: selected, proposed, why)
       └─ execute selected:
            DISPATCH    → fire.dispatch  (OPA gate → graph.invoke)
            RECONCILE   → fire.reconcile (unchanged)
            NOTIFY      → OPA gate → timers.record_notification → DELIVERED
            CANCEL      → CANCELLED
            FAIL_BUDGET → FAILED "notification budget exhausted"
  └─ datalog.tick_invariants(timer_rows, case_rows)
       unwatched(C): case at monitoring/waiting with no live reassessment timer → escalation "unwatched_case" (technician)
       orphan(T):    live timer whose case has no state or is closed           → escalation "orphan_timer"  (technician)
       (deduped by (case_id, reason) via timers.escalation_exists; timers are never mutated by this layer)
```

Three reminder kinds go through the NOTIFY path: `gate_reminder` (rung by
`cycle % 2`: assigned nurse, then any charge nurse), `reassessment_reminder`
(any charge nurse) and `senior_reminder` (any shift lead) — I15's widening.

BPpy b-threads: `proposer` (DISPATCH for reassessment, NOTIFY for reminders,
CANCEL for unknown kinds); `no_blind_redispatch` (state UNKNOWN/DISPATCHING →
block DISPATCH, request RECONCILE); `stale_reminder` (pause resolved → block
NOTIFY, request CANCEL, priority 2); `notify_budget` (count ≥ budget → block
NOTIFY, request FAIL_BUDGET, priority 1); `one_decision` (after the first
event, block everything). Strategy: `PriorityBasedEventSelectionStrategy`, so
the run is deterministic and yields exactly one event.

## Fail-closed

Any engine failure (binary missing, timeout, parse error, Prolog exception)
is a *deny* with reason `engine_unavailable:<engine>`, recorded in
`last_error`; the timer goes `FAILED` (retryable) or, if the graph itself was
unreachable, follows the existing `store_unreachable` → `UNKNOWN` →
`ESCALATED_TO_HUMAN` path. The sweeper never crashes and never falls back to
a Python guard silently.

## Prerequisite: HEAD is red

`c8a6d43` deleted `fire._GATE_RUNG_RECIPIENTS` but kept its use at
`fire.py:130`, so every gate reminder raises `NameError` (4 failing tests).
The plan's Task 0b restores it, indexed `cycle % len(...)` because gate cycles
are now `visit + rung`. Two further failures (CRM degrade) are unrelated to
the monitor and stay the user's; the plan's green bar is those two.

## Public surface kept

`fire.fire_id`, `fire.dispatch`, `fire.reconcile`, `fire.notify`,
`fire.RECONCILE_BUDGET`, `fire.NOTIFICATION_BUDGET_PER_WINDOW`,
`sweeper.run_once(conn, *, worker_id, graph)`, and the three
`deterministic` guard signatures are unchanged — every existing test in
`tests/monitor/`, `tests/test_denial.py`, `tests/test_gates.py` and
`tests/test_release_everywhere.py` stays green. New:
`fire.handle`, `fire._recipient_class`, `timers.record_decision`,
`timers.escalation_exists`, `timers.all_rows`, `timers.decision` column,
`audit_denial(..., layer=...)`, `prolog.may_resolve_gate`.

## Runtime requirements

- SWI-Prolog (`swipl`, apt) — pyswip links to it.
- `opa` static binary on `PATH` or at `$OPA_BIN`.
- Python: `pyswip`, `pyDatalog`, `bppy` (added to `triage-app/pyproject.toml`).

## Out of scope

`verify_no_identifiers` (I11 redaction, not monitor), the spec's other Datalog
use (I3 acuity provenance), Z3/Alloy/SPIN design-time checks (I4, I13), the
audit-log-replay half of the temporal monitor, the `awaiting_reassessment`
pause-event arbitration, the CRM degrade bug, and an OPA server sidecar
(subprocess per call measured at ~12 ms; fine for the sweeper).
