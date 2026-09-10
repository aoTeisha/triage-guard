# crewAi-app

Triage Guard, implemented as a CrewAI `Flow` — a deterministic workflow whose
steps are wired in code, with exactly one LLM call. See
[app/README.md](app/README.md) for the architecture, the deterministic/LLM
split, and the full layout.

```
crewAi-app/
├── app/
│   ├── agents/             # one .jsonc per actor in the spec's table
│   │   └── mocks/          # one canned-output .json per actor that mocks one
│   ├── flow/                # the Flow: state, steps, deterministic logic
│   ├── schemas/             # Pydantic task-output schemas (legacy, unused)
│   ├── guards/              # one deterministic guard concern per file
│   ├── crm_client.py        # CRM SQLite-stub client
│   ├── observability.py     # Langfuse client + agent_span()
│   ├── mock_cases.py        # the four editable demo inputs
│   └── main.py               # entrypoint
├── tests/
└── pyproject.toml          # its own uv project, separate from intake-channel/ and crm-stub/
```

## Run

```bash
uv sync
uv run triage-guard            # clean demo case
uv run triage-guard missing    # or: missing / failed / injection
uv run tests
```
