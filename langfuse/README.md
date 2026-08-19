# Langfuse + crewAI hello world

Self-hosted Langfuse (docker) + a minimal crewAI agent that sends a trace to it.

## Run Langfuse

```bash
cp .env.example .env
docker compose up -d
```

Web UI: http://localhost:3000 — login `admin@local.dev` / `changeme123` (auto-bootstrapped via `.env`).

## Run the agent

```bash
cd agent-example
cp .env.example .env
uv run main.py
```

Prints a canned "Hello, World!" (no real LLM call) and sends the trace to Langfuse — check the UI for a `hello-world-crew` span.

## Files

- `docker-compose.yml` — official Langfuse v4 self-host stack (postgres, clickhouse, redis, minio, web, worker)
- `.env.example` — template, copy to `.env` before first boot
- `.env` — bootstraps org/project/user/API keys on first boot
- `agent-example/main.py` — crewAI `Agent`/`Task`/`Crew` shape, mock output, manual Langfuse span
- `agent-example/pyproject.toml` — uv project (crewai, langfuse, python-dotenv)
- `agent-example/.env.example` — template, copy to `.env` before first run
- `agent-example/.env` — Langfuse keys, must match this folder's `.env`
