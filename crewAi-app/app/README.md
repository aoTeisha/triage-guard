# Triage Guard — CrewAI Flow (skeleton)

This app runs Triage Guard as a **CrewAI `Flow`** — a deterministic workflow
whose steps are wired in code, with exactly one LLM call. It replaces the earlier
crew-of-tasks wiring (`crew.jsonc` + `main.py`'s manual task loop) with the Flow
described in `docs/SPECIFICATION.md` ("The system runs as a CrewAI Flow").

Built against <https://docs.crewai.com/v1.15.17/en/concepts/flows> using
`@start` / `@listen` / `@router` / `or_`.

Everything is a **skeleton**: mock input/output flows end to end with no LLM, no
running CRM stub, and no formal-layer engine. Swap the mock producers one at a
time; the state shape and the routing stay put.

## Run it

```bash
uv run triage-guard            # clean demo case (full happy path)
uv run triage-guard missing    # arrow 16 — missing fields
uv run triage-guard failed     # arrow 17 — nothing usable
uv run triage-guard injection  # arrow 18 — prompt-injection rejected

uv run tests                   # smoke tests (routing shape) + guard tests
```

Each run prints the final state plus the full `emit_event_log` audit trail with
the spec's arrow labels.

## What's deterministic vs. LLM

The split follows the **Actors / Agents** table in `docs/SPECIFICATION.md`. The
neuro-symbolic rule is enforced structurally: the deterministic actors are plain
Python, and only one function is allowed to call a model.

| Actor / step (spec)                | Here                                   | Kind          |
| ---------------------------------- | -------------------------------------- | ------------- |
| Acuity Classifier                  | `flow/agents.py:invoke_acuity_classifier` | **LLM** (mock) — with a deterministic red-flag pre-check first |
| Intake Parser                      | `guards/` + `flow/triage_flow.py:parsing` | deterministic |
| Input Normalizer + PII schema-drop | `flow/agents.py:build_model_payload`   | deterministic |
| Safety Validation                  | `flow/agents.py:invoke_safety_validation` | deterministic (mock; real = Prolog/Datalog/Z3/OPA) |
| Output Verification                | `flow/deterministic.py:verify_output`  | deterministic |
| CRM / Patient DB                   | `crm_client.py`                        | deterministic |
| Audit                              | `flow/deterministic.py:emit_event_log` | deterministic |
| Human Escalation / Nurses / Tech   | `flow/agents.py:invoke_human_escalation` | human (mock)  |

**The only place a model is ever called is `invoke_acuity_classifier`.** Its
red-flag pre-check is deliberately deterministic and runs *before* the model, so
a hard clinical trigger forces emergent (`acuity_source = rule_forced`) without
the LLM. Every other step is code — that is the whole point of the split.

## Layout

```
app/
  main.py              entrypoint — seeds mock state, kickoff(), prints audit trail
  mock_cases.py        the four editable demo inputs (clean/missing/failed/injection)
  agents/              one .jsonc per actor in the spec's Actors/Agents table
    mocks/             one canned-output .json per actor that returns mock data
  flow/
    triage_flow.py     the Flow: @start/@listen/@router control-plane spine
    state.py           TriageState — the Data/Control/World planes as one state
    deterministic.py   order_key, acuity-gap resolution, audit, symbolic predicates
    agents.py          one function per actor; the single LLM step lives here
  guards/              deterministic intake guards (reused, unchanged)
  schemas/             pydantic task-output schemas (legacy, unused)
  crm_client.py        CRM SQLite-stub client (reused)
  observability.py     Langfuse spans (reused)
```

Every actor in the table above has a matching `app/agents/<actor>.jsonc`
(role/type/reads/proposes/tech, copied from the spec). The ones that return
canned data also have `app/agents/mocks/<actor>.json` — edit the JSON to
change what a mock step returns, not the Python. The one actor that's a real
LLM call, Acuity Classifier, keeps the extra `role`/`goal`/`backstory`/`llm`
fields a crewAI `Agent` needs; the deterministic/human/store actors don't —
giving them an LLM persona is exactly the pre-Flow crew-of-tasks pattern this
app replaced.

## Wiring reality in, step by step

1. **Acuity Classifier → real LLM.** In `invoke_acuity_classifier`, replace the
   mock branch with a real call: load `app/agents/acuity_classifier.jsonc` as
   a crewAI `Agent` (it's the only LLM actor, so no crew/manager wiring is
   needed) and run it. Keep the red-flag pre-check ahead of it.
2. **Safety Validation → symbolic engines.** Replace `invoke_safety_validation`
   and the `verify_*` predicates in `deterministic.py` with OPA / Z3 / Prolog /
   Datalog calls. The Flow already routes `pass` / `fail` and the V·* verification
   outcomes off their results.
3. **CRM.** Start `crm-stub/` and set `CRM_BASE_URL`; `fetch_patient_data`
   already maps found / not_found / db_error to the degrade path (`AF·db`).
4. **Human Escalation.** Put a real UI/queue behind `invoke_human_escalation`.
5. **Monitoring / treatment-move / release.** The skeleton ends at `monitoring`;
   extend with the World-plane transitions (`1b.y`, `19`, `FV`, `REL`) and the
   treatment-move execution state machine from `SYSTEM_MODELING.md`.

## Note on `plot()`

`app.main:plot` calls `Flow.plot("TriageFlowPlot")`. Because the routers return
their labels from `if` branches rather than as static literals, CrewAI's static
plotter prints "events not statically inferable" warnings and may omit some edges
in the picture — runtime routing is unaffected (the smoke tests cover it). If you
want a complete static plot, hoist the router return values into module-level
label constants the plotter can see.
