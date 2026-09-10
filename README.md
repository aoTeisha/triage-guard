# Triage Guard

Under-triage in hospital waiting rooms gets caught too late, and patients deteriorate
without being reassessed. Triage Guard helps staff identify how far a patient has
deteriorated, which improves ER capacity logistics and treatment.

**Docs:**

- [Architecture and State-Machine Specification](docs/SPECIFICATION.md)
- [System Modeling](docs/SYSTEM_MODELING.md)

## The Triage guard app

A **LangGraph** skeleton: the Transitions table from `docs/SPECIFICATION.md`
transcribed into a `StateGraph`, so the set of allowed moves is declared data the
framework validates and can draw. Skeleton only — the actors return canned output
and the symbolic engines are stubs — but nothing is faked away to make it run: the
human gate really suspends the case to a checkpoint, retry budgets really count,
and an unauthorized action is really refused (`BLK`) without moving the case.

Eleven of the twelve actors are deterministic code, humans, or data stores. The
one generative step, the Acuity Classifier, is mocked by default and needs no key.

### Run

```bash
cd langfuse && docker compose up -d && cd ..   # Langfuse stack, if not already up
cp triage-app/.env.example triage-app/.env      # optional — mock mode needs nothing in it
cd triage-app
uv sync
uv run triage-guard            # clean / missing / failed / injection
uv run pytest                  # 92 tests, offline
```

Each run prints the final state and the full audit trail, labelled with the arrows
from the Transitions table. With Langfuse configured it also sends one trace per
run, with a span per node.

To drive it from a browser instead — including pausing at the acuity gate and
resolving it as a charge nurse — run `intake-channel/`.

See [triage-app/README.md](triage-app/README.md) for the full details, and
[docs/plans/2026-09-10-langgraph-migration-design.md](docs/plans/2026-09-10-langgraph-migration-design.md)
for why this is LangGraph and not CrewAI.

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
