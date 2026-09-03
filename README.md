# Triage Guard

Under-triage in hospital waiting rooms gets caught too late, and patients deteriorate
without being reassessed. Triage Guard helps staff identify how far a patient has
deteriorated, which improves ER capacity logistics and treatment.

**Docs:**

- [Architecture and State-Machine Specification](docs/SPECIFICATION.md)
- [System Modeling](docs/SYSTEM_MODELING.md)

## The Triage guard app

A crewAI skeleton, one file per agent. This is skeleton only — no implementation yet, that comes later. Nothing calls an LLM: in mock mode each agent returns canned output and records one step in Langfuse, so you can see the shape of the trace before wiring in any reasoning.

```
app/
├── crew.jsonc          # wiring: agents, tasks, hierarchical process
├── agents/             # one .jsonc per agent
│   ├── orchestrator.jsonc      s
│   ├── intake_parser.jsonc
│   ├── acuity_classifier.jsonc
│   └── safety_validator.jsonc
├── schemas.py          # Pydantic models for structured task output
├── mock_data.py        # demo case 1 + canned agent outputs
├── observability.py    # Langfuse client + agent_span()
└── main.py             # entrypoint
```

Z3 / Prolog / OPA tools go in an `app/tools/` folder, created when you write the first
one. One file per tool, referenced from an agent as `"tools": ["custom:<filename>"]`.

### Run

```bash
cd langfuse && docker compose up -d && cd ..   # Langfuse stack, if not already up
cp .env.example .env                            # fill in the LANGFUSE_* values
uv sync
uv run triage-guard
```

This prints the three mock agent outputs and sends a `triage-case` trace to
[http://localhost:3000](http://localhost:3000), with one nested span per agent.

### Adding an agent

See [app/README.md](app/README.md) for a worked example.

## The CRM stub

A local stand-in for the patient-history CRM the crew reads from: a SQLite DB of
20 mock patients behind the exact contract in the spec, wrapped in FastAPI. Not a
real external system — but because the contract matches, a real CRM can replace
it without touching the rest of the architecture.

```
crm-stub/
├── crm/
│   ├── models.py       # PatientRecord, FetchResult/PatchResult, status enums
│   ├── repository.py   # SQLite implementation of the three operations
│   ├── seed.py         # 20 varied mock patients
│   └── api.py          # FastAPI wrapper + `uv run crm` entrypoint
├── tests/
├── pyproject.toml      # its own uv project, separate from the app
└── docker-compose.yml  # run the crm from docker
```

| Operation | Endpoint | Returns |
|---|---|---|
| `fetch_patient_data(id)` | `GET /patients/{id}` | `200` found / `404` not_found / `503` db_error |
| `patch_patient_data(id, visit)` | `PATCH /patients/{id}` | `200` ok / `503` db_error |
| `is_available()` | `GET /health` | `{"available": true\|false}` |

`db_error` is the only outcome that trips the CRM fail-open path; `not_found` is
a normal empty (new patient), not a failure. `POST /admin/simulate-down?enabled=true`
forces `db_error` at runtime, so the degrade path can be demoed without a real outage.

### Run

```bash
cd crm-stub
uv sync
uv run crm        # seeds patients.db on first run, then serves on :8000
```

Or in a container, same entry point: `cd crm-stub && docker compose up`.
Interactive API docs at [http://localhost:8000/docs](http://localhost:8000/docs).

See [crm-stub/README.md](crm-stub/README.md) for the full details.

## The intake channel

A standalone nurse-facing intake form, standing in for the real website intake
described in the spec.it's a small HTTP service like the
CRM stub: look up a patient, pick one of four mock submission types (clean /
missing / failed / injection), submit, and the payload runs through the
`parse_intake` task and shows up as a Langfuse span.

### Run

```bash
cd intake-channel
uv sync
uv run intake-channel   # serves on :8001, needs the CRM stub on :8000
```

See [intake-channel/README.md](intake-channel/README.md) for the full details.
