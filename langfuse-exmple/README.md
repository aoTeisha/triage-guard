# Langfuse + crewAI hello world

Self-hosted Langfuse (docker) + a minimal crewAI agent that sends a trace to it.

## Run Langfuse

```bash
docker compose up -d
```

Web UI: http://localhost:3000 — login `admin@local.dev` / `changeme123` (auto-bootstrapped via `.env`).

## Run the agent

```bash
cd agent
uv run main.py
```

Prints a canned "Hello, World!" (no real LLM call) and sends the trace to Langfuse — check the UI for a `hello-world-crew` span.

## Files

- `docker-compose.yml` — official Langfuse v4 self-host stack (postgres, clickhouse, redis, minio, web, worker)
- `.env` — bootstraps org/project/user/API keys on first boot
- `agent/main.py` — crewAI `Agent`/`Task`/`Crew` shape, mock output, manual Langfuse span
- `agent/pyproject.toml` — uv project (crewai, langfuse, python-dotenv)
- `agent/.env` — Langfuse keys, must match this folder's `.env`
