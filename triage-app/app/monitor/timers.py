"""The waiting-room timer store: table schema, scheduling new timers, and the
atomic claim/lease queries the sweeper uses to pick up work safely.

This module only owns the `timers`, `sweeper_heartbeats`, `notifications`,
and `escalations` tables, plus the atomic claim statements that let multiple
sweeper processes race against the same rows without double-claiming one.
It never fires anything itself — actually delivering a timer's event,
handling lost acknowledgments, and de-duplicating fires all live in
`app.monitor.fire` instead.

Uses the same Postgres database as the graph's checkpointer (`app/runner.py`'s
`DSN`) so a case's timers and its state can be read together in one place,
but not the same connection: the checkpointer owns its own, so the two were
never in one transaction. Every function here takes an open `conn` from its
caller rather than reaching for `connection()` itself, which is what lets the
tests hand each one an isolated database.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import psycopg


@lru_cache(maxsize=1)
def connection() -> psycopg.Connection:
    """This process's connection to the timer store, opened once and reused by
    every caller in it — the same database the graph's checkpointer writes to.

    One per process, not one shared across them: the sweeper runs as its own
    process and opens its own connection (`sweeper.main`), which is what makes
    the atomic claim queries below worth having.

    Reads the `TRIAGE_CHECKPOINT_DB` env var independently rather than
    importing `app.runner.DSN` directly, because importing `app.runner`
    would pull in the whole graph module just to get a connection string.
    """
    # autocommit: every statement below is self-contained (each write is a
    # single INSERT/UPDATE, and a lone statement is atomic in Postgres on its
    # own), so nothing here needs a multi-statement transaction. Without it,
    # psycopg opens a transaction on the first statement and the read-only
    # helpers (`heartbeat_status`, `notification_count_in_window`) would leave
    # this long-lived connection sitting "idle in transaction" forever,
    # holding locks that block the sweeper's own heartbeat write.
    #
    # lock_timeout: the board polls this store every few seconds, so a
    # statement that can block forever on a lock turns a stuck row into a
    # hung UI. Failing fast instead lets the caller's degrade path report a
    # problem, which is the behavior the board is built for.
    dsn = os.environ.get("TRIAGE_CHECKPOINT_DB", "postgresql://triage:triage@localhost:5434/triage")
    conn = psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=5s")
    init_schema(conn)
    return conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS timers (
  timer_id     TEXT PRIMARY KEY,
  case_id      TEXT NOT NULL,
  kind         TEXT NOT NULL,      -- reassessment | gate_reminder | safety_park | reassessment_reminder
  cycle        INTEGER NOT NULL,
  due_at       TEXT NOT NULL,      -- ISO8601 UTC
  fire_state   TEXT NOT NULL DEFAULT 'SCHEDULED',
  fire_id      TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  lease_until  TEXT,
  worker_id    TEXT,
  last_error   TEXT,
  created_at   TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')),
  updated_at   TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'))
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (fire_state, due_at);

CREATE TABLE IF NOT EXISTS sweeper_heartbeats (
  worker_id    TEXT PRIMARY KEY,
  beat_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id               SERIAL PRIMARY KEY,
  case_id          TEXT NOT NULL,
  reason           TEXT NOT NULL,
  channel          TEXT NOT NULL,
  recipient_class  TEXT NOT NULL,
  sent_at          TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')),
  UNIQUE (case_id, reason)
);

CREATE TABLE IF NOT EXISTS escalations (
  id               SERIAL PRIMARY KEY,
  case_id          TEXT NOT NULL,
  fire_id          TEXT NOT NULL,
  raised_at        TEXT NOT NULL DEFAULT (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')),
  channel          TEXT NOT NULL,
  recipient_class  TEXT NOT NULL,   -- charge_nurse | technician
  reason           TEXT NOT NULL
);
"""


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def due_in(minutes: int) -> str:
    """ISO8601 UTC timestamp `minutes` from now — the `due_at` every caller
    of `schedule` needs. Centralized so the three graph nodes that schedule a
    timer (`monitoring`, `awaiting_human_approval`, `reassessment_required`)
    don't each redo the same `datetime.now(timezone.utc) + timedelta(...)`.
    """
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def schedule(conn: psycopg.Connection, *, case_id: str, kind: str, cycle: int, due_at: str) -> str:
    """Insert a new SCHEDULED timer row. `timer_id` is built as
    `case_id:kind:cycle`.

    Idempotent on `timer_id`: the graph's `monitoring` node calls this every
    time its pause actually resumes, which can happen more than once for the
    same cycle on a replay — so a repeat call must be a no-op rather than
    creating a duplicate row or overwriting an already-scheduled `due_at`.
    """
    timer_id = f"{case_id}:{kind}:{cycle}"
    conn.execute(
        "INSERT INTO timers (timer_id, case_id, kind, cycle, due_at) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (timer_id) DO NOTHING",
        (timer_id, case_id, kind, cycle, due_at),
    )
    return timer_id


