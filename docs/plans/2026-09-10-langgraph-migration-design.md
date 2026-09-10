# Triage Guard — CrewAI → LangGraph migration design

**Date:** 2026-09-10
**Status:** executed — see "Outcome" at the end of this document
**Scope:** migrate the workflow engine, remove dead/duplicated code, and land every
outstanding change agreed in the review so far — ending at a working, mock-driven
skeleton wired to the intake UI with no shortcuts.

---

## 1. Why

CrewAI was adopted on 2026-08-28 (`f63d621`), when the architecture had an
**Orchestrator Agent** as the sole writer to State — a manager-plus-workers topology
that CrewAI's hierarchical process fits well.

On 2026-09-10 (`94d2218`, `d25e827`) the architecture moved to deterministic workflow
orchestration. That commit is a rename: 100 insertions, 101 deletions, substituting
"Flow" for "Orchestrator" throughout, with CrewAI written into the replacement text
(`| Agent orchestration | **CrewAI** |` → `| Workflow orchestration | **CrewAI Flows** |`).
The framework was carried forward by the wording of the rename, not re-evaluated.

The result today: `crewAi-app` imports exactly one CrewAI module —
`from crewai.flow.flow import Flow, listen, or_, router, start` — and no `Agent`,
`Crew`, or `Task`. 154 locked packages, including `chromadb`, `onnxruntime` and
`tokenizers`, support one decorator.

Two consequences of the rename are live defects:

- `ACTION_DENIED` is attributed to `Flow (governance)`, but a CrewAI `@router` must
  return a branch and every branch runs. There is no primitive for "the attempt is
  refused and the case does not move."
- `state_machine.py` and `deterministic.move_authorized()` — the orchestrator's
  *judgment* half — are written, tested, and called from nowhere. They are not dead
  code; they are the part of the orchestrator that lost its home.

### Framework fit against the spec

| Spec requirement | CrewAI Flow | LangGraph |
| --- | --- | --- |
| Declared set of allowed edges | none; `@router` returns any string | `add_conditional_edges(node, fn, {label: node})` — first-class path map |
| Per-agent retry budget `N` | hand-rolled counter | `RetryPolicy(max_attempts=N, retry_on=…)` per node |
| Degrade path on exhaustion (`AF·<agent>`) | hand-rolled branches | `error_handler=` per node returning `Command(update=…, goto=…)` |
| Human gate, durable | `@human_feedback` — but classifies the human's free text with an LLM | `interrupt()` / `Command(resume=…)` — structured, no model |
| Durable state | `@persist` | checkpointer + `thread_id` + checkpoint history |
| Accurate control-plane diagram | `plot()` infers edges from source; currently broken | `draw_mermaid()` renders the declared edge set |
| Langfuse | manual spans | first-class callback handler |

The `@human_feedback` LLM classifier is disqualifying on its own: it would place
`gpt-4o-mini` between a charge nurse and her acuity ruling, breaking the spec's
invariant that the Acuity Classifier is the only generative model in the decision path.

### Migration is cheap because the core never touched the framework

`guards/`, `deterministic.py`, `crm_client.py`, `schemas/`, `mock_cases.py` and all
mock JSON are plain Python and Pydantic. They move unchanged. One file
(`flow/triage_flow.py`, 236 lines) is rewritten; 5 of 28 tests are rewritten.

---

## 2. Target architecture

### 2.1 Directory rename

`crewAi-app/` → `triage-app/`. The distribution name (`triage-guard`) and the
import package (`app`) are unchanged, so the only external edit is one line in
`intake-channel/pyproject.toml`:

```toml
[tool.uv.sources]
triage-guard = { path = "../triage-app", editable = true }
```

Done with `git mv` so history follows.

### 2.2 Layout

