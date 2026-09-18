# db

Local Postgres for triage-guard, run via Docker Compose.

## Start

```bash
docker compose -f db/docker-compose.yml up -d
```

## Connection (pgAdmin / psql)

| Field | Value |
|---|---|
| Host | `localhost` |
| Port | `5434` |
| Maintenance DB | `triage` |
| Username | `triage` |
| Password | `triage` |

Host port is 5434, not 5432 — native `postgresql.service` already holds 5432 on this machine.

Databases: `triage` (created by image) and `crm` (created by `init/` scripts on first boot).

Override user/password via `POSTGRES_USER` / `POSTGRES_PASSWORD` env vars before `up`.

## "database crm does not exist"

`init/` only runs when the volume is created — never on an existing one. If
you already had a `triage_postgres_data` volume from before `crm` was added
(or from before this `db/` layout existed), recreate the volume:

```bash
docker compose -f db/docker-compose.yml down -v   # -v drops the volume, wipes local data
docker compose -f db/docker-compose.yml up -d
```
