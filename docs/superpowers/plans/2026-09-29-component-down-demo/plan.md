# Component-down demo scenario

## Context

User wants a demo case showing the system keeps working when one component is
unavailable (LLM, Prolog, Datalog, OPA, monitor). Principle chosen: **degrade to a
human, never stop the patient's path** — but never unsafe (no unproven payload to
the model, no approval bypass).

Exploration showed the demo can't be a toggle alone — today three outages leave
the case stuck:
- **OPA down** → privacy check counts "engine unavailable" as a PII leak →
  `V_HALT_PII` → `awaiting_recovery`; recovery re-halts, release refused (OPA).
- **Prolog/Datalog down in safety** → verdict `fail` with `safety_fail` reason →
  gate demands an acuity change (I7) the nurse has no reason to make.
- **LLM down** → already works (`classifier_fallback`, nurse acuity, flagged).
- **Monitor** is a separate process; board computes `monitor_degraded` but never
  renders it.

Decisions made with user: one submission type `component_down` + second
dropdown (like `trace_violation`); OPA-down degrades instead of halting;
validator-down gate offers **revalidate** (no acuity change needed), never override.

## Design

### Graph fixes (triage-app)

1. **OPA privacy check down → skip the model, continue.**
   - `app/deterministic.py` `verify_no_identifiers` / `app/verification.py`
     `verify_redacted_payload`: tell "OPA couldn't answer" (every deny reason
     starts `engine_unavailable:opa` and the regex scan is clean) apart from a real
     leak. Real leak keeps halting exactly as today.
   - `app/graph/nodes/redaction.py`: on engine-unavailable, keep the payload in
     state (internal only, Datalog provenance reads it), add
     `degraded=["opa"]`, audit new transition `PRIVACY_GATE_DOWN`
     (`app/labels.py`, readable value `privacy_gate_down`, engines `["OPA"]`).
   - `app/graph/routers.py` `route_after_redaction` + `app/graph/build.py`: that
     state routes to `classifier_fallback` — model never sees the unproven payload;
     nurse's acuity used, cross-check off, flagged. Same path as LLM-down.
2. **Prolog/Datalog down in safety → validator-down gate.**
   - `app/actors/safety.py` `validate`: when `provenance["engine_error"]` or
     `prolog_error` is set, raise the existing `ValidatorUnavailable` instead of a
     `fail` verdict → existing `_on_safety_error` → `safety_fallback`
     (`app/graph/nodes/safety.py`), already `degraded=["safety_validation"]`.
   - `safety_fallback` sets new reason `human_bridge.VALIDATOR_DOWN`
     (`app/actors/human_bridge.py`, options `["revalidate", "escalate_further"]`).
   - `app/graph/nodes/gate.py`: `VALIDATOR_DOWN` + `revalidate` → skip I7 change
     check (engine caused the failure, not the input), bump `correction_rounds`,
     back to `safety_validating`. `escalate_further` reuses existing branch.
   - `app/graph/routers.py` `route_gate`: the rounds/escalate check that tests
     `escalation_reason == "safety_fail"` also covers `VALIDATOR_DOWN`, so a long
     outage hands off to shift lead after `MAX_CORRECTION_ROUNDS`.
   - Check the after-run trace check (`app/verification.py` / `tests/test_trace_check.py`)
     accepts `PRIVACY_GATE_DOWN` and the validator-down loop; extend rules if not.

### Demo plumbing (board)

- `board/board/mock_cases.py`: `SubmissionType` += `"component_down"` (clean
  payload branch); `COMPONENTS = ("llm", "prolog", "datalog", "opa")`.
- `board/board/intake.py`: `SubmitRequest.component` validated like `violation`
  (422 when missing/unknown); `/api/demo-cases` adds `"components"`; submit wraps
  `runner.start_case` in the component's patch, same one-process caveat comment as
  `safety_fail`:
  - llm → `patch("app.actors.acuity_classifier.classify", side_effect=RuntimeError(...))`
  - prolog → `patch("app.symbolic.prolog._engine", side_effect=...)` (engine fault → `PrologEngineError` path)
  - datalog → patch the pyDatalog engine call inside `app.symbolic.datalog` so
    `acuity_provenance` returns its own `engine_error`
  - opa → `patch("app.symbolic.opa._via_server"/"_via_subprocess", side_effect=ConnectionError(...))`
    so the real `except` in `opa.evaluate` runs
