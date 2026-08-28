# Langfuse (self-hosted)

Observability backend for Triage Guard — traces, logs, and the eval pipeline.
The crew that sends traces here lives in [`../app/`](../app/); see the
[root README](../README.md) to run it.

## Run

```bash
cp .env.example .env
docker compose up -d
```

Web UI: http://localhost:3000 — login `admin@local.dev` / `changeme123`
(auto-bootstrapped via `.env`).

## Files

- `docker-compose.yml` — official Langfuse v4 self-host stack (postgres, clickhouse, redis, minio, web, worker)
- `.env.example` — template, copy to `.env` before first boot
- `.env` — bootstraps the org, project, user and API keys on first boot

## Notes

- **`LANGFUSE_INIT_*` only applies on first boot against an empty database.**
  Editing those values later renames nothing. To re-bootstrap, wipe the volumes:
  `docker compose down -v && docker compose up -d` — this destroys all stored
  traces. The API keys are pinned in `.env`, so they survive and the app's
  `../.env` keeps working.
- The keys in this file must match `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
  in the repo-root `.env`.
- This deployment runs in **v4 events_only mode**: the `/api/public/traces` read
  API is disabled and the legacy `observations` table stays empty. Trace data
  lives in the `events_core` / `events_full` ClickHouse tables. The UI is unaffected.
