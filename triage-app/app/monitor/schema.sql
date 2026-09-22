CREATE TABLE IF NOT EXISTS timers (
  timer_id            TEXT PRIMARY KEY,
  case_id             TEXT NOT NULL,
  kind                TEXT NOT NULL,   -- reassessment | gate_reminder | safety_park | reassessment_reminder
  -- Which scheduling of this case+kind this row is. Supplied by the caller and
  -- baked into `timer_id`, so a later scheduling gets its own row instead of
  -- colliding with the previous one. Not a retry count and never bounded.
  schedule_seq        INTEGER NOT NULL,
  due_at              TIMESTAMPTZ NOT NULL,
  fire_state          TEXT NOT NULL DEFAULT 'SCHEDULED',
  fire_id             TEXT,
  -- How many times reconciliation has tried to work out whether an UNKNOWN
  -- fire actually landed. Bounded by RECONCILE_BUDGET; spending it escalates
  -- to a human. Dispatch failures do not touch this.
  reconcile_attempts  INTEGER NOT NULL DEFAULT 0,
  -- While a sweeper holds this row it sets this a little into the future, and
  -- the claim queries skip rows whose lock has not expired. A sweeper that
  -- dies never releases it; the lock just runs out and another sweeper takes
  -- the row. `due_at` above is the deadline; this is only the lock.
  locked_until        TIMESTAMPTZ,
  worker_id           TEXT,            -- which sweeper holds (or last held) the lock
  last_error          TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS timers_due ON timers (fire_state, due_at);

-- Column renames (2026-09-22): the old names read as things they were not —
-- `cycle`/`attempts` looked like two counters of the same thing, `lease_until`
-- like a deadline, `decision` like the charge nurse's gate decision. Postgres
-- has no RENAME COLUMN IF EXISTS, so rename only while the old name is still
-- there. Safe to re-run; a fresh database matches nothing and falls through.
DO $$
DECLARE pair text[];
BEGIN
  FOREACH pair SLICE 1 IN ARRAY ARRAY[
    ARRAY['cycle', 'schedule_seq'],
    ARRAY['attempts', 'reconcile_attempts'],
    ARRAY['lease_until', 'locked_until'],
    ARRAY['decision', 'chosen_action']
  ] LOOP
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'timers'
                  AND column_name = pair[1]) THEN
      EXECUTE format('ALTER TABLE timers RENAME COLUMN %I TO %I', pair[1], pair[2]);
    END IF;
  END LOOP;
END $$;

-- Which action the symbolic layers picked for this timer's latest claim, and
-- what they overrode to get there, e.g.
-- "RECONCILE (proposed DISPATCH: dispatch: blind_redispatch_from_unknown)".
-- Must stay below the rename block: an older database gets this column by
-- rename, and adding it first would leave a second, empty one.
ALTER TABLE timers ADD COLUMN IF NOT EXISTS chosen_action TEXT;

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

-- The board reads both tables newest-first over a time window on every
-- refresh; without these each read is a sequential scan.
CREATE INDEX IF NOT EXISTS notifications_sent_at ON notifications (sent_at DESC);
CREATE INDEX IF NOT EXISTS escalations_raised_at ON escalations (raised_at DESC);