```
triage-app/
  pyproject.toml
  .env.example                    NEW — lives beside the project that reads it
  app/
    main.py                       kickoff() + demo runner
    states.py                     State enum (control plane)
    events.py                     Event enum
    labels.py                     Arrow + Route labels (spec traceability)
    budgets.py                    RETRY_BUDGET table (per-agent N)
    graph/
      __init__.py                 exports build_graph, TriageState
      state.py                    TriageState + reducers
      nodes.py                    one function per control-plane node
      routers.py                  conditional-edge functions (pure, testable)
      policies.py                 RetryPolicy + error_handler per node
      build.py                    StateGraph wiring — THE transition table
    actors/
      __init__.py
      intake.py                   deterministic parser
      normalizer.py               PII schema-drop + urgency (mock BERT)
      acuity_classifier.py        the single LLM (mock | live)
      safety.py                   symbolic validator (mock)
      human_bridge.py             interrupt() wrappers for the two gates
      mocks/*.json                canned outputs (unchanged content)
    verification.py               verify_output — schema + range + invariants
    deterministic.py              order_key, acuity resolution, audit, OPA stubs
    guards/                       unchanged
    schemas/                      unchanged (now load-bearing)
    crm_client.py                 unchanged
    observability.py              Langfuse callback handler + span helper
    mock_cases.py                 unchanged
    mock_data.py                  unchanged (imported by intake-channel)
  tests/
docs/
  actors/                         NEW — the 11 non-LLM actor .jsonc spec cards
```

**Retired:** `app/flow/` (replaced by `app/graph/`), `app/state_machine.py`
(superseded — see §2.5).

### 2.3 Why these module boundaries

Split by *who owns it and when it changes*, not one-concept-per-file:

| Module | Changes when |
| --- | --- |
| `states.py`, `events.py` | the spec's control plane changes |
| `labels.py` | the graph's internal wiring changes |
| `budgets.py` | production timeout data arrives |
| `graph/build.py` | the Transitions table changes |
| `actors/` | an actor's implementation is swapped mock → real |

`states.py` and `events.py` are shared vocabulary — the graph and the spec both
refer to them, neither owns them. That is the seam that stops drift.

### 2.4 The transition table becomes the graph

`docs/SPECIFICATION.md` § Transitions transcribes directly into `build.py`:

```python
builder.add_conditional_edges(
    State.PARSING,
    route_intake,
    {
        Event.DATA_PARSED:             State.RESOLVING_IDENTITY,
        Event.MISSING_FIELDS_DETECTED: State.MISSING_FIELDS_REQUESTED,
        Event.SUBMISSION_FAILED:       State.SUBMISSION_FAILED,
        Event.INVALID_INPUT_DETECTED:  State.INPUT_REJECTED,
    },
)
```

The set of legal moves is data the framework holds, validates at compile time, and
renders. It cannot silently disagree with what runs.

### 2.5 What happens to `state_machine.py`

It is superseded, and this resolves the A/B/C question from the review — the answer
becomes **C, with the framework doing the work**:

| `state_machine.py` provided | Replacement |
| --- | --- |
| `State`, `Event` enums | promoted to `app/states.py`, `app/events.py` |
| the transition table | `graph/build.py` edge maps |
| guard evaluation per edge | `graph/routers.py`, called by conditional edges |
| `accepted=False` (the `BLK` row) | `Route.DENIED` → `action_denied` node → `END`, state untouched |

Its 12 tests are **not deleted** — they are rewritten as two stronger suites:
assertions over the compiled graph's declared edge set (`graph.get_graph()`), and
unit tests of the router functions in isolation. Testing the edge map directly is
closer to the spec than testing a hand-written table was.

### 2.6 Denial (`BLK`) in LangGraph

```python
def route_authorization(state: TriageState) -> str:
    ok, reason = move_authorized(state, state.actor_role)
    return Route.PROCEED if ok else Route.DENIED

builder.add_conditional_edges(
    State.TREATMENT_MOVE_REQUESTED,
    route_authorization,
    {Route.PROCEED: State.EXECUTING_MOVE, Route.DENIED: State.ACTION_DENIED},
)
```

`action_denied` appends the `BLK` audit record with the denying layer and reason,
writes **no** other state, and goes to `END`. The case is parked exactly where it
was; a later event re-enters the graph. This is the spec's "the attempt is refused,
the case does not move," expressed natively.

