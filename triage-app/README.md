# triage-app

Triage Guard's control plane, implemented as a **LangGraph `StateGraph`** — a
deterministic workflow whose allowed transitions are declared as data, with
exactly one LLM call. See [app/README.md](app/README.md) for the architecture,
the deterministic/LLM split, and the full layout.

```
triage-app/
├── app/
│   ├── graph/              the StateGraph: build (the transition table), nodes, routers, state
│   ├── actors/             one module per participant; mocks/ holds canned output
│   ├── guards/             deterministic intake guards
│   ├── schemas/            Pydantic payload schemas
│   ├── states.py           control-plane vocabulary
│   ├── events.py           events / intake outcomes
│   ├── labels.py           spec arrows + internal route labels
│   ├── budgets.py          retry budgets N
│   ├── verification.py     output verification
│   ├── deterministic.py    ordering, acuity bands, audit, OPA predicates
│   ├── runner.py           start / resume / snapshot a case
│   └── main.py             entrypoint
├── tests/                  92 tests, offline
└── pyproject.toml          its own uv project, like crm-stub/ and intake-channel/
```

## Run

```bash
uv sync
cp .env.example .env        # optional — mock mode needs nothing in it

uv run triage-guard         # clean / missing / failed / injection
uv run pytest
uv run plot                 # regenerate docs/diagrams/control-plane.mmd
```

To drive it from the browser instead, run `intake-channel/` — it submits through
this graph and can resolve the human gate.
