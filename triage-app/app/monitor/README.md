# The waiting-room monitor

This explains `app/monitor/` — the background process that watches patients
waiting in the queue and makes sure nobody gets forgotten.

## The problem it solves

A patient gets triaged and put in the queue. Someone has to check on them
again after a while — that's called a "reassessment". Nobody sits there
watching a clock for each patient, so a background process does: the
**monitor**. It also chases up staff who haven't answered an approval
request in time.

## How a case "waits"

When a case needs to wait (for a reassessment, or for a nurse's approval),
it doesn't sit in a loop burning CPU. It saves its exact state to the
database and **pauses** — this uses LangGraph's `interrupt()`. Later, the
monitor (or a nurse) sends an event that **resumes** the case from exactly
where it paused, with new information attached (e.g. "your timer went off"
or "here's the nurse's answer"). This is why the tables below store so
little — the actual case state lives in LangGraph's own checkpoint tables;
the monitor's tables only track the clock.

## The files

- **`timers.py`** — owns the database tables. Schedules timers, lets a
  sweeper safely "claim" one so two sweepers never grab the same row, and
  records what happened.
- **`fire.py`** — "a timer just went off — now what happens?" Gathers the
  facts about the case and timer, asks the decision-making layers what to
  do, then actually does it (dispatch, reconcile, or send a reminder).
- **`bthreads.py`** — the decision-making itself, using a library called
  BPpy (behavioral programming). One thread proposes the obvious action;
  other threads act as safety rules that can override it.
- **`sweeper.py`** — the loop that ties it together. Every 5 seconds it asks
  `timers.py` what's due, hands each one to `fire.py`, and afterwards runs a
  sanity check across every timer. Run it with `uv run sweeper`.
- **`app/symbolic/`** — three independent rule engines (`fire.py` asks all
  of them and requires them to agree): Prolog rules, an OPA policy, and
  Datalog rules. These aren't part of this folder but `fire.py` and
  `sweeper.py` depend on them for every real decision — see "the three
  engines that double-check every decision" below.

## Timers: what they are and why each one exists

A **timer** is one row in the `timers` table: "at this time, this case needs
this thing to happen." There are four kinds:

### 1. `reassessment` — the recheck clock

Created when a case clears into the queue (`monitoring` state calls
`timers.schedule(...)`). Says: "check on this patient again in N minutes."
N depends on how urgent the patient is (their ESI acuity score, 1 = most
urgent, 5 = least urgent):

| Acuity | Recheck every |
| --- | --- |
| 1 (most urgent) | 10 minutes |
| 2 | 15 minutes |
| 3 | 30 minutes |
| 4 | 60 minutes |
| 5 (least urgent) | 120 minutes |

When it fires, the sweeper wakes the paused case with a `REASSESSMENT_TIMEOUT`
event. The case then re-runs its full pipeline (classification, gate, safety
checks) from scratch on fresh vitals — same as a brand new case — because a
changed condition deserves the same scrutiny a first visit got.

These numbers are working placeholders, not settled clinical policy — a
Medical Director still needs to sign off on the actual minutes.

### 2. `gate_reminder` — nudge someone to approve a case

When a case is paused waiting for a human to approve it (the
"human-approval gate"), two of these are scheduled at once — one for each
"rung" of escalation:

- Rung 0, fires after 10 minutes: pings the nurse who's assigned to the case.
- Rung 1, fires after 20 minutes: widens to any charge nurse.

Which rung a `gate_reminder` belongs to is `cycle % 2` (cycle 0 = rung 0,
cycle 1 = rung 1, cycle 2 = rung 0 again next gate visit, etc). If the case
has already moved past the gate by the time the reminder fires, nothing is
sent — it's cancelled instead (see "cancellation" below).

### 3. `reassessment_reminder` — nudge someone to re-file a patient

After a reassessment fires, the case pauses again waiting for a nurse to
submit fresh vitals (`awaiting_reassessment_submission`). This timer fires
15 minutes later and pings any charge nurse if nobody has re-filed yet.
Without this timer, that particular pause would be the one place in the
whole system where a waiting patient has no clock running on them at all.

### 4. `senior_reminder` — nudge a shift lead

When a case has bounced between the approval gate and safety re-validation
too many times (3 rounds) and gets escalated up to a senior, this timer
fires 10 minutes later and pings any shift lead if the case is still stuck
waiting for approval.

### The two families

`reassessment` timers are the only kind that actually changes the case —
they wake it up and move it forward. The other three (`gate_reminder`,
`reassessment_reminder`, `senior_reminder`) only send a notification; they
never touch the case's state. Because of that, notify-only timers skip
the "delivering" step entirely and go straight from due to done — see the
state diagram below.

## The `timers` table

One row per timer. Columns:

| Column | What it holds |
| --- | --- |
| `timer_id` | Unique id, built as `case_id:kind:cycle` (e.g. `case-42:reassessment:3`). Because it includes `cycle`, each new reassessment cycle gets its own row instead of colliding with the previous one. |
| `case_id` | Which patient case this timer belongs to. |
| `kind` | One of the four kinds above. |
| `cycle` | Which round this is for that case+kind. Reassessment cycles increment on the case each fire; gate reminder cycles are `visit + rung` so each gate visit gets fresh ids. |
| `due_at` | The timestamp this timer should fire at. |
| `fire_state` | Where this timer is in its lifecycle — see the states table below. |
| `fire_id` | A fingerprint of `(case_id, kind, cycle, due_at)`, computed once the timer is actually acted on. Used to check "did *this exact* firing land in the case's history", so a retry can tell the difference between "never happened" and "happened but we lost track." |
| `attempts` | How many times reconciliation has tried to figure out what happened to an unclear (`UNKNOWN`) timer. Once this hits the reconcile budget (3), the timer gives up and escalates to a human instead of trying forever. |
| `lease_until` | While a sweeper is working on this row, this is set a bit into the future (30 seconds) so no other sweeper grabs it too. If a sweeper crashes mid-work, the lease simply expires and another sweeper picks the row back up. |
| `worker_id` | Which sweeper process currently holds (or last held) the lease on this row. |
| `last_error` | Human-readable reason the timer is stuck or failed, e.g. `engine_unavailable:prolog`, `layer_disagreement: bppy=X prolog=Y`, `opa denied dispatch: ...`, `case not found`. |
| `created_at` / `updated_at` | Standard bookkeeping timestamps. |
| `decision` | What the decision-making layers actually chose for this timer's most recent claim, and if a safety rule overrode the obvious choice, which one and why. Example: `RECONCILE (proposed DISPATCH: dispatch: blind_redispatch_from_unknown)`. Written *before* the chosen action runs; `fire_state` is written *after*. |

There's also an index on `(fire_state, due_at)` so "find everything that's
due" is a fast lookup rather than a table scan.

## Fire states — every status a timer can be in

"Fire" means a timer going off, like an alarm going off. This is the timer's
full life cycle:

| `fire_state` | Meaning |
| --- | --- |
| `SCHEDULED` | Sitting in the queue, waiting for `due_at` to arrive. |
| `DUE` | Its time came and a sweeper has claimed it — about to be handled. |
| `DISPATCHING` | (reassessment timers only) Actively waking the case up right now. |
| `DELIVERED` | Done — it worked. Terminal state. |
| `FAILED` | A clear "no": the case is closed/missing, the reminder's notification budget ran out, or an engine refused it (`engine_unavailable:*`) or two engines disagreed (`layer_disagreement:*`). Safe to retry (for reassessments) or just leave as failed (for reminders). |
| `UNKNOWN` | Not sure it worked — the process may have died mid-attempt. Needs reconciliation. |
| `ESCALATED_TO_HUMAN` | Reconciliation tried enough times (3) and still can't tell — a human needs to look at it. Terminal state. |
| `CANCELLED` | A notify-only reminder that no longer matters, because the thing it was about to remind someone of is already resolved. Terminal state. |

There is deliberately no stored "reconciling" state — reconciliation takes
an `UNKNOWN` row and resolves it to one of `DELIVERED`, `FAILED`, `UNKNOWN`
again, or `ESCALATED_TO_HUMAN` in one single database call, so there's never
a window where a row sits in a "currently reconciling" limbo.

Transitions:

- `SCHEDULED` → `DUE` once `due_at` has passed and a sweeper claims it.
- `DUE` → `DISPATCHING` (reassessment) or straight to `DELIVERED` /
  `CANCELLED` / `FAILED` (notify-only reminder, no dispatching step needed).
- `DISPATCHING` → `DELIVERED` (the resume landed), `FAILED` (resume raised
  an error, or the case is already gone), or `UNKNOWN` (the worker process
  died mid-call, so the lease just expired without a clean answer).
- `FAILED` → `DISPATCHING` again if it gets picked up for a retry.
- `UNKNOWN` → `DELIVERED` (reconciliation found this exact `fire_id` already
  in the case's history), `FAILED` (case is still sitting at the same pause,
  so nothing landed — safe to redispatch), `UNKNOWN` again (still can't
  tell, but attempts left), or `ESCALATED_TO_HUMAN` (attempts budget spent).

**The one rule that matters most:** `UNKNOWN` never jumps straight back to
`DISPATCHING`. If a fire's outcome is unclear, blindly retrying it could
deliver it twice. Reconciliation always checks the case's audit trail for
that exact `fire_id` first; only a clear "it never happened" leads to a
safe retry.

## The sweeper loop

`uv run sweeper` runs forever. Every 5 seconds it does, in order:

1. **Heartbeat.** Write a row saying "I'm alive" (see below).
2. **Claim newly due timers.** Ask the database for every `SCHEDULED` timer
   whose `due_at` has passed and that nobody else currently holds the lease
   on, and mark them `DUE` with a fresh 30-second lease, atomically (so two
   sweepers running at once can never grab the same timer).
3. **Claim leftovers.** Also grab any `FAILED`, `UNKNOWN`, or lease-expired
   `DISPATCHING` timer — these are retries from earlier ticks.
4. **Hand each claimed timer to `fire.handle()`**, which decides and
   executes what happens to it.
5. **Run one consistency check** (the Datalog pass, explained below) over
   the *entire* timer store, looking for two kinds of trouble across all
   cases at once — not just the ones claimed this tick.

If anything in a single tick blows up unexpectedly, the sweeper logs it and
keeps ticking — a crash in one tick must never stop the whole loop.

## The three engines that double-check every decision

`fire.py` never decides anything by itself. For every claimed timer it
builds one bag of facts (kind, current state, is the case still paused where
this timer expects it to be, how many notifications went out recently) and
asks three separate systems, each answering a different question:

1. **BPpy (`bthreads.py`)** — "which single action is allowed right now?"
   A `proposer` thread asks for the obvious thing (dispatch a reassessment,
   send a reminder). Other threads act as safety rules: if the timer's last
   attempt is unacknowledged, a rule blocks a blind redispatch and requests
   `RECONCILE` first instead. If a reminder's pause already resolved, a rule
   cancels it instead of sending it. If the notification budget for that
   recipient is used up, a rule fails it instead of sending yet another
   alert. Exactly one of these wins per timer.

2. **Prolog** (`app/symbolic/rules/monitor.pl`) — answers the exact same
   question independently, using its own rules, and can also explain *why*
   something is refused. `fire.py` requires Prolog's answer to match BPpy's
   answer exactly — if they disagree, the timer is marked `FAILED` with
   `layer_disagreement` rather than guessing which one is right. If the
   Prolog engine (`swipl`) itself isn't running, the timer fails with
   `engine_unavailable:prolog`.

3. **OPA** (`app/symbolic/policy/monitor.rego`) — the very last check,
   immediately before the actual side effect (waking the case, or recording
   a sent notification). Answers "is this exact action allowed to happen
   right this instant?" If the `opa` binary isn't running, the action is
   refused with `engine_unavailable:opa`.

4. **Datalog** (`app/symbolic/datalog.py`) — not per-timer. Runs once at
   the end of every sweeper tick over the whole timer store plus every case
   it touches, looking for two problems no single timer can see on its own:
   a waiting patient that nothing is currently watching (no live timer for
   them at all), and a timer that's still alive but points at a case that no
   longer exists. Each finding becomes one row in `escalations`, addressed
   to a technician, written once per (case, problem) so a problem found on
   every tick doesn't spam escalations every 5 seconds. This pass never
   touches a timer's state — a timer it keeps flagging as a problem is
   evidence to look at, not something to quietly fix.

If either the Prolog or the OPA binary isn't installed and running, every
action they'd need to approve is refused rather than silently allowed
through — the sweeper keeps ticking, but the affected rows sit as
`FAILED`/`engine_unavailable:*` until the engine comes back.

## The other tables

### `sweeper_heartbeats` — watching the watcher

A crashed sweeper fails silently from the outside — timers just quietly
stop firing, with no error anywhere. So every tick, the sweeper writes/
updates one row here per `worker_id`, with the current timestamp
(`beat_at`). Something outside the sweeper (the board's `/api/heartbeat`
endpoint) checks the age of these rows: if a worker's last heartbeat is
older than 3x the sweep interval (i.e. older than 15 seconds, since the
sweeper ticks every 5), that worker counts as stale. If every known worker
is stale, or there are no heartbeat rows at all, the whole monitor is
considered degraded and the board shows a "monitor degraded" warning. A dead
process can't report its own death, so this check has to live somewhere
else — hence a separate table plus an outside reader.

Columns: `worker_id` (primary key, one row per sweeper process),
`beat_at` (last time that worker said "I'm alive").

### `notifications` — every reminder actually sent

One row per notification that was actually delivered. Used to enforce the
per-recipient rate limit (no more than 5 notifications to the same
recipient class inside any 60-minute window, so a bug in a timer loop can't
flood staff with alerts) and to prevent sending the exact same reminder
twice.

Columns:

| Column | What it holds |
| --- | --- |
| `id` | Auto-incrementing primary key. |
| `case_id` | Which case this notification was about. |
| `reason` | What it was for, built as `{kind}_{cycle}` (e.g. `gate_reminder_1`). |
| `channel` | Where it was sent — currently always `notification_strip` (a UI element, not email/SMS). |
| `recipient_class` | Who got it: `assigned_nurse`, `any_charge_nurse`, or `any_shift_lead`. |
| `sent_at` | When it was recorded. |

There's a uniqueness constraint on `(case_id, reason)` — trying to insert
the same case+reason twice is silently rejected, which is what makes
sending the same reminder twice impossible even under a race.

### `escalations` — every time a human had to be paged

One row per out-of-band alert raised either by a timer that ran out of
reconciliation attempts (`ESCALATED_TO_HUMAN`) or by the Datalog
consistency check (`unwatched_case`, `orphan_timer`).

Columns:

| Column | What it holds |
| --- | --- |
| `id` | Auto-incrementing primary key. |
| `case_id` | Which case triggered this. |
| `fire_id` | The fingerprint of the timer firing that led to this escalation. For Datalog findings (which aren't about one specific firing), this is a synthetic value like `invariant:unwatched_case`. |
| `raised_at` | When it happened. |
| `channel` | Where the alert went — currently always `notification_strip`. |
| `recipient_class` | Who it's addressed to: `technician` (infra problems, Datalog findings) or `charge_nurse` (a reassessment that's ambiguously overdue). |
| `reason` | Why: `store_unreachable`, `reassessment_overdue`, `unwatched_case`, or `orphan_timer`. |

## Cancellation, in detail

Only notify-only reminders (`gate_reminder`, `reassessment_reminder`,
`senior_reminder`) get cancelled — a reassessment timer either dispatches or
fails, never cancels. A reminder gets cancelled when, by the time it fires,
the thing it would have reminded someone about is already resolved — e.g. a
`gate_reminder` fires but the case already got approved and moved past the
gate. Sending it anyway would be a stale, confusing alert, so BPpy's
`stale_reminder` rule intercepts it before it goes out and cancels it
instead.

## Two real bugs this design had to fix

**Reusing timer ids.** The timer id is `case_id:kind:cycle`. Early on,
`cycle` was always 0, so every new reassessment for the same case reused the
exact same `timer_id` and `due_at` forever — the second reassessment never
actually got scheduled. Fixed by giving each case its own
`reassessment_cycle` counter that increments on every fire.

**Matching on nothing.** Reconciliation checked "did this fire land" by
comparing `fire_id` values in the case's audit log. But a nurse manually
reporting a patient got worse produces an audit entry with no `fire_id` at
all (`None`) — and so do several other unrelated audit entries. Comparing
`None == None` made those look like a match, so real nurse reports were
mistaken for a timer's own delivery and silently dropped. Fixed by only
counting a match when there's an actual fingerprint to compare
(`if fire_id and rec.get("fire_id") == fire_id`).

Both bugs only showed up once a case went through more than one cycle —
"worked in the first test" and "actually works" turned out to be different
claims.