`deterministic.move_authorized()` gets its first caller here.

### 2.7 Node inventory

| Node | Kind | Spec arrows |
| --- | --- | --- |
| `intake_received` | deterministic entry | 1a, 2 |
| `parsing` | deterministic guards | 3 |
| `missing_fields_requested` | terminal | 16 |
| `submission_failed` | terminal | 17 |
| `input_rejected` | terminal (security) | 18 |
| `resolving_identity` | CRM (retry, fail-open) | 4b |
| `redacting_routing` | schema-drop + OPA (fail-closed) | 5, 6, V·halt·PII |
| `classifying` | **the one LLM** (retry) | 7, 8 |
| `acuity_resolution` | deterministic bands | 9a, 9b, 9c |
| `awaiting_approval_acuity` | `interrupt()` | 1b.z·acuity |
| `safety_validating` | symbolic (retry) | 9→10, 10·fail |
| `awaiting_approval_safety` | `interrupt()` | 1b.z·safety |
| `monitoring` | terminal for this slice | 11·pass |
| `agent_failed` | halt | AF·*, V·halt |
| `action_denied` | refusal, no state change | BLK |

### 2.8 Retry budgets

`app/budgets.py`, with the doc's unknown preserved:

```python
# SYSTEM_MODELING.md § Unknowns: "Per-agent retry budgets are undefined in the
# spec. We do not invent a number." These are working defaults so the skeleton
# runs; replace from real timeout data before production.
RETRY_BUDGET = {
    "acuity_classifier": 2,   # LLM: transient timeouts/malformed JSON. Capped at 2 —
                              # P95 <= 8s allows ~3 total attempts.
    "safety_validation": 1,   # symbolic engine: one blip, then route to charge nurse
    "crm":               2,   # network, non-critical, fail-open
    "pii_schema_drop":   0,   # pure deterministic code — a retry cannot fix a fault.
                              # Critical-closed: halt immediately.
    "pii_bert_ner":      1,   # model service; on exhaustion drop free text and continue
    "human_bridge":      2,   # network to UI/queue, then manual notification channel
}
```

Consumed as `RetryPolicy(max_attempts=RETRY_BUDGET[name])` on `add_node`.
`N = 0` is a deliberate design statement, not a placeholder.

### 2.9 Human gates

```python
def awaiting_approval_acuity(state: TriageState) -> dict:
    decision = interrupt({
        "reason": "acuity_gap",
        "case_id": state.case_id,
        "nurse_proposed_acuity": state.nurse_proposed_acuity,
        "system_proposed_acuity": state.system_proposed_acuity,
        "options": ["use_nurse_acuity", "use_system_acuity"],
        "required_role": "charge_nurse",
    })
    ...
```

Resumed with `Command(resume={"decision": …, "resolver_role": "charge_nurse"})`.
No model interprets the clinician. The resolver's role is checked deterministically
before the decision is applied; a non-charge resolver routes to `action_denied`.

In mock mode the bridge auto-resumes from `actors/mocks/human_escalation.json`, so
the demo runs unattended without the gate being fake — the same `interrupt()` fires,
the mock just supplies the resume payload.

### 2.10 Persistence

`SqliteSaver` at `triage-app/.triage_state.db` (gitignored). `thread_id = case_id`,
so a case is a thread. Required anyway — `interrupt()` does not work without a
checkpointer. Checkpoint history becomes a second, framework-level audit trail
alongside `emit_event_log`.

### 2.11 The single LLM call

```python
llm = ChatOpenAI(
    model=os.environ["MODEL"],                      # anthropic/claude-opus-5
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
).with_structured_output(AcuityProposal)
```

- `anthropic/claude-sonnet-4` is **not** in OpenRouter's live model list — the current
  config would fail on first call. Verified live ids include `anthropic/claude-opus-5`
  and `anthropic/claude-fable-5.1`. Default to Opus: clinical classifier, strongest
  reasoning.
