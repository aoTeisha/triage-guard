# Progress — monitor symbolic engines

Plan: `plan.md` · Spec: `docs/superpowers/specs/2026-09-19-monitor-symbolic-engines-design.md`

Rule: `uv run pytest -q` after every task. **The bar is the Task 0b baseline — `2 failed` (both CRM, out of scope), nothing else.** No git commands — user commits.

- [ ] **Task 0** Toolchain: `pyswip`, `pyDatalog`, `bppy` in pyproject; `opa` binary at `~/.local/bin/opa` (or `$OPA_BIN`); README install section; record the baseline
- [ ] **Task 0b** Unbreak `fire.notify`: restore `_GATE_RUNG_RECIPIENTS` (HEAD bug, 4 tests) → new baseline `2 failed`
- [ ] **Task 1** Prolog (I14): `app/symbolic/{__init__,prolog}.py`, `rules/monitor.pl`; `actor_is_charge` delegates; `tests/symbolic/test_prolog.py` (14)
- [ ] **Task 2** OPA (I5, I9, I18): `app/symbolic/opa.py`, `policy/monitor.rego`; `move_authorized`/`release_authorized` delegate; `audit_denial(layer=)`; `_shared.release_case` / `terminal.denied` / `gate` senior branch / **`routers.route_gate`** all stop duplicating the rule; `tests/symbolic/test_opa.py` (8) + 1 in `test_denial.py` + 1 in `test_routers.py`
- [ ] **Task 3** BPpy: `app/monitor/bthreads.py` (incl. `senior_reminder`); `tests/monitor/test_bthreads.py` (12)
- [ ] **Task 4** `fire.handle` + `_context` + `_recipient_class` + OPA gates + `decision` column / `record_decision`; 7 new tests in `test_fire.py`; existing notify + senior-reminder tests unchanged and green
- [ ] **Task 5** Sweeper routes through `fire.handle`; 1 new test in `test_sweeper.py`
- [ ] **Task 6** Temporal monitor pass (I15/I16): `app/symbolic/datalog.py`, `timers.all_rows` / `escalation_exists`, `sweeper._check_invariants`; `tests/symbolic/test_datalog.py` (7) + 2 in `test_sweeper.py`
- [ ] **Task 7** Docs: `app/monitor/README.md` files list, mermaid, symbolic-layers section, reminder flavours; live `uv run sweeper` tick

## Known-red at HEAD (not this plan's doing)

| Test | Cause | Owner |
| --- | --- | --- |
| `tests/monitor/test_fire.py::test_notify_delivers_a_reminder_while_the_gate_is_still_open` | `NameError: _GATE_RUNG_RECIPIENTS` (`fire.py:130`) | **Task 0b** |
| `tests/monitor/test_fire.py::test_notify_widens_to_any_charge_nurse_on_the_second_rung` | same | Task 0b |
| `tests/monitor/test_fire.py::test_notify_stops_sending_once_the_recipients_budget_is_spent` | same | Task 0b |
| `tests/test_denial.py::test_a_refused_gate_keeps_its_reminders_deliverable` | same | Task 0b |
| `tests/test_failures.py::test_a_crm_outage_degrades_and_continues` | `state["degraded"]` is `[]` — the CRM outage no longer records a degrade | **user, out of scope** |
| `tests/test_graph.py::test_clean_case_walks_the_documented_arrows_in_order` | `'AF·db' is not in list` — same missing CRM-degrade path | **user, out of scope** |

## Log

| When | What | Result |
| --- | --- | --- |
| 2026-09-19 | spike: bppy 1.0.4 / pyswip 0.3.3 / pyDatalog 0.22.4 / opa 1.20.2 in scratch venv | all four engines answer as the plan expects |
| 2026-09-20 | re-review against HEAD `c8a6d43` (11 commits later); ran full suite | 6 failed; plan rewritten (see `findings.md` § Re-review 2026-09-20) |
| 2026-09-20 | second sweep: every file in `git diff a3363f3..HEAD`, plus all 745 lines of `docs/SPECIFICATION.md` | found `routers.route_gate` duplicating the I14 rule (added to Task 2) + 5 spec-vs-code gaps for the user (`findings.md` § 6) |
| 2026-09-20 | fix wave + scoped re-review closed (see `.superpowers/sdd/2026-09-19-monitor-symbolic-engines/progress.md` ledger for detail) | plan DONE: 256 passed, 0 failed |

## Scope

Symbolic engines (Prolog, OPA, BPpy, Datalog) are wired into `app/monitor/` only — gate authorization, dispatch/notify decisions, and the temporal sweeper pass. The rest of the app (`app/graph/`, `app/deterministic.py` outside the delegated predicates, intake/triage logic) still enforces its rules in plain Python. Extending symbolic engines beyond monitor is future work, not started.
