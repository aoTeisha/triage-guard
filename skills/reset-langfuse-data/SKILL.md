---
name: reset-langfuse-data
description: Wipe all traces, observations, scores and sessions from the self-hosted triage-guard Langfuse so it starts empty. Use when asked to reset Langfuse, clear traces, wipe observability data, or get a clean Langfuse for a demo.
---

# Reset Langfuse Data

Destroys every Langfuse volume (postgres, clickhouse, minio, redis) of the
`langfuse` compose project in `langfuse/`, then boots it again. On an empty
database Langfuse re-bootstraps the org, project, admin user and API keys
from `langfuse/.env`, so the keys the app uses keep working and nothing
app-side changes.

Lost for good: all traces, observations, scores, sessions, datasets, prompts,
and anything created by hand in the UI (extra users, LLM connections,
evaluators). Nothing in this repo seeds those, so they don't come back.

## Steps

1. **Precheck `langfuse/.env` exists and has the `LANGFUSE_INIT_*` keys.**
   Without it the stack boots with no project and no API keys, and the app's
   tracing breaks. If missing: `cp langfuse/.env.example langfuse/.env`.
   The `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` values must equal
   `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` in the repo-root `.env`.

2. **Confirm with the user** — even if they invoked this skill by name. Say
   what is lost (list above) and that it can't be undone. Wait for a yes.

3. **Wipe and restart** — run from `langfuse/`, so only the `langfuse`
   compose project is touched. Other stacks such as `langfuse-exmple-*` and
   the app's `triage-guard-postgres` belong to other projects; leave them.
   ```bash
   cd langfuse
   docker compose down -v
   docker compose up -d
   ```

4. **Wait for ready.** Poll until it returns `200` (worker migrations can
   take 30–60 s):
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/api/public/health
   ```

5. **Verify the app's keys work** — expect JSON listing project
   `triage-guard`. `401` means the keys in `langfuse/.env` and the root
   `.env` differ; fix step 1 and repeat step 3.
   ```bash
   # .env has unquoted values with spaces, so grep the keys; don't source it
   pk=$(grep -oP '^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=\K.*' .env)
   sk=$(grep -oP '^LANGFUSE_INIT_PROJECT_SECRET_KEY=\K.*' .env)
   curl -s -u "$pk:$sk" http://localhost:3000/api/public/projects
   ```

6. **Report:** Langfuse is empty, UI at http://localhost:3000 with the same
   login (`admin@local.dev` / `changeme123` unless `.env` says otherwise),
   same API keys. A running app process needs no restart, but traces it
   sent while the stack was down are lost.

## Common Mistakes

| Mistake | Result |
|---|---|
| `docker compose down -v` from the repo root or another dir | Wipes a different compose project, or fails |
| `docker volume prune` / `docker system prune --volumes` | Deletes volumes of every stopped stack on the machine |
| Skipping the `.env` precheck | Fresh stack with no project or keys; tracing returns 401 |
| Calling it done after `up -d` | UI/API not ready yet; first traces may be dropped |
