---
name: reset-board-db
description: Wipe the triage-guard demo data (cases, timers, notifications, escalations) so the board starts fresh. Use when asked to reset the board, wipe demo data, clean the db, or start over with a clean queue.
---

# Reset Board DB

Drops every case, timer, notification and escalation from the `triage`
Postgres database used by `triage-app`, `board`, and `intake-channel`. The
`crm` database (patient records) is never touched — this only resets the
case/checkpoint side.

This is destructive and the DB is shared across every worktree and running
service that points at it (same `triage-guard-postgres` container). Always
confirm with the user before running the drop, even if they invoked this
skill by name — say what will be wiped and wait for a yes.

## Steps

1. **Make sure Postgres is up:**
   ```bash
   docker compose -f db/docker-compose.yml up -d
   ```

2. **Confirm with the user** what's about to happen: every case, timer,
   notification and escalation in the `triage` DB will be dropped;
   `crm` (patient records) is untouched; tables auto-recreate on the next
   run (`PostgresSaver.setup()` in `runner.py`, `timers.init_schema()` in
   `app/monitor/timers.py`), so nothing needs to be started manually
   afterward. Wait for an explicit go-ahead.

3. **Wipe the schema:**
   ```bash
   docker compose -f db/docker-compose.yml exec -T postgres \
     psql -U triage -d triage -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
   ```

4. **Report what happened** — which tables were dropped (the `DROP SCHEMA`
   output lists them via `NOTICE: drop cascades to ...`) — and mention that
   the board/intake-channel/triage-guard CLI can be used immediately; no
   restart is needed since each opens its own connection and the schema is
   recreated automatically on first use.

If any service (board, intake-channel, the sweeper) is running against this
DB while you wipe it, its next read will see empty tables and its next
write will recreate them — no crash, but any in-flight case is gone. Mention
this if you know something is running.
