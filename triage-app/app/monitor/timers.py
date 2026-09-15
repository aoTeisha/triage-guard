"""The waiting-room timer store: table schema, scheduling new timers, and the
atomic claim/lease queries the sweeper uses to pick up work safely.

This module only owns the `timers`, `sweeper_heartbeats`, `notifications`,
and `escalations` tables, plus the atomic claim statements that let multiple
sweeper processes race against the same rows without double-claiming one.
It never fires anything itself — actually delivering a timer's event,
handling lost acknowledgments, and de-duplicating fires all live in
`app.monitor.fire` instead.

Uses the same SQLite file as the graph's checkpointer (`app/runner.py`'s
`DB_PATH`): a timer row and the case state it refers to sometimes need to be
written in one transaction, which only works if they're in the same
database. Because of that, this module never opens its own connection —
every function here takes an open `conn` from its caller.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def connection() -> sqlite3.Connection:
    """The shared SQLite connection, opened once per process and reused by
    both the graph's `monitoring` node and the sweeper — the same file the
    graph's checkpointer also writes to.

    Reads the `TRIAGE_CHECKPOINT_DB` env var independently rather than
    importing `app.runner.DB_PATH` directly, because importing `app.runner`
    would pull in the whole graph module just to get a file path.
    """
    db_path = Path(
        os.environ.get("TRIAGE_CHECKPOINT_DB", Path(__file__).resolve().parent.parent.parent / ".triage_state.db")
    )
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
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
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (fire_state, due_at);

CREATE TABLE IF NOT EXISTS sweeper_heartbeats (
  worker_id    TEXT PRIMARY KEY,
  beat_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id          TEXT NOT NULL,
  reason           TEXT NOT NULL,
  channel          TEXT NOT NULL,
  recipient_class  TEXT NOT NULL,
  sent_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (case_id, reason)
);

CREATE TABLE IF NOT EXISTS escalations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id          TEXT NOT NULL,
  fire_id          TEXT NOT NULL,
  raised_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  channel          TEXT NOT NULL,
  recipient_class  TEXT NOT NULL,   -- charge_nurse | technician
  reason           TEXT NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def schedule(conn: sqlite3.Connection, *, case_id: str, kind: str, cycle: int, due_at: str) -> str:
    """Insert a new SCHEDULED timer row. `timer_id` is built as
    `case_id:kind:cycle`.

    Idempotent on `timer_id`: the graph's `monitoring` node calls this every
    time its pause actually resumes, which can happen more than once for the
    same cycle on a replay — so a repeat call must be a no-op rather than
    creating a duplicate row or overwriting an already-scheduled `due_at`.
    """
    timer_id = f"{case_id}:{kind}:{cycle}"
    conn.execute(
        "INSERT OR IGNORE INTO timers (timer_id, case_id, kind, cycle, due_at) VALUES (?, ?, ?, ?, ?)",
        (timer_id, case_id, kind, cycle, due_at),
    )
    conn.commit()
    return timer_id


def set_state(conn: sqlite3.Connection, timer_id: str, fire_state: str, **fields: object) -> None:
    """Move a timer to a new `fire_state`, optionally updating other columns
    (`fire_id`, `attempts`, `lease_until`, `worker_id`, `last_error`) in the
    same write. Only the sweeper's own code (in `app.monitor.fire`) ever
    calls this — the graph itself never writes to the timer store directly.
    """
    columns = ["fire_state", *fields.keys(), "updated_at"]
    placeholders = ["?"] * (1 + len(fields)) + ["strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"]
    conn.execute(
        f"UPDATE timers SET {', '.join(f'{c} = {p}' for c, p in zip(columns, placeholders))} WHERE timer_id = ?",
        (fire_state, *fields.values(), timer_id),
    )
    conn.commit()


def claim_due(conn: sqlite3.Connection, *, worker_id: str, lease_seconds: int) -> list[dict]:
    """Atomically claim every SCHEDULED timer whose `due_at` has passed.

    Uses a single `UPDATE ... RETURNING` statement, so if multiple sweeper
    processes are running at once, they can never double-claim the same row
    — the database itself serializes the update. A row whose previous lease
    has expired (its worker likely crashed) is picked up here exactly like a
    fresh SCHEDULED row, since ownership is decided purely by whether the
    lease has expired, not by which worker claimed it first.
    """
    cur = conn.execute(
        """
        UPDATE timers
           SET fire_state = 'DUE',
               lease_until = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?),
               worker_id = ?,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
         WHERE fire_state = 'SCHEDULED'
           AND due_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
           AND (lease_until IS NULL OR lease_until < strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        RETURNING timer_id, case_id, kind, cycle, due_at
        """,
        (f"+{lease_seconds} seconds", worker_id),
    )
    claimed = [
        dict(zip(("timer_id", "case_id", "kind", "cycle", "due_at"), row))
        for row in cur.fetchall()
    ]
    conn.commit()
    return claimed


def claim_retryable(conn: sqlite3.Connection, *, worker_id: str, lease_seconds: int) -> list[dict]:
    """Atomically claim every `FAILED`, `UNKNOWN`, or lease-expired
    `DISPATCHING` timer — timers that need a redispatch (`FAILED`), a
    reconcile check (`UNKNOWN`), or that crashed mid-dispatch and are being
    treated the same as `UNKNOWN` (a `DISPATCHING` row whose lease expired).
    Same race-safety guarantee as `claim_due`. This function only claims the
    rows; deciding what to actually do with each one is the caller's job
    (`app.monitor.fire`), not this claim query's.
    """
    cur = conn.execute(
        """
        UPDATE timers
           SET lease_until = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?),
               worker_id = ?,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
         WHERE fire_state IN ('FAILED', 'UNKNOWN', 'DISPATCHING')
           AND (lease_until IS NULL OR lease_until < strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        RETURNING timer_id, case_id, kind, cycle, due_at, fire_state, fire_id, attempts
        """,
        (f"+{lease_seconds} seconds", worker_id),
    )
    claimed = [
        dict(zip(("timer_id", "case_id", "kind", "cycle", "due_at", "fire_state", "fire_id", "attempts"), row))
        for row in cur.fetchall()
    ]
    conn.commit()
    return claimed


def record_notification(conn: sqlite3.Connection, *, case_id: str, reason: str, channel: str,
                         recipient_class: str) -> bool:
    """Insert a notification record. Returns `False` if one already exists
    for this exact case+reason combination — so a given reminder step fires
    at most once, even if the sweeper revisits a case's stale timer row
    before it gets cancelled.
    """
    try:
        conn.execute(
            "INSERT INTO notifications (case_id, reason, channel, recipient_class) VALUES (?, ?, ?, ?)",
            (case_id, reason, channel, recipient_class),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def notification_count_in_window(conn: sqlite3.Connection, *, recipient_class: str, window_minutes: int) -> int:
    """How many notifications this recipient class has already received in
    the trailing time window — used by the sweeper to enforce its
    per-recipient notification rate limit and avoid flooding staff.
    """
    return conn.execute(
        """
        SELECT COUNT(*) FROM notifications
         WHERE recipient_class = ?
           AND sent_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
        """,
        (recipient_class, f"-{window_minutes} minutes"),
    ).fetchone()[0]


def record_escalation(conn: sqlite3.Connection, *, case_id: str, fire_id: str, channel: str,
                       recipient_class: str, reason: str) -> None:
    """Evidence that the monitor raised an alert itself, out-of-band."""
    conn.execute(
        "INSERT INTO escalations (case_id, fire_id, channel, recipient_class, reason) VALUES (?, ?, ?, ?, ?)",
        (case_id, fire_id, channel, recipient_class, reason),
    )
    conn.commit()


def heartbeat_status(conn: sqlite3.Connection, *, stale_after_seconds: int) -> dict:
    """Reports whether the sweeper looks alive, based on how recently it last
    beat — checked from outside the sweeper itself, since a process that had
    died couldn't reliably report its own death. No heartbeat row at all
    (the sweeper never started, or died before its first tick) counts as
    degraded, the same as every worker's heartbeat being stale.
    """
    rows = conn.execute(
        """
        SELECT worker_id, beat_at,
               beat_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) AS stale
          FROM sweeper_heartbeats
        """,
        (f"-{stale_after_seconds} seconds",),
    ).fetchall()
    workers = [{"worker_id": w, "beat_at": b, "stale": bool(s)} for w, b, s in rows]
    degraded = not workers or all(w["stale"] for w in workers)
    return {"workers": workers, "degraded": degraded}


def heartbeat(conn: sqlite3.Connection, *, worker_id: str) -> None:
    """Upsert this worker's heartbeat row, so `heartbeat_status` can tell it's
    still alive.
    """
    conn.execute(
        """
        INSERT INTO sweeper_heartbeats (worker_id, beat_at)
        VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT (worker_id) DO UPDATE SET beat_at = excluded.beat_at
        """,
        (worker_id,),
    )
    conn.commit()