def set_state(conn: psycopg.Connection, timer_id: str, fire_state: str, **fields: object) -> None:
    """Move a timer to a new `fire_state`, optionally updating other columns
    (`fire_id`, `attempts`, `lease_until`, `worker_id`, `last_error`) in the
    same write. Only the sweeper's own code (in `app.monitor.fire`) ever
    calls this — the graph itself never writes to the timer store directly.
    """
    columns = ["fire_state", *fields.keys(), "updated_at"]
    placeholders = ["%s"] * (1 + len(fields)) + [_now_expr()]
    conn.execute(
        f"UPDATE timers SET {', '.join(f'{c} = {p}' for c, p in zip(columns, placeholders))} WHERE timer_id = %s",
        (fire_state, *fields.values(), timer_id),
    )


def _now_expr() -> str:
    """SQL expression for the current UTC time, formatted the same as the
    ISO8601 strings this store already writes everywhere else.
    """
    return "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')"


def _rows(cur: psycopg.Cursor, *columns: str) -> list[dict]:
    """Cursor rows as dicts keyed by `columns`, in the same order as the
    query's `RETURNING`/`SELECT` list. `claim_due` and `claim_retryable` both
    claim rows this way; psycopg's default row factory only gives back plain
    tuples, so something has to name the columns.
    """
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def claim_due(conn: psycopg.Connection, *, worker_id: str, lease_seconds: int) -> list[dict]:
    """Atomically claim every SCHEDULED timer whose `due_at` has passed.

    Uses a single `UPDATE ... RETURNING` statement, so if multiple sweeper
    processes are running at once, they can never double-claim the same row
    — the database itself serializes the update. A row whose previous lease
    has expired (its worker likely crashed) is picked up here exactly like a
    fresh SCHEDULED row, since ownership is decided purely by whether the
    lease has expired, not by which worker claimed it first.
    """
    cur = conn.execute(
        f"""
        UPDATE timers
           SET fire_state = 'DUE',
               lease_until = to_char((now() AT TIME ZONE 'UTC') + %s * INTERVAL '1 second', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
               worker_id = %s,
               updated_at = {_now_expr()}
         WHERE fire_state = 'SCHEDULED'
           AND due_at <= {_now_expr()}
           AND (lease_until IS NULL OR lease_until < {_now_expr()})
        RETURNING timer_id, case_id, kind, cycle, due_at
        """,
        (lease_seconds, worker_id),
    )
    claimed = _rows(cur, "timer_id", "case_id", "kind", "cycle", "due_at")
    return claimed


