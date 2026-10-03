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
docker compose -f ../db/docker-compose.yml up -d postgres   # starts the shared Postgres
uv sync                         # create the env and install deps
uv run crm                      # seeds the `crm` database on first run, then serves on :8000
```

`CRM_DATABASE_URL`, `CRM_HOST` and `CRM_PORT` override the defaults
(`postgresql://triage:triage@localhost:5434/crm`, `127.0.0.1`, `8000`).

For reload during development, or to seed on its own:

```bash
uv run uvicorn crm.api:app --reload
uv run crm-seed --reset
```

## Run with Docker Compose

```bash
docker compose up
```

No Dockerfile: the service runs the official uv image with this directory
bind-mounted and runs the same `uv run crm` entry point, so it installs deps,
seeds the `crm` database if empty, and serves the API on `localhost:8000`. The
data lives in the shared Postgres (`../db/docker-compose.yml`, its own `crm`
database), so it persists across restarts and is shared with local `uv run` —
start that container first. The `/health` endpoint backs the container
healthcheck.

## Endpoints

- `GET  /patients/{id}` — `200` found, `404` not_found, `503` db_error
- `PATCH /patients/{id}` — write-back; `200` ok, `503` db_error, `422` for a
  `new_visit` that could not be read back (no `date` or `acuity`)
- `GET  /health` — `{ "available": true|false }`
- `POST /admin/simulate-down?enabled=true|false` — toggle the outage at runtime

## A visit in `prior_visits`

```json
{"date": "2026-01-15", "acuity": 2, "notes": "shortness of breath",
 "chief_complaint": "shortness_of_breath",
 "vitals": {"hr": 112, "rr": 26, "bp": "152/94", "spo2": 91, "temp_c": 37.4}}
```

Triage Guard writes one per released case (`PATCH` with `{"new_visit": ...}`).
`chief_complaint` and `vitals` use the names and shapes of its model payload: a
complaint code from its fixed list, and the signs `hr`, `rr`, `bp` ("120/80"),
`spo2`, `temp_c`. A later case for the patient sends them to the model as
history. `notes` is words for the staff reading the record and never reaches the
model. Visits written before the two fields existed, like most of the seed, have
only `date`, `acuity` and `notes`, and read back with `chief_complaint` and
`vitals` as `null`. The seed gives a few later visits both, so the demo shows them.

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

Covers the three fetch outcomes, write-back (append and upsert, with and without
a visit's complaint and vitals), the db-error
simulation via both the constructor flag and the env flag, and the HTTP
status-code mapping.