- Model comes from `.env` only. `agents/acuity_classifier.jsonc` becomes
  `actors/acuity_classifier.json` (no comments — `json.loads` rejects `//`) holding
  only `role`/`goal`/`backstory`, rendered into the prompt.
- `AcuityProposal` (`schemas/acuity_proposal.py`) enforces `1 <= acuity <= 5` at the
  boundary. It is no longer "legacy, unused."
- `TRIAGE_LLM=mock|live` (default `mock`) selects the canned proposal or the real call.
  The deterministic red-flag pre-check runs first in **both** modes.

### 2.12 Output verification everywhere

`verify_output` is called at all five proposal points, not one:

| Node | Verifies |
| --- | --- |
| `parsing` | `ParseResult` schema |
| `resolving_identity` | CRM record shape |
| `redacting_routing` | payload schema **and** `verify_no_identifiers` (structural → `V·halt·PII`) |
| `classifying` | `AcuityProposal` (Pydantic gives schema + range) |
| `safety_validating` | `SafetyVerdict` schema |

Failures split per spec: **structural** (identifier leak, invariant breach) → no retry,
`agent_failed`. **Recoverable** (schema/range/null) → retry on the agent's budget
(`V·retry`), then that agent's `AF·` degrade path (`V·exhausted`).

### 2.13 Intake UI

`intake-channel` already does real CRM lookup and real payload construction, then
throws it at `MOCK_PARSE_RESULTS` — the pre-Flow mock loop. It becomes the front door:

```
POST /submit           → graph.invoke(case, config={"configurable":{"thread_id": case_id}})
                         returns either a final state or {status: "pending", interrupt: {...}}
POST /resume/{case_id} → graph.invoke(Command(resume=payload), config=...)
GET  /case/{case_id}   → graph.get_state(config) — current state + audit trail
```

The gate pause becomes visible in the UI instead of being mocked away. `mock_data.py`
and `observability.py` stay — the UI imports both.

### 2.14 Observability

Langfuse's first-class LangGraph callback handler replaces manual spans:

```python
graph.invoke(case, config={"configurable": {"thread_id": case_id},
                           "callbacks": [langfuse_handler]})
```

Every node is traced automatically. `observability.py` keeps `agent_span` for the
UI's own spans, and loses the import-time `load_dotenv()` / `get_client()` side
effect (moved behind a cached accessor).

---

## 3. Dependencies

**Remove:** `crewai[tools]>=1.15.16` (154 locked packages).

**Add:**
```toml
"langgraph>=1.2",                    # error_handler= requires >=1.2
"langgraph-checkpoint-sqlite>=2.0",
"langchain-openai>=0.2",             # OpenRouter via base_url
```
Unchanged: `httpx`, `langfuse`, `python-dotenv`, `pydantic`.

`[tool.crewai] type = "crew"` is deleted. Entry points: `triage-guard`, `kickoff`.

---

## 4. Phases

Each phase ends green. No phase leaves the tree broken.

### Phase 0 — Branch and baseline
- `git switch -c feat/langgraph-migration`
- Record baseline: `uv run tests` (28 passing).

**Done when:** baseline recorded.

---

### Phase 1 — Cleanup (framework-independent)
1. Delete duplicate `SafetyVerdict` from `flow/state.py`; import from `schemas/`.
2. Rewrite `schemas/__init__.py` docstring — it is load-bearing, not legacy.
3. **Keep** `mock_data.py` and `observability.py` (imported by `intake-channel`).
4. Remove the unused `integration` pytest marker (re-added in Phase 9 with a real test).

**Done when:** 28 tests still pass; one `SafetyVerdict` in the tree.

---

### Phase 2 — Domain vocabulary
Create `states.py`, `events.py`, `labels.py`, `budgets.py`. Move the `State` / `Event`
enums out of `state_machine.py`; it imports them for now.

**Done when:** `state_machine.py` defines no enums; its 12 tests pass unchanged; no
bare control-state string literals outside these modules.

---

### Phase 3 — The graph (mocks only, no LLM, no persistence)
1. `graph/state.py` — `TriageState` from `flow/state.py`, plus `id: str` and an
   `audit_log` reducer so concurrent writes append rather than clobber.
