# Triage Guard — LangGraph control plane (skeleton)

The system runs as a **LangGraph `StateGraph`**: a deterministic workflow whose
allowed transitions are declared as data, with exactly one LLM call in the whole
decision path.

Everything is a skeleton — mock actors, no symbolic engines, no real model unless
you ask for one — but nothing is faked away to make it run. The gates really
pause, the retries really count, the refusals really refuse.

## Run it

```bash
uv sync
uv run triage-guard            # clean case — full happy path
uv run triage-guard missing    # arrow 16 — missing fields
uv run triage-guard failed     # arrow 17 — nothing usable
uv run triage-guard injection  # arrow 18 — prompt injection rejected
uv run pytest                  # 92 tests, offline, ~40s
uv run plot                    # regenerate docs/diagrams/control-plane.mmd
```

Every run prints the final state and the full audit trail, labelled with the
arrows from `docs/SPECIFICATION.md` § Transitions.

## Why LangGraph

CrewAI was chosen (2026-08-28, `f63d621`) for an architecture that had an
**Orchestrator Agent** as sole writer to State — a manager-plus-workers topology
CrewAI fits well. The move to workflow orchestration (`94d2218`) removed the
orchestrator but carried the framework forward by rename rather than
re-evaluation, leaving the codebase importing one decorator module and none of
CrewAI's multi-agent machinery.

What LangGraph gives that the Flow could not:

| Spec requirement | Here |
| --- | --- |
| A declared set of allowed edges | `add_conditional_edges(node, router, {label: node})` in `graph/build.py` |
| Per-agent retry budget `N` | `RetryPolicy(max_attempts=N)` per node, from `budgets.py` |
| `BLK` — refuse an action, case does not move | `Route.DENIED` → `action_denied` → END |
| A human gate that survives a restart | `interrupt()` + checkpointer, no model interpreting the clinician |
| A diagram that cannot lie | `draw_mermaid()` renders the declared edge set |

The last one matters more than it sounds: CrewAI's `plot()` inferred edges by
reading source and silently dropped the ones it could not resolve.

## Deterministic vs. LLM

Eleven of twelve actors are deterministic code, humans, or data stores. The split
is enforced structurally — `actors/acuity_classifier.py` is the only module in the
package permitted to import an LLM client.

| Actor / step | Here | Kind |
| --- | --- | --- |
| Acuity Classifier | `actors/acuity_classifier.py` | **LLM** (mock by default) — with a deterministic red-flag pre-check first |
| Intake Parser | `actors/intake.py` + `guards/` | deterministic |
| Input Normalizer + PII drop | `actors/normalizer.py` | deterministic (BERT urgency is mocked) |
| Safety Validation | `actors/safety.py` | deterministic (mock; real = Prolog/Datalog/Z3/OPA) |
| Output Verification | `verification.py` | deterministic |
| CRM / Patient DB | `crm_client.py` | deterministic |
| Audit | `deterministic.py:audit` | deterministic |
| Human Escalation / nurses / tech | `actors/human_bridge.py` | human |

The red-flag pre-check runs **before** the model in mock and live mode alike — a
safety rule that only fires when the model is switched on is not a safety rule.

## Layout

```
app/
  main.py            entrypoint — demo cases, audit trail, plot()
  runner.py          start / resume / snapshot a case (used by CLI *and* UI)
  states.py          State enum — the spec's control plane
  events.py          Event enum + IntakeOutcome
  labels.py          Arrow (spec traceability) + Route (graph-internal)
  budgets.py         retry budgets N + the correction-round loop guard
  verification.py    output verification: schema, range, invariants
  deterministic.py   order_key, acuity bands, audit records, OPA predicates
  graph/
    build.py         THE transition table — nodes + conditional edge maps
    nodes.py         one function per control state
    routers.py       the guards, as pure functions
    state.py         TriageState + reducers
  actors/            one module per participant; mocks/ holds canned output
  guards/            deterministic intake guards
  schemas/           Pydantic payload schemas (verification + LLM response_format)
  crm_client.py      CRM stub client
  observability.py   Langfuse callback handler + manual spans
  mock_cases.py      the demo inputs
```

Actor spec cards live in `docs/actors/<actor>.jsonc` — one per row of the spec's
Actors/Agents table. Only the classifier has a persona file here
(`actors/acuity_classifier.json`), because only it is fed to a model.

## Mocks

Canned output lives in `actors/mocks/*.json`. **Edit the JSON, not the Python.**

`TRIAGE_LLM=live` swaps the classifier for a real model; nothing else changes.
Mock mode does *not* skip the gate — the graph interrupts either way, and the
canned charge-nurse reply is fed back through the same `resume` path the browser
uses.

## Wiring reality in

1. **Safety Validation → symbolic engines.** Replace `actors/safety.py:validate`
   and the predicates in `deterministic.py`. The graph already routes pass / fail
   and the V·* rows off their results.
2. **Urgency scorer.** Replace `actors/normalizer.py:score_urgency`; raise
   `ScorerUnavailable` and the safe-drop degrade path is already wired.
3. **CRM.** Start `crm-stub/` and set `CRM_BASE_URL`.
4. **Human gate.** Already real. Point a UI at `/resume/{case_id}`.
5. **Retry budgets.** `budgets.py` holds working defaults standing in for a
   documented unknown (`SYSTEM_MODELING.md` § Unknowns). Replace from measured data.
6. **World plane.** `monitoring` is where this slice ends. `reassessment_required`
   and `case_closed` are in `State` but declared in `UNIMPLEMENTED_STATES`; a test
   fails if a state is neither wired nor declared.
