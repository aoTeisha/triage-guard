# crewAi-app

A crewAI skeleton, one file per agent. This is skeleton only — no implementation yet, that comes later. Nothing calls an LLM: in mock mode each agent returns canned output and records one step in Langfuse, so you can see the shape of the trace before wiring in any reasoning.

```
crewAi-app/
├── app/
│   ├── crew.jsonc          # wiring: agents, tasks, hierarchical process
│   ├── agents/             # one .jsonc per agent
│   │   ├── orchestrator.jsonc
│   │   ├── intake_parser.jsonc
│   │   ├── acuity_classifier.jsonc
│   │   └── safety_validator.jsonc
│   ├── schemas/             # one Pydantic model per file, structured task output
│   ├── guards/              # one deterministic guard concern per file
│   ├── mock_data.py        # demo case 1 + canned agent outputs
│   ├── observability.py    # Langfuse client + agent_span()
│   └── main.py             # entrypoint
├── tests/
└── pyproject.toml          # its own uv project, separate from intake-channel/ and crm-stub/
```

Z3 / Prolog / OPA tools go in an `app/tools/` folder, created when you write the first
one. One file per tool, referenced from an agent as `"tools": ["custom:<filename>"]`.

## Run

```bash
# from crewAi-app/
cd ../langfuse && docker compose up -d && cd ../crewAi-app   # Langfuse stack, if not already up
cp ../.env.example ../.env                                    # fill in the LANGFUSE_* values, stays at repo root
uv sync
uv run triage-guard
```

This prints the three mock agent outputs and sends a `triage-case` trace to
[http://localhost:3000](http://localhost:3000), with one nested span per agent.

## Adding an agent

See [app/README.md](app/README.md) for a worked example.