- Static UI: `index.html` radio "Component down — one service unavailable" + hidden
  `#component-fields` select; `labels.js` `COMPONENT_LABELS`, `DEGRADED_LABELS` +
  `DEGRADED_EFFECTS` for `acuity_classifier`, `opa`, `prolog`, `datalog`,
  `safety_validation`; `panel.js` "Running degraded" box;
  `GATE_OPTIONS/GATE_HEADINGS.validator_down`; `intake.js` show/hide + send
  `component` (mirror `violation`); `board.js` change listener.
- **Monitor**: not in the dropdown (it's a separate process; a per-request patch
  can't take it down). `queue.js` `renderCounters` renders a "monitor degraded —
  reminders paused" banner from existing `monitor_degraded`. Demo = stop the
  sweeper; cases still flow to the queue.

### UI names the engine and what it skipped

`degraded` gets one key per engine, so the UI can say exactly which one is down:
`acuity_classifier`, `opa`, `prolog`, `datalog` (plus existing `crm`,
`safety_validation` for a generic validator crash). `ValidatorUnavailable` carries
which engine(s) failed; `safety_fallback` writes those keys.

Two places on the board:
- **Card chip** (`queue.js`, `DEGRADED_LABELS`): short — "OPA down", "Prolog down",
  "Datalog down", "AI down".
- **Case panel "Running degraded" box** (`panel.js`, new `DEGRADED_EFFECTS` in
  `labels.js`): one box per down engine, listing consequences:

| Down | Panel box |
|---|---|
| OPA | **OPA unavailable** · no privacy check on the payload · AI classifier not used (data not proven clean) · nurse's acuity kept · flagged for later review |
| Prolog | **Prolog unavailable** · safety rules not checked · charge nurse must revalidate · AI acuity still used |
| Datalog | **Datalog unavailable** · acuity history / who-set-it not checked · charge nurse must revalidate · AI acuity still used |
| AI classifier | **AI classifier unavailable** · no second opinion on acuity · nurse's acuity kept · flagged for later review |

Gate heading for the validator-down gate names the engine too:
"Safety check incomplete — Prolog unavailable. Revalidate when it's back."

### Expected demo outcomes

| Component | Case ends | Shown on board |
|---|---|---|
| llm | queue, nurse's acuity | chip "AI down" + panel box |
| opa | queue, nurse's acuity, model skipped | chip "OPA down" + panel box |
| prolog / datalog | charge-nurse gate "revalidate"; revalidate → queue | chip "Prolog down"/"Datalog down" + panel box + gate heading |
| monitor (sweeper stopped) | queue as normal | top banner "Monitor down — reminders and reassessment timers paused" |

### Out of scope (recorded in findings.md, not fixed)

Real long outages still have stuck spots outside the intake path: OPA refuses
every release/move; Prolog down blocks `may_resolve_gate` so nobody can answer the
gate; OPA-down writeback timer is `CANCELLED` (CRM visit lost); Datalog failure in
`find_duplicate_active_case` fails open mislabelled as CRM; sweeper invariant pass
fails silently; enum quoting in `prolog._term` means two safety.pl rules never fire.

## Files / workflow

- Tracking trio in `docs/superpowers/plans/2026-09-29-component-down-demo/`
  (`plan.md` TDD tasks, `progress.md`, `findings.md` incl. stuck paths above).
- `docs/STATUS.md` entry, `docs/SYSTEM_MODELING.md` Safe Fallbacks: add OPA
  privacy-gate and validator-down-revalidate behavior.
- No git commands. No arrow codes.

## Verification

- triage-app unit tests (TDD, write first): `tests/test_failures.py` — OPA
  engine-down reaches `classifier_fallback`, never calls classifier, not halted;
  real leak still halts; Prolog-down and Datalog-down reach `validator_down` gate;
  `revalidate` with no change → safety passes → queue; repeated outage → shift
  lead. Update `tests/test_safety_validation.py` engine-error tests (now raise).
  `cd triage-app && uv run pytest`.
- board tests: `board/tests/intake/test_case_lifecycle.py` parametrized over
  `COMPONENTS` (status + degraded chip + gate reason); `test_intake_api.py`
  422 on missing/unknown component, `/api/demo-cases` lists components. Needs
  `docker compose -f db/docker-compose.yml up -d postgres`.
- Manual: run board, submit each component, check outcome table above; stop
  sweeper, see banner.