2. `graph/nodes.py` — one function per node from §2.7.
3. `graph/routers.py` — pure functions returning `Event`/`Route` labels.
4. `graph/build.py` — `StateGraph`, every edge from the Transitions table.
5. `actors/` — split `flow/agents.py` per actor; mocks move with them.
6. `main.py` — `kickoff()` runs the four demo cases.
7. Delete `app/flow/`.

**Done when:** all four demo cases reach their spec-correct terminal node;
`draw_mermaid()` renders the full control plane with no missing edges;
new tests assert the compiled edge set matches the Transitions table.

---

### Phase 4 — Fault tolerance
1. `RetryPolicy(max_attempts=RETRY_BUDGET[n])` per node.
2. `error_handler=` per node implementing each `AF·` row (classifier → nurse acuity +
   gate off + flag; PII drop → halt; safety → route all to charge; CRM → intake-only).
3. `verification.py` — real schema/range/invariant checks; wired at all five points.
4. `retry_count` actually incremented; `V·retry` → `V·exhausted` ladder live.

**Done when:** tests force each failure mode and assert the spec's arrow, terminal
node, and degrade flags. `V·halt·PII` halts. `pii_schema_drop` never retries.

---

### Phase 5 — Persistence and human gates
1. `SqliteSaver`, `thread_id = case_id`.
2. `interrupt()` at both gates; role checked before the decision applies.
3. Mock bridge auto-resumes from JSON; live mode returns pending.

**Done when:** a case pauses at the gate, the process exits, a new process resumes it
from the checkpoint and finishes with the correct acuity and `human_confirmed`.

---

### Phase 6 — Permission layer (`BLK`)
1. `action_denied` node; `Route.DENIED` from authorization routers.
2. `move_authorized()` called for the first time; role check on gate resolvers.
3. Retire `state_machine.py`; port its 12 tests to edge-map + router tests.

**Done when:** an unauthorized gate resolution and an unauthorized treatment move both
log `BLK`, change no state, and end the run. Test count ≥ baseline.

---

### Phase 7 — UI
1. `intake-channel` path dep → `../triage-app`.
2. `/submit` invokes the graph; `/resume/{case_id}`; `/case/{case_id}`.
3. `channel.js` renders the pending gate and posts a decision.

**Done when:** all four demo cases are driveable from the browser end to end,
including pausing at the acuity gate and resuming from the UI.

---

### Phase 8 — Observability
Langfuse callback handler on every invoke; fix the import-time side effect.

**Done when:** one submit produces a trace with a span per node.

---

### Phase 9 — LLM
1. `actors/acuity_classifier.json` (no comments), model from `.env`.
2. `with_structured_output(AcuityProposal)`; `TRIAGE_LLM=mock|live`.
3. Red-flag pre-check ahead of the model in both modes.
4. One `@pytest.mark.integration` test hitting a real model.

**Done when:** `TRIAGE_LLM=mock uv run tests` is green offline; `live` produces a
validated `AcuityProposal`; an out-of-range acuity is rejected by verification.

---

### Phase 10 — Docs and packaging
1. `git mv crewAi-app triage-app`; update `pyproject.toml`, entry points, `.env.example`.
2. Move the 11 non-LLM actor `.jsonc` files to `docs/actors/`.
3. `SPECIFICATION.md`: "CrewAI Flow" → LangGraph, `ACTION_DENIED` attributed to the
   permission layer. `SYSTEM_MODELING.md`: same. Both READMEs rewritten.
4. Regenerate the control-plane diagram from `draw_mermaid()` into `docs/diagrams.md`.
5. Record the decision in `docs/plans/` (this document) and reference it from the spec.

**Done when:** no "CrewAI" outside the historical note; the committed diagram is
generated from the compiled graph.

---

## 5. Test strategy

