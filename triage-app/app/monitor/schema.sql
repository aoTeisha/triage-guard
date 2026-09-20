CREATE TABLE IF NOT EXISTS timers (
  timer_id     TEXT PRIMARY KEY,
  case_id      TEXT NOT NULL,
  kind         TEXT NOT NULL,      -- reassessment | gate_reminder | safety_park | reassessment_reminder
  cycle        INTEGER NOT NULL,
  due_at       TIMESTAMPTZ NOT NULL,
  fire_state   TEXT NOT NULL DEFAULT 'SCHEDULED',
  fire_id      TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  lease_until  TIMESTAMPTZ,
  worker_id    TEXT,
  last_error   TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (fire_state, due_at);

-- What the symbolic layers chose for this timer's latest claim, e.g.
-- "RECONCILE (proposed DISPATCH: dispatch: blind_redispatch_from_unknown)".
ALTER TABLE timers ADD COLUMN IF NOT EXISTS decision TEXT;

CREATE TABLE IF NOT EXISTS sweeper_heartbeats (
  worker_id    TEXT PRIMARY KEY,
  beat_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id               SERIAL PRIMARY KEY,
  case_id          TEXT NOT NULL,
  reason           TEXT NOT NULL,
  channel          TEXT NOT NULL,
  recipient_class  TEXT NOT NULL,
  sent_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (case_id, reason)
);

CREATE TABLE IF NOT EXISTS escalations (
  id               SERIAL PRIMARY KEY,
  case_id          TEXT NOT NULL,
  fire_id          TEXT NOT NULL,
  raised_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  channel          TEXT NOT NULL,
  recipient_class  TEXT NOT NULL,   -- charge_nurse | technician
  reason           TEXT NOT NULL
);
