# CRM stub (SQLite)

A local stand-in for the patient-history CRM that Triage Guard reads from. It is
not a real external system — it is a SQLite database with mock records, behind
the same contract described in the main `SPECIFICATION.md` ("Local CRM stub").
Because the contract is identical, it can be replaced by a real CRM later
without changing the rest of the architecture.

Managed with [uv](https://docs.astral.sh/uv/); runnable directly or via Docker
Compose.

## Contract

Three operations, matching the spec:

| Operation                            | Returns                                    | Notes                                                      |
| ------------------------------------ | ------------------------------------------ | ---------------------------------------------------------- |
| `fetch_patient_data(id)`             | `found(record)` / `not_found` / `db_error` | `not_found` is a normal empty (new patient), not a failure |
| `patch_patient_data(id, visit_data)` | `ok` / `db_error`                          | write-back of the current visit                            |
| `is_available()`                     | `true` / `false`                           | health check for degrade decisions                         |

`db_error` is the only outcome that trips the CRM fail-open path; `found` and
`not_found` both mean the DB is reachable.

## Layout

```
crm/
  models.py      # PatientRecord, FetchResult/PatchResult, status enums
  repository.py  # SQLite implementation of the three operations
  seed.py        # 20 varied mock patients
  api.py         # FastAPI wrapper exposing the contract over HTTP
tests/
  test_repository.py
  test_api.py
pyproject.toml         # uv project + dependencies
docker-compose.yml     # service on the official uv image + healthcheck
```

## Run with uv (local)

```bash
uv sync      # create the env and install deps
uv run crm   # seeds patients.db on first run, then serves on :8000
```

`CRM_DB_PATH`, `CRM_HOST` and `CRM_PORT` override the defaults
(`patients.db`, `127.0.0.1`, `8000`).

For reload during development, or to seed on its own:

```bash
uv run uvicorn crm.api:app --reload
uv run crm-seed --db patients.db --reset
```

## Run with Docker Compose

```bash
docker compose up
```

No Dockerfile: the service runs the official uv image with this directory
bind-mounted and runs the same `uv run crm` entry point, so it installs deps,
seeds `patients.db` if empty, and serves the API on `localhost:8000`. The DB is
a normal file in this directory,
so it persists across restarts and is shared with local `uv run`. The `/health`
endpoint backs the container healthcheck.

## Endpoints

- `GET  /patients/{id}` — `200` found, `404` not_found, `503` db_error
- `PATCH /patients/{id}` — write-back; `200` ok, `503` db_error
- `GET  /health` — `{ "available": true|false }`
- `POST /admin/simulate-down?enabled=true|false` — toggle the outage at runtime

## Simulating a DB outage

The `db_error` outcome is driven by an env flag, so you can exercise the
fail-open degrade path without a real outage.

Local:

```bash
CRM_SIMULATE_DOWN=true uv run uvicorn crm.api:app
```

Docker Compose — set it in `docker-compose.yml` (`CRM_SIMULATE_DOWN: "true"`) and
restart, or toggle at runtime for a demo:

```bash
curl -X POST "localhost:8000/admin/simulate-down?enabled=true"
```

## Tests

```bash
uv run tests            # extra args pass through, e.g. uv run tests -k api
```

Covers the three fetch outcomes, write-back (append and upsert), the db-error
simulation via both the constructor flag and the env flag, and the HTTP
status-code mapping.