| Suite | Fate |
| --- | --- |
| `test_guards.py` (9) | unchanged |
| `test_crm_client.py` (4) | unchanged |
| `test_state_machine.py` (12) | rewritten as edge-map + router tests (Phase 6) |
| `test_flow.py` (5) | rewritten as `test_graph.py` (Phase 3) |
| **new** `test_edges.py` | compiled edge set vs. the Transitions table |
| **new** `test_failures.py` | every `AF·` and `V·` row (Phase 4) |
| **new** `test_gates.py` | interrupt/resume across a process boundary (Phase 5) |
| **new** `test_denial.py` | `BLK` rows (Phase 6) |
| **new** `test_llm.py` | `integration`-marked (Phase 9) |

Net: 28 → roughly 55. Every spec arrow that has code has a test.

---

## 6. Risks

| Risk | Mitigation |
| --- | --- |
| LangGraph API churn (`error_handler` needs ≥1.2, streaming `v3`) | pin minimums in `pyproject.toml`, commit `uv.lock` |
| Rename churn across two packages | `git mv`; single path-dep edit; Phase 10 last so the tree stays green |
| Pydantic state + reducers is a new pattern here | Phase 3 lands it alone, before fault tolerance stacks on top |
| `interrupt()` semantics (node re-executes from the top on resume) | keep gate nodes side-effect-free before the `interrupt()` call; covered by `test_gates.py` |
| Scope creep into full implementation | phases stop at the documented skeleton; monitoring, treatment-move and release stay out |

---

## 7. Explicitly out of scope

Deferred to the full implementation, unchanged by this migration:

- Real symbolic engines (OPA / Z3 / Prolog / Datalog) — `verify_*` stay deterministic stubs
- Real BERT/NER urgency scorer
- Waiting Room Monitor timers, treatment-move execution state machine, release
- The board (read-model projection)
- Production checkpointer (Postgres) and deployment

---

## 8. Open decisions

1. **Directory rename** `crewAi-app/` → `triage-app/` — recommended, but it touches
   two packages. Say the word if you would rather keep the path and only fix the docs.
2. **Phase 6 in this pass or its own?** It is the one phase that is new architecture
   rather than migration. Recommended: include it — the graph makes it cheap, and
   leaving `move_authorized()` uncalled repeats the mistake the rename made.

---

# Outcome — executed 2026-09-10

Branch `feat/langgraph-migration`. Baseline 30 tests → **146 passing**
(triage-app 92, intake-channel 39, crm-stub 15). Fast suite went from 55.8s to
40s despite tripling in size; the old time was mostly importing CrewAI.
Locked dependencies: **154 → 67**.

## Deviations from the plan above

| Planned | Actual | Why |
| --- | --- | --- |
| Rename last (Phase 10) | Rename **first** | Every later edit would otherwise have landed in a directory about to move. |
| Phases 1-3 separate | Merged | Phase 1's edits were in files Phase 3 rewrote. |
| Add `id` to `TriageState` | Not added | That was a CrewAI `@persist` requirement. LangGraph keys on `thread_id` in the invoke config. |
| Keep `mock_data.py` | **Deleted** | It was live only because `intake-channel` imported `MOCK_PARSE_RESULTS`. Wiring the UI to the graph removed the last caller. |
| Keep `observability.py` as-is | Rewritten | Still used by the UI, but the import-time client construction is gone and it now serves the Langfuse callback handler. |

## Transition-table findings

Reported rather than silently patched, per instruction.

**T1 — Three spec states were absent from the implementation.** `data_parsed`,
`acuity_proposed` and `verdict_proposed` are on-entry pass-through states in the
Transitions table. The CrewAI Flow collapsed all three: `parsing` went straight to
`resolving_identity`, and `safety_validating` straight to `monitoring`. All three
are now real nodes, so the audit trail shows arrow 4 before 4b and arrow 10 before
11·pass, as the table says.

**T2 — Arrow 11 is unreachable, and the prose contradicts the table.**
Row 476: `verdict_proposed --[escalation_needed]--> awaiting_human_approval`.
`escalation_needed` is defined (line 356) as *"verdict fail ∨ low confidence ∨
policy hit"*. But `verdict_proposed` is only reachable via arrow 10, which
requires `safety_pass` — so "verdict fail" is false by construction there. Of the
remaining disjuncts, `confidence_ok` is marked *"Optional: wire in … or drop"*
with its threshold "(to confirm)", and "policy hit" has no definition anywhere.
With neither wired, **arrow 11 can never fire**.

