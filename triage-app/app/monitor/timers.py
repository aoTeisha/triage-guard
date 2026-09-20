"""Timer store: schedule timers and let the sweeper claim them safely.

Owns `timers`, `sweeper_heartbeats`, `notifications`, and `escalations`.
Does not fire events — that lives in `app.monitor.fire`.

Uses the same Postgres DB as the graph checkpointer (`TRIAGE_CHECKPOINT_DB`).
Callers pass in an open `conn` so tests can use their own database.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


@lru_cache(maxsize=1)
def connection() -> psycopg.Connection:
    """One Postgres connection for this process. Reused on later calls."""
    # autocommit: each write is one statement; no multi-statement txn needed.
    # Without it, read helpers would leave this connection idle in a txn.
    #
    # lock_timeout: fail in 5s so a stuck lock does not hang the board UI.
    dsn = os.environ.get("TRIAGE_CHECKPOINT_DB", "postgresql://triage:triage@localhost:5434/triage")
    conn = psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=5s")
    init_schema(conn)
    return conn

def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(Path(__file__).with_name("schema.sql").read_text())


def due_in(minutes: int) -> datetime:
    """UTC time `minutes` from now, for `schedule(..., due_at=...)`."""
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def schedule(conn: psycopg.Connection, *, case_id: str, kind: str, cycle: int, due_at: datetime | str) -> str:
    """Insert a SCHEDULED timer. Id is `case_id:kind:cycle`. Repeat calls are a no-op."""
    timer_id = f"{case_id}:{kind}:{cycle}"
    conn.execute(
        "INSERT INTO timers (timer_id, case_id, kind, cycle, due_at) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (timer_id) DO NOTHING",
        (timer_id, case_id, kind, cycle, due_at),
    )
    return timer_id


def set_state(conn: psycopg.Connection, timer_id: str, fire_state: str, **fields: object) -> None:
    """Set `fire_state` and any extra columns (`fire_id`, `attempts`, …) in one write."""
    assignments = ", ".join(f"{c} = %s" for c in ("fire_state", *fields))
    conn.execute(
        f"UPDATE timers SET {assignments}, updated_at = now() WHERE timer_id = %s",
        (fire_state, *fields.values(), timer_id),
    )


def claim_due(conn: psycopg.Connection, *, worker_id: str, lease_seconds: int) -> list[dict]:
    """Claim every due SCHEDULED timer. Safe if several sweepers run at once."""
    # One UPDATE ... RETURNING: Postgres serializes it, so two sweepers never take the same row.
    return conn.cursor(row_factory=dict_row).execute(
        """
        UPDATE timers
           SET fire_state = 'DUE',
               lease_until = now() + %s * INTERVAL '1 second',
               worker_id = %s,
               updated_at = now()
         WHERE fire_state = 'SCHEDULED'
           AND due_at <= now()
           AND (lease_until IS NULL OR lease_until < now())
        RETURNING timer_id, case_id, kind, cycle, due_at
        """,
        (lease_seconds, worker_id),
    ).fetchall()


def claim_retryable(conn: psycopg.Connection, *, worker_id: str, lease_seconds: int) -> list[dict]:
    """Claim FAILED, UNKNOWN, or lease-expired DISPATCHING timers. Caller decides what to do next."""
    return conn.cursor(row_factory=dict_row).execute(
        """
        UPDATE timers
           SET lease_until = now() + %s * INTERVAL '1 second',
               worker_id = %s,
               updated_at = now()
         WHERE fire_state IN ('FAILED', 'UNKNOWN', 'DISPATCHING')
           AND (lease_until IS NULL OR lease_until < now())
        RETURNING timer_id, case_id, kind, cycle, due_at, fire_state, fire_id, attempts
        """,
        (lease_seconds, worker_id),
    ).fetchall()


def record_notification(conn: psycopg.Connection, *, case_id: str, reason: str, channel: str,
                         recipient_class: str) -> bool:
    """Save a notification. Returns False if this case+reason was already recorded."""
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
    """How many notifications this recipient class got in the last `window_minutes`."""
    return conn.execute(
        """
        SELECT COUNT(*) FROM notifications
         WHERE recipient_class = %s
           AND sent_at >= now() - %s * INTERVAL '1 minute'
        """,
        (recipient_class, window_minutes),
    ).fetchone()[0]


def record_escalation(conn: psycopg.Connection, *, case_id: str, fire_id: str, channel: str,
                       recipient_class: str, reason: str) -> None:
    """Log that the monitor sent an out-of-band alert."""
    conn.execute(
        "INSERT INTO escalations (case_id, fire_id, channel, recipient_class, reason) VALUES (%s, %s, %s, %s, %s)",
        (case_id, fire_id, channel, recipient_class, reason),
    )


def heartbeat_status(conn: psycopg.Connection, *, stale_after_seconds: int) -> dict:
    """Whether the sweeper looks alive. No heartbeat, or all stale, means degraded."""
    rows = conn.execute(
        """
        SELECT worker_id, beat_at,
               beat_at < now() - %s * INTERVAL '1 second' AS stale
          FROM sweeper_heartbeats
        """,
        (stale_after_seconds,),
    ).fetchall()
    workers = [{"worker_id": w, "beat_at": b, "stale": bool(s)} for w, b, s in rows]
    degraded = not workers or all(w["stale"] for w in workers)
    return {"workers": workers, "degraded": degraded}


def heartbeat(conn: psycopg.Connection, *, worker_id: str) -> None:
    """Mark this worker as still running."""
    conn.execute(
        """
        INSERT INTO sweeper_heartbeats (worker_id, beat_at)
        VALUES (%s, now())
        ON CONFLICT (worker_id) DO UPDATE SET beat_at = excluded.beat_at
        """,
        (worker_id,),
    )
