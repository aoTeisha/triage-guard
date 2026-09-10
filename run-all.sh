#!/usr/bin/env bash
# Boots the whole stack: langfuse (optional, best-effort), crm-stub, intake-channel.
# Ctrl+C stops everything.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if command -v docker >/dev/null 2>&1 && [ -f langfuse/docker-compose.yml ]; then
    echo "== langfuse (background) =="
    (cd langfuse && docker compose up -d) || echo "  skipped: langfuse failed to start"
fi

echo "== crm-stub :8000 =="
(cd crm-stub && uv run crm) &
CRM_PID=$!
trap 'kill "$CRM_PID" 2>/dev/null' EXIT

for i in $(seq 1 20); do
    curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && break
    sleep 0.5
done

echo "== intake-channel :8001 (Ctrl+C to stop everything) =="
(cd intake-channel && uv run intake-channel)