Separately, line 711 states the safety-validation failure path is *"arrows 10·fail
and 11 via `escalation_needed`"* — but a failed verdict takes 10·fail and never
reaches `verdict_proposed`, so arrow 11 cannot carry it. Table and prose disagree.

Left as-is in code: `route_verdict` always returns `CLEARED`, the escalate branch
stays wired, and the docstring records why. Wiring a confidence threshold later is
a one-line change. **Needs a spec decision**, not a code fix.

**T3 — The safety-correction loop was unbounded in the spec's table.** Arrow
1b.z·safety routes the gate back to `safety_validating`, which can fail again and
return to the gate. `SYSTEM_MODELING.md` § 188 / § 229 requires a finite number of
correction rounds and flags the number as a third undefined budget. The table
itself carries no such guard. Implemented as `MAX_CORRECTION_ROUNDS` in
`budgets.py` with the same "documented unknown" treatment as the retry budgets.
On exhaustion the case is held at the gate — the spec says it "escalates" but does
not say to whom, so it is not routed onward.

**T4 — `order_key` at intake contradicted the guards.** Row 1a assigns `order_key`
at `intake_received`, but the bucket needs an acuity and the only one available
there is the nurse's. The old code did `nurse_proposed_acuity or 5`, silently
inventing an ESI level — directly against `guards/fields.py`, which states
`nurse_proposed_acuity` "is mandatory and never inferred: absent means arrow 16,
never a guessed value". Now: no acuity, no `order_key`. A case without one is
heading for `missing_fields_requested` and does not need a queue position.
Covered by `test_missing_fields_case_gets_no_invented_queue_position`.

**T5 — `acuity_source` cannot serve both of its stated purposes.** Line 560
defines it as the provenance of the *final* acuity, and line 398 has
`auto_resolve_acuity_upward` overwrite it with `auto_resolved` — so a red-flag
forced emergent loses its `rule_forced` marking at arrow 9b. But line 639 wants
that fact retained "so red-flag firing rates can be tuned". One single-valued
field cannot do both. Added `red_flag_fired: bool` alongside it; `acuity_source`
keeps its documented meaning unchanged.

**T6 — "Retry budgets shared with crash retries" is not implementable as written.**
Line 504 says a verification retry consumes "its existing `retry_budget_left(agent)`,
shared with crash retries". LangGraph's `RetryPolicy` counts attempts internally and
discards state writes from a failed attempt, so a crash retry cannot increment a
counter held in graph state. Implemented as two mechanisms bounded by the same `N`:
`RetryPolicy(max_attempts=N+1)` for transport exceptions, and a self-loop edge plus
`retry_count[agent]` for malformed output. Literally sharing one counter would mean
giving up LangGraph's backoff and jitter. Flagged rather than resolved.

**T7 — Also fixed in passing:** the old `classifying` step logged
`V·exhausted·classifier` on the *first* verification failure, having attempted no
retry at all; and `resolve_acuity` returned `-1` for the 9c case, a sentinel
indistinguishable from an ESI level downstream. It now returns `None`.

## Verified live

With `crm-stub` and `intake-channel` running, through the browser's own endpoints:

```
clean,  CRM up   1a 2 3 4 4b·found 5 6 8 9b 10 11·pass      → monitoring, acuity 2
clean,  CRM down 1a 2 3 4 AF·db    5 6 8 9b 10 11·pass      → monitoring, degraded=[crm]
gap              1a 2 3 4 4b·found 5 6 8 9c                  → paused, acuity None
  resolved       … 9c 1b.z·acuity 10 11·pass                 → monitoring, human_confirmed
  refused        … 9c BLK BLK                                → action_denied, nothing changed
```

The resolved-gate trail shows `1b.z·acuity` **before** `10`: safety genuinely
re-runs after a human resolution. Correct-and-revalidate, no override path.