def claim_retryable(conn: psycopg.Connection, *, worker_id: str, lease_seconds: int) -> list[dict]:
    """Atomically claim every `FAILED`, `UNKNOWN`, or lease-expired
    `DISPATCHING` timer — timers that need a redispatch (`FAILED`), a
    reconcile check (`UNKNOWN`), or that crashed mid-dispatch and are being
    treated the same as `UNKNOWN` (a `DISPATCHING` row whose lease expired).
    Same race-safety guarantee as `claim_due`. This function only claims the
    rows; deciding what to actually do with each one is the caller's job
    (`app.monitor.fire`), not this claim query's.
    """
    cur = conn.execute(
        f"""
        UPDATE timers
           SET lease_until = to_char((now() AT TIME ZONE 'UTC') + %s * INTERVAL '1 second', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
               worker_id = %s,
               updated_at = {_now_expr()}
         WHERE fire_state IN ('FAILED', 'UNKNOWN', 'DISPATCHING')
           AND (lease_until IS NULL OR lease_until < {_now_expr()})
        RETURNING timer_id, case_id, kind, cycle, due_at, fire_state, fire_id, attempts
        """,
        (lease_seconds, worker_id),
    )
    claimed = _rows(cur, "timer_id", "case_id", "kind", "cycle", "due_at", "fire_state", "fire_id", "attempts")
    return claimed


def record_notification(conn: psycopg.Connection, *, case_id: str, reason: str, channel: str,
                         recipient_class: str) -> bool:
    """Insert a notification record. Returns `False` if one already exists
    for this exact case+reason combination — so a given reminder step fires
    at most once, even if the sweeper revisits a case's stale timer row
    before it gets cancelled.
    """
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO notifications (case_id, reason, channel, recipient_class) VALUES (%s, %s, %s, %s)",
                (case_id, reason, channel, recipient_class),
            )
        return True
    except psycopg.errors.UniqueViolation:
        return False


def notification_count_in_window(conn: psycopg.Connection, *, recipient_class: str, window_minutes: int) -> int:
    """How many notifications this recipient class has already received in
    the trailing time window — used by the sweeper to enforce its
    per-recipient notification rate limit and avoid flooding staff.
    """
    return conn.execute(
        """
        SELECT COUNT(*) FROM notifications
         WHERE recipient_class = %s
           AND sent_at >= to_char((now() AT TIME ZONE 'UTC') - %s * INTERVAL '1 minute', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
        """,
        (recipient_class, window_minutes),
    ).fetchone()[0]


def record_escalation(conn: psycopg.Connection, *, case_id: str, fire_id: str, channel: str,
                       recipient_class: str, reason: str) -> None:
    """Evidence that the monitor raised an alert itself, out-of-band."""
    conn.execute(
        "INSERT INTO escalations (case_id, fire_id, channel, recipient_class, reason) VALUES (%s, %s, %s, %s, %s)",
        (case_id, fire_id, channel, recipient_class, reason),
    )


def heartbeat_status(conn: psycopg.Connection, *, stale_after_seconds: int) -> dict:
    """Reports whether the sweeper looks alive, based on how recently it last
    beat — checked from outside the sweeper itself, since a process that had
    died couldn't reliably report its own death. No heartbeat row at all
    (the sweeper never started, or died before its first tick) counts as
    degraded, the same as every worker's heartbeat being stale.
    """
    rows = conn.execute(
        """
        SELECT worker_id, beat_at,
               beat_at < to_char((now() AT TIME ZONE 'UTC') - %s * INTERVAL '1 second', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS stale
          FROM sweeper_heartbeats
        """,
        (stale_after_seconds,),
    ).fetchall()
    workers = [{"worker_id": w, "beat_at": b, "stale": bool(s)} for w, b, s in rows]
    degraded = not workers or all(w["stale"] for w in workers)
    return {"workers": workers, "degraded": degraded}


def heartbeat(conn: psycopg.Connection, *, worker_id: str) -> None:
    """Upsert this worker's heartbeat row, so `heartbeat_status` can tell it's
    still alive.
    """
    conn.execute(
        f"""
        INSERT INTO sweeper_heartbeats (worker_id, beat_at)
        VALUES (%s, {_now_expr()})
        ON CONFLICT (worker_id) DO UPDATE SET beat_at = excluded.beat_at
        """,
        (worker_id,),
    )
