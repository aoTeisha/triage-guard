# Outage switch — system-wide component outages

## Context

The component-down demo took one component offline for one new submission, from the
new-case form. An outage affects every patient, so it moves to a board-level switch
that stays on until switched off, and reaches every action: new cases, gate answers,
moves to treatment, releases, and the sweeper's timers.

A switch that stays on also reaches the authorization checks after intake. Decided
with the user: an outage never blocks treatment or release. When the engine that
authorizes an action cannot answer, a shift lead signs before the action, and plain
Python still checks the facts the engine would have checked.

## Design

1. **Outage flags in Postgres.** Table `component_outages (component PRIMARY KEY, since)`
   in `app/monitor/schema.sql`. Module `app/outages.py`: `COMPONENTS`, `enabled()`
   (env `DEMO_OUTAGES=1`), `is_down(component)` (never touches the DB
   when disabled), `check(component)` raising `RuntimeError("simulated outage: …")`,
   `set_down(component, down)`, `down_list()`.
2. **Hooks at the lowest call into each component:** `acuity_classifier.classify`
   (llm), `opa.evaluate` inside its `try` (opa), `prolog._engine` (prolog; the cached
   engine moves behind it), `datalog.acuity_provenance` and `tick_invariants` inside
   their `try` (datalog), and `sweeper.run_once` skipping the tick and the heartbeat
   (monitor).
3. **Shift lead stands in when an authorization engine cannot answer.**
   `deterministic.move_authorized` / `release_authorized` (OPA) and
   `prolog.may_resolve_gate` (Prolog): a real answer is kept; no answer → only
   `shift_lead`, with Python checking `safety_passed and approved` (move), a valid
   reason (release), and the why starts with `SHIFT_LEAD_STANDS_IN`. The nodes add
   `degraded=["opa"]` / `["prolog"]` when that happens.
4. **Writeback timer:** OPA unable to answer → `FAILED` (retried), not `CANCELLED`.
5. **Board API:** `GET /api/outages` → `{enabled, components, down}`;
   `PUT /api/outages/{component}` `{down: bool}`; 404 when not enabled.
6. **Board UI:** "System health" button in the header with a toggle per component; a
   red "Simulated outage: …" strip while any is on; move/release get a role picker
   that defaults to shift lead with a note while OPA is down; the gate defaults to
   shift lead with a note while Prolog is down.
7. **Remove** the per-case `component_down` scenario (form, `intake.py` patching,
   `mock_cases.py`, its tests).

## Verification

triage-app: outage module tests; each hook raises when flagged and not otherwise;
shift-lead fallback for move/release/gate (allowed for shift lead, refused for others,
facts still checked, real denial kept); writeback FAILED on OPA outage; sweeper skips
the tick when the monitor is flagged. Board: outage API tests, lifecycle tests with a
switch on. Full suites both apps.