## Still open

- T2 needs a spec decision (wire `confidence_ok`, define "policy hit", or drop arrow 11).
- Real symbolic engines, real BERT scorer, real model (`TRIAGE_LLM=live` is wired
  and untested against a live key).
- World plane: `reassessment_required` and `case_closed` are declared in
  `UNIMPLEMENTED_STATES`; `test_edges.py` fails if that set drifts.

---

# Follow-up — arrow 11 resolved 2026-09-10

**The arrows are invoke/propose pairs, one pair per agent.** Not a spec bug — a
notation collision. The Actors-table agents each get two arrows: the odd one
calls the agent, the even one records what it proposed back.

```
3 / 4      Intake Parser        (4 splits into 16 / 17 / 18 by outcome)
7 / 8      Acuity Classifier
9a-c / 10  Safety Validation
11 / 12    Human Escalation
13 / 14    Waiting Room Monitor
```

Three details in the table confirm it: arrow 11's action is literally
`invoke_human_escalation`; arrow 12 is a self-loop on `awaiting_human_approval`
("escalation recorded"), which is meaningless as a state transition and exact as
an agent return; and `11·pass` is a suffix, which only parses if 11 is the call.
Arrows that are neither (4b, 5, 6, 20x) are on-entry actions or notifications the
Actions column names — the CRM and the normalizer are not agents.

## What that resolves

**T2 is withdrawn.** `escalation_needed` = "verdict fail ∨ low confidence ∨
policy hit" is a *cross-cutting* condition naming every reason to invoke the
Human Escalation agent, not a guard local to `verdict_proposed`. Its disjuncts
fire in different places: "verdict fail" on the 10·fail edge, "low confidence" at
`verdict_proposed`, "policy hit" nowhere (still undefined). Read that way the
definition is complete and line 711 is consistent — 10·fail is the state
transition, arrow 11 is the agent call riding on it.

Only the confidence disjunct had no home. Now wired:
`CONFIDENCE_THRESHOLD = 0.70` in `budgets.py`, flagged as a placeholder like the
retry budgets, skipped during a classifier outage per the Guards table's "not
applicable" note.

## Missing arrows, now emitted

The invoke halves were absent from the trail: **4b, 7, 11, 12, 13**. The routing
was right; the trace was half the story. A clean run now reads:

```
1a 2 3 4 4b (4b·found|AF·db) 5 6 7 8 9b 10 — 11·pass 13
```

## Three bugs the new tests caught

**A crashing actor killed the run.** `RetryPolicy` retries and then re-raises;
the `error_handler=` from the plan was never wired. So the AF·* rows only covered
an actor that *returned* something unusable, never one that crashed — which is
the case the failure model is actually written about. Handlers now route
`classifying`, `safety_validating` and `resolving_identity` to their degrade
paths. Two LangGraph details worth recording: the `error: NodeError` parameter is
injected **by type annotation**, not position, and `Command(goto=...)` may only
name a destination already declared for the failing node — hence handing over to
the existing fallback nodes rather than jumping to their targets.

**The low-confidence gate looped.** Resolving it does not change `confidence`, so
`verdict_proposed` re-escalated the same case forever. Guarded on
`human_decision`, which only the gate writes.

**`acuity_source == human_confirmed` does not mean a human was consulted.**
Arrow 9a sets it when the nurse and the system merely agree. The first version of
the loop guard tested it and suppressed the gate for exactly the cases that never
reached one. Third overloaded-field finding, alongside T5 (`acuity_source` vs the
red-flag fact) and T1.

Also cleared: `UrgencyScores` and `SafetyVerdict` were being checkpointed as
Pydantic instances, which LangGraph warns will be blocked in a future version.
Stored as dicts; the model re-validates them on read.

**Tests 92 → 104** in triage-app (143 with intake-channel).

## Still open

- "policy hit" remains undefined; it is an `or` on one line in `route_verdict`
  when it gains a definition.
- The confidence threshold is a placeholder, not a measured value.
