# Waiting-room monitor — design and build plan

**Date:** 2026-09-15
**Status:** proposal, not yet built
**Closes:** `docs/STATUS.md` item **5a — "The waiting-room timers that flag a patient
who's been waiting too long."**
**Builds on:** the failure discipline of `docs/SYSTEM_MODELING.md` §4–§5 (UNKNOWN is not
FAILED; reconcile before you re-act), applied to a second machine.

---

## 1. What this is

The **Waiting Room Monitor** is named in five places in `SPECIFICATION.md` — the Actors
table, four events (`REASSESSMENT_TIMEOUT`, `DETERIORATION_DETECTED`,
`GATE_TIMER_ASSIGNED_NURSE`, `GATE_TIMER_ESCALATE_ANY_CHARGE`), two state variables
(`reassessment_timer`, `gate_timer`), and two safety invariants (Reassessment bound,
Wait liveness) — and has no code at all. Arrows `13`, `14`, `20a`, `20b` exist as
`Arrow` members and as one audit line written by `terminal.py::monitoring` that says
`start_reassessment_timer` and starts nothing.

### Not a fifth service — the spec's sixth agent, in-process

The board and `intake-channel` are separate FastAPI services because they are **views
and front doors**: they never write case state, so a network hop between them and the
graph costs nothing but latency. The monitor is different in kind. Writing a timer row
and moving a case into `monitoring` (arrow 13) has to land as one unit — a network call
between "the case entered `monitoring`" and "a timer now watches it" is exactly the gap
where a case could leave the first without ever getting the second, which is the hazard
this whole plan exists to close (§3 makes this precise). A component that must share a
transaction with the graph's own state write does not belong behind a service boundary;
the boundary itself becomes a new failure mode, on top of the ones this plan already
has to handle.

So this plan builds the monitor as the spec's own vocabulary already names it — the
**sixth agent in the Agent Core** (Actors table: Intake Parser, Acuity Classifier,
Safety Validation, Human Escalation, **Waiting Room Monitor**, Audit) — living inside
`triage-app`, beside the other five:

```
triage-app/app/monitor/timers.py            timer store: schema, schedule, claim/lease (§4, §6)
triage-app/app/monitor/fire.py              the fire state machine: dispatch, ack, reconcile, notify (§4, §5)
triage-app/app/monitor/sweeper.py           the worker loop (`uv run sweeper`)
triage-app/app/graph/nodes/terminal.py      monitoring / awaiting_reassessment (arrows 13, 14)
triage-app/app/graph/nodes/reassessment.py  reassessment_required (arrow 15)
```

One package, `app/monitor/`, not scattered top-level modules — the graph nodes
that pause and resume import `timers` from it directly and never import `fire`
or `sweeper`, keeping the "proposes a moment, does not decide" split (§7)
visible in the import graph, not just in prose.

run as its own **process**, not its own **service** — `uv run sweeper` alongside
`uv run triage-guard` — sharing the one triage store `2026-09-11-board-service-design.md`
§2b already settled on for checkpoints, the board projection, and (later) the
treatment-move outbox. No new `pyproject.toml`, no new port, no HTTP API of its own:

```
crm-stub        :8000   patient history
intake-channel  :8001   the front door — one case in
board           :8002   the room — all cases out, and now the monitor's status too (§10)
triage-app              the control plane, the sixth agent, and its worker process
```

The monitor is still a **clock and a signaller**, exactly as first framed: it holds
durable per-case timers, notices when one comes due, and delivers an event into the
graph. It never decides anything clinical and never writes case state directly — the
same authority split the board obeys, for the same reason, here enforced by being the
same codebase rather than by a contract between two of them.

## 2. Scope

**In:** the per-patient reassessment timer (arrow 13 → 14), the approval-gate reminder
ladder (20a → 20b), the safety-fail parking SLA that `SYSTEM_MODELING.md` §2.5 assigns
to this monitor, a durable timer store, the sweeper that fires them, and the failure
model below.

**Out:** the monitor never computes acuity, never re-sorts the queue, never writes
`clinical_status`, `acuity`, or `order_key`. **Timer firing forces a reassessment; it does
not re-sort the queue** (`SPECIFICATION.md` § Queue ordering rule) and the
acuity-write-authority invariant says in as many words that the timer never changes an
acuity. Every effect the monitor has on a case goes through the graph as an event.

**Non-goals for v1:** a real vitals feed for `DETERIORATION_DETECTED` (§9 — there is no
source for it today and this plan does not invent one), an HTTP API of its own (§1, §10),
push notification transports beyond the board's existing notification strip, per-nurse
routing rules. Running more than one sweeper process is also not required for v1 — the
claim statement in §6 is already race-safe for N processes without change, so scaling out
later (§6b) is additive, not a rewrite.

## 3. Why this service needs the UNKNOWN treatment at all

The instinct is that the treatment-move discipline does not apply here. Firing a timer is
not irreversible: nobody is wheeled into a room because a clock ticked. So why not fire,
and fire again if unsure?

Because "irreversible" was never the actual criterion. The criterion in §4 is **an action
whose outcome the acting component cannot observe**, where acting twice is not free. A
timer fire qualifies on both counts:

- **The monitor cannot observe the outcome.** It dispatches a fire into the control
  plane and waits for an acknowledgement. The acknowledgement can be lost while the fire
  itself lands — the identical decision/execution gap that produces `UNKNOWN` for the
  Gateway, with the sweeper as decider and the graph as executor.
- **A duplicate fire is not free.** It re-notifies a nurse, which is the one currency this
  service spends and cannot claw back: a monitor that cries twice trains people to
  discount it, and an alert channel nobody reads fails the wait-liveness guarantee just as
  completely as a channel that stays silent. A duplicate also advances the reminder ladder
  a rung early (20a → 20b widens to *any* charge nurse, i.e. it spends the scarce resource
  §6 of the modelling doc is entirely about) and, for a case in `reassessment_required`,
  re-enters the front door a second time.

So: same machine, same rule — **a timeout is UNKNOWN, not FAILED, and a fire is never
re-dispatched from UNKNOWN without reconciling first.**

### The one place it must differ, and why

The treatment move resolves uncertainty toward **inaction**: when we do not know, we do
nothing until we know, because acting twice can start treatment twice. The waiting room
must resolve uncertainty toward **action**: when we do not know, we escalate anyway,
because the hazard this service exists to prevent is a patient nobody looked at. The
hazards point in opposite directions, so the safe defaults do too.

Concretely, this changes two things and nothing else:

1. **Exhausting the budget is not a failure mode here — it is the product.** For the
   treatment move, `ESCALATED_TO_HUMAN` means the automation gave up and a human has to
   finish an act the machine could not. For a timer fire, the whole point of the fire is
   to get a human's attention; so when delivery into the graph cannot be established, the
   monitor **raises the escalation directly** — notification strip, technician alert,
   degraded banner — and that discharges `G(waiting -> F≤T escalation_raised)` even
   though the control plane never heard about it. The service's worst failure degrades
   into exactly its purpose. Nothing about the treatment move has this property, which is
   why it is worth stating.
2. **The forbidden transition is narrower.** `UNKNOWN → DISPATCHING` is still forbidden. But
   `UNKNOWN → ESCALATED_TO_HUMAN` is *permitted directly*, without passing through
   reconciliation, once the reconciliation budget is spent. Under the treatment machine
   that shortcut would be dangerous; here refusing it is what leaves a patient unwatched.

### One thing that is strictly better here

`SYSTEM_MODELING.md` §8 names the load-bearing assumption: reconciliation trusts its
source, and a Gateway that reports "done" for a move that never happened would resolve
`UNKNOWN → CONFIRMED` for a patient who silently left the system. **This monitor's source
of truth is inside the boundary** — our own checkpoint store and append-only audit log,
written by the single Flow step that owns case state. Reconciliation here is a read
against our own records, not a re-query of a foreign system. That does not make it
infallible, but it removes the specific assumption that would force a redesign of the
treatment machine.

### The residual gap, even in-process

Running the monitor as the sixth agent inside `triage-app` (§1) removes the *routine*
version of the decision/execution gap — a network call that can time out. It does not
remove it entirely. LangGraph commits a node's checkpoint only after the node returns,
so "write the timer row" and "the case's own checkpoint write" are still two separate
commits even when both happen in the same call stack; a crash between them, while rare,
is not impossible. Nothing new has to be built for this: the recovery sweep (§5b.3)
already has to catch a case sitting in `monitoring` with no live timer row, because it
has to cover the lost-ack case regardless. Moving in-process removes the failure mode
that would have been *common* (a timed-out request); what is left is the *rare* one the
catch-up sweep already has to handle.

## 4. The fire state machine

One instance per **fire attempt**, not per timer. A timer that fires, is delivered, and
reschedules produces one machine per cycle.

### States

| State                | Meaning                                                                       | Final?  |
| -------------------- | ----------------------------------------------------------------------------- | ------- |
| `SCHEDULED`          | Timer row exists with a `due_at` in the future. Nothing to do.                | no      |
| `DUE`                | `due_at` has passed; claimed by the sweeper under a lease.                    | no      |
| `DISPATCHING`             | Fire dispatched to the control plane, awaiting acknowledgement.               | no      |
| `DELIVERED`          | Acknowledged — the case shows the effect. Next cycle is scheduled.            | **yes** |
| `FAILED`             | Explicit rejection (`ACTION_DENIED`, case closed, thread gone). Evidence the fire did **not** take effect; safe to re-dispatch or to cancel. | no |
| `UNKNOWN`            | Dispatch timed out, no acknowledgement. We do **not** know whether it landed. | no      |
| `RECONCILING`        | Reading the case's checkpoint + audit log to establish whether it landed.     | no      |
| `ESCALATED_TO_HUMAN` | Budget spent. The monitor raised the alert itself, out-of-band.               | **yes** |
| `CANCELLED`          | The timer's reason to exist ended (case closed, released, re-filed).          | **yes** |

Three final states, and every path reaches one. `CANCELLED` is a state and not a row
deletion on purpose: a deleted timer and a timer that was never created look identical
afterwards, and "nobody is watching this patient" must never be reachable by accident.

### Event catalog

| Event                  | Transition                       | Producer            | Evidence source                    |
| ---------------------- | -------------------------------- | ------------------- | ---------------------------------- |
| `schedule`             | — → SCHEDULED                    | Monitor (on arrow 13 / gate entry) | graph audit record       |
| `due`                  | SCHEDULED → DUE                  | Monitor sweeper     | its own clock + the timer row      |
| `dispatch`             | DUE → DISPATCHING                     | Monitor sweeper     | —                                  |
| `ack_applied`          | DISPATCHING → DELIVERED               | Flow (graph)        | the case's own state write         |
| `ack_refused`          | DISPATCHING → FAILED                  | Flow (graph)        | `ACTION_DENIED` / closed case      |
| `dispatch_timeout`     | DISPATCHING → UNKNOWN                 | Monitor sweeper     | its own timer                      |
| `reconcile_result`     | RECONCILING → DELIVERED / FAILED | Monitor sweeper     | checkpoint + audit log (`fire_id`) |
| `reconcile_unresolved` | RECONCILING → UNKNOWN            | Monitor sweeper     | — (store unreachable)              |
| `redispatch`           | FAILED → DISPATCHING                  | Monitor sweeper     | its own attempt counter            |
| `budget_spent`         | FAILED / UNKNOWN → ESCALATED_TO_HUMAN | Monitor sweeper | its own attempt counter          |
| `cancel`               | any non-final → CANCELLED        | Monitor (on case event) | graph state (closed / re-filed) |

Same split as §4 of the modelling doc: everything derived from state the sweeper holds
(clock, attempt counter, reconciliation outcome) is the sweeper's to produce; only
`ack_applied` / `ack_refused` originate outside it, and they come from the Flow, which is
the component that actually applied the event.

A third ack outcome, `ack_deferred` (DISPATCHING → DISPATCHING, same `fire_id`), covers a thread
suspended at an `interrupt()` (§17.1): the graph reports "not resumable yet," which is
positive evidence, not a timeout, so it does not enter `UNKNOWN` and does not consume an
attempt.

### State owner

`fire_state` has a single owner: **the sweeper loop**, one writer, claiming rows under a
lease (§6). The graph never writes `fire_state`; it reports facts (the ack), exactly as
the Tool Gateway reports receipts and the single Flow step turns them into state. Two
sweepers writing the same row would race to fire the same timer twice, which is the
duplicate-notification hazard arriving through the back door.

### Forbidden transition

**`UNKNOWN → DISPATCHING` directly.** From `UNKNOWN` the machine passes through `RECONCILING`;
only a reconciled `FAILED` permits re-dispatch, and only `budget_spent` permits the jump
straight to `ESCALATED_TO_HUMAN`.

**Hazard.** The fire landed, the case moved to `reassessment_required`, the nurse was
notified, and the acknowledgement was lost in transit. A blind re-dispatch notifies a
second time, advances the ladder, and — for the reassessment timer — re-enters the front
door on a case already being re-filed.

**Second line of defence: `fire_id`.** Every dispatch carries
`fire_id = hash(case_id, timer_kind, cycle, due_at)`, stable across re-dispatches of the
same cycle. The graph node that accepts timer events de-dupes on it: a repeat `fire_id`
is a no-op that returns the original outcome. This is the `idempotency_key` of §5, in the
same role — the state machine protects the decision, the key protects the interface, and
neither is trusted alone.

## 5. The two failures this plan is about

### 5a. Timeout — the control plane does not answer

The sweeper dispatches a fire; the configured dispatch window passes with no ack.

- **Next state: `UNKNOWN`**, never `FAILED`. A timeout is the absence of evidence.
- **Forbidden:** re-dispatch from `UNKNOWN`.
- **Correct next step:** `RECONCILING`. Ask the source of truth: load the case's
  checkpoint and scan its audit log for a record carrying this `fire_id`. Found → the fire
  landed → `DELIVERED`, schedule the next cycle. Not found, and the case is in a state
  that proves the fire did not apply → `FAILED`, re-dispatch with the same `fire_id`.
- **If reconciliation cannot resolve** (checkpoint store locked, unreadable, ambiguous):
  stay `UNKNOWN` and re-query with backoff, bounded by the reconcile budget. A
  reconciliation is a read and changes nothing, so repeating it is safe — the same
  asymmetry §4 draws between retrying a query and retrying an act.
- **On exhaustion:** `ESCALATED_TO_HUMAN` — and per §3, the monitor raises the alert
  itself rather than only recording that it gave up.

### 5b. Unavailable — and the silent version of it

Two different things wear this name, and only one of them is loud.

**The thing the monitor calls is down** (graph, checkpoint store, notification transport).
This is ordinary: dispatch fails fast with an explicit error, the fire goes to `FAILED`
(explicit rejection is evidence, not absence), backoff, re-dispatch within budget,
`alert_technician` on exhaustion, and the board shows a "monitor degraded" banner so
staff know the clock is not currently guaranteeing anything. This follows the spec's
existing per-agent failure pattern: degrade and alert, never hard-halt.

**The monitor itself is down** — and this one is silent, which makes it the most dangerous
failure in the service. Every other component in Triage Guard announces its own outage by
failing a call somebody made. Nobody calls the monitor. When it dies, no timer fires, no
error is raised, no case changes state, the board looks calm, and
`G(waiting -> F≤T escalation_raised)` is being violated continuously with no symptom.
Three mechanisms, and the service is not shippable without all three:

1. **Timers are durable, never in memory.** Due-times live as rows in the shared triage
   store (§6). A process that dies loses nothing but time; an in-memory scheduler loses
   the patients.
2. **A dead-man's switch.** The sweeper writes a heartbeat row every tick. Absence of
   heartbeat, not presence of error, is the alarm: a heartbeat older than `k × sweep
   interval` pages the on-call technician and raises the board banner. This is the only
   detector for the silent case, and it lives outside the monitor (the board reads the
   heartbeat; a monitor that could self-report its own death would not be dead).
3. **A recovery sweep on restart — which is reconciliation over the whole store.** Every
   timer with `due_at` in the past is overdue, not lost. On startup the sweeper walks
   them oldest-first, and for each one re-reads the case before dispatching, because
   during the outage the case may have moved on by other means (released, moved to
   treatment, gate resolved) and the right action is `CANCELLED`, not a stale fire. Two
   guards on the sweep: **bounded rate**, so a long outage does not dump a thousand
   notifications into the room at once (the thundering herd is itself an alarm-fatigue
   event), and a **`timer_gap` flag** written onto every case whose timer was overdue by
   more than the grace window — so the trail shows honestly that there was a window in
   which this patient was not being watched. A silent window that leaves no record is
   indistinguishable afterwards from a window that was fine.

### Why "monitor down" and "fire refused" are opposite cases

Same shape as the `validation failed` / `validator down` pair in §2 of the modelling doc,
and worth stating for the same reason. A **refused fire** is information: the graph
looked and said no (case closed, action denied) — the correct response is to stop,
because there is a concrete reason this timer should not fire. A **dead monitor** is the
absence of information: nothing was evaluated, and the correct response is the opposite —
assume every overdue patient still needs looking at, and catch up. Treating them alike in
either direction is a bug: stop-on-silence forgets patients, catch-up-on-refusal fires
timers at released patients.

## 6. Timer store and the sweeper

**Where.** A table in the **shared triage store**, not a store of its own. This was already
settled in `2026-09-11-board-service-design.md` §2b — checkpoints, the board projection,
the reassessment timers (5a) and the treatment-move outbox (4) live together — for the
reason given there: a timer row and the case state it refers to must be writable in one
transaction, or there is a window where the clock and the case disagree.

```sql
CREATE TABLE timers (
  timer_id     TEXT PRIMARY KEY,   -- case_id + kind + cycle
  case_id      TEXT NOT NULL,
  kind         TEXT NOT NULL,      -- reassessment | gate_reminder | safety_park | reassessment_reminder
  cycle        INTEGER NOT NULL,   -- 0,1,2... per case+kind; the ladder rung
  due_at       TEXT NOT NULL,      -- ISO8601 UTC
  fire_state   TEXT NOT NULL,      -- §4
  fire_id      TEXT,               -- idempotency key of the current attempt
  attempts     INTEGER NOT NULL DEFAULT 0,
  lease_until  TEXT,               -- claim lease; NULL when unclaimed
  worker_id    TEXT,               -- which sweeper process holds the lease (§6b)
  last_error   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX timers_due ON timers (fire_state, due_at);

-- One row per sweeper process. v1 runs exactly one row here; §6b explains why the
-- schema already supports more without a migration.
CREATE TABLE sweeper_heartbeats (
  worker_id    TEXT PRIMARY KEY,
  beat_at      TEXT NOT NULL
);
```

**Claiming.** One statement, so the single-owner property survives a second process
started by accident:

```sql
UPDATE timers SET fire_state='DUE', lease_until=:now_plus_lease,
       worker_id=:me, updated_at=:now
 WHERE fire_state='SCHEDULED' AND due_at <= :now
   AND (lease_until IS NULL OR lease_until < :now)
 RETURNING timer_id, case_id, kind, cycle;
```

A lease that expires with the row still in `DISPATCHING` is exactly the `UNKNOWN` case and is
routed into `RECONCILING`, not re-dispatched — the crash-mid-dispatch path and the
lost-ack path are the same path, which is the point of modelling it as absence of
evidence rather than as a kind of error. Note that reconciliation does not have to be
done by the process that dispatched: a lease expiring is what hands the row to
*whichever* sweeper claims it next, itself included — see §6b.

**Clock.** UTC, from the store's own `now`, not the process clock, so a container with
skewed time cannot mass-fire or mass-delay. Monotonic sleep between sweeps; `due_at`
comparisons are wall-clock because the durations are clinical, not computational.

### 6b. More than one sweeper — supported by the schema, not needed for v1

**v1 ships exactly one sweeper process.** This subsection exists so that adding more
later is a config change, not a redesign — the same "swap, not rewrite" property the
board's `BoardRepo` Protocol already relies on for its own storage upgrade (Option A →
B in `2026-09-11-board-service-design.md` §3).

**What already works with no change.** The claim statement above is a single atomic
`UPDATE ... WHERE ... RETURNING`. With N sweeper processes racing it on the same row,
exactly one succeeds — the others simply match zero rows and move on to the next
candidate. No coordinator, no explicit locking protocol between processes: the database
is the only arbiter. Crash recovery is identical whether the same process or a
different one performs the next sweep, because ownership is decided by *lease expiry*,
not by which process started the attempt — a stuck `DISPATCHING` row is picked up by
whoever claims it next and routed into `RECONCILING`, exactly as in the single-process
case.

**What does not come for free: SQLite is a single writer.** WAL mode allows concurrent
readers plus one writer at a time. With 2–3 sweeper processes claiming against the same
file, a losing `UPDATE` does not silently return zero rows the way it does under one
writer's serialized queue — it can raise `SQLITE_BUSY`. That is not a correctness bug
(the loser still did not corrupt anything), but it does mean the claim needs a short
`busy_timeout` and a plain retry, or a crash on `SQLITE_BUSY` will look like a sweeper
failure when it is really lock contention. This is the trigger, not a byproduct: the
same "move to Postgres" step already flagged in `2026-09-11-board-service-design.md` §3
Option B and this plan's own milestones (§16, M4/"later") is where the claim query
becomes `SELECT ... FOR UPDATE SKIP LOCKED`, which partitions the due set across
replicas with no lock contention at all — the standard pattern for this exact problem,
and the reason SQLite is fine for one sweeper but Postgres is the right answer for
several.

**Heartbeat becomes per-replica.** `sweeper_heartbeats` is already keyed by `worker_id`
rather than being a single row, so N processes write N rows. The metric in §12 changes
accordingly: with one sweeper, "no heartbeat" means the monitor is dead; with N, the
threshold is "fewer live heartbeats than the expected replica count" — losing one of
three is degraded capacity, worth a lower-severity page, not the silent-outage alarm
(§5b) that a single missing heartbeat is when there was only ever one.

**Why to run more than one at all.** Two reasons, neither needed today: throughput, if
dispatch or reconciliation itself becomes slow (e.g. a redispatch that re-enters the
graph and calls the classifier again); and availability, so the overdue backlog keeps
draining while one replica restarts instead of waiting on a supervisor.

## 7. What the monitor may and may not write

The board's rule, restated for the clock, because this is the second chance the design has
to grow a second writer on the World plane.

| The monitor writes | The monitor never writes |
| --- | --- |
| `timers` rows (`fire_state`, `due_at`, `attempts`, leases) | `acuity` — acuity write-authority invariant, in as many words |
| its own heartbeat | `order_key` / queue position — the clock never re-sorts |
| notification records (deduped by `case_id + reason`, per the spec's `notify_user` idempotency) | `clinical_status` — only the graph moves a case |
| `timer_gap` flags, via the graph as an event | `safety_verdict`, `approved`, anything on the Data plane |

Every case-affecting effect is an **event delivered to the graph**, which decides whether
it is permitted and writes the result. If a fire arrives for a case whose state does not
allow it, the answer is the existing `ACTION_DENIED` / `BLK` path, rendered on the board
like any other refusal. The monitor proposes a moment; the Flow decides what it means.

## 8. Intervals and budgets — the numbers we do not invent

Three numbers this service needs, none of which is in the spec. The repo's existing stance
(`app/budgets.py`: the mechanism is real, the values are visible placeholders in one
table) applies, with one distinction worth drawing:

| Number | Fixed from | Why |
| --- | --- | --- |
| Reassessment interval per acuity band | **clinical policy**, not telemetry | How often a level-2 patient must be re-looked-at is a medical governance decision. No amount of production data answers it; the charge nurse / medical director does. |
| Gate ladder rungs (20a delay, 20b delay) | measured approval latency | This is a data question: how long before a reminder helps rather than annoys. |
| Dispatch timeout, reconcile budget, sweep interval | measured store/graph latency | Same class as the existing `RETRY_BUDGET` — set from real timings. |

These live in the repo's existing `app/budgets.py`, next to `RETRY_BUDGET` and
`MAX_CORRECTION_ROUNDS`, not in a table of their own — one place a reader checks for
"is this number real yet," not two. Each entry is marked with which of the three
sources must set it, and the reassessment intervals additionally marked as requiring
sign-off rather than a measurement. Starting values exist only so the sweeper runs; the
band structure (a level-2 interval shorter than a level-4 one) is the part that is
designed, the minutes are the part that is provisional.

Note the shape this shares with the other two undefined budgets in the project (machine
retry limit, human correction rounds): finite, escalates on exhaustion, decided from
evidence rather than feel. That is now four of them, already tracked in `STATUS.md`
§ Four loose ends.

## 9. `DETERIORATION_DETECTED` has no source, and this plan does not pretend otherwise

The spec lists it as an internal event from this monitor carrying "patient id + signal".
There is no signal. Intake is a one-shot structured form (per the 2026-09-13 classifier
design), there is no vitals stream, no bedside monitor integration, and no nurse-facing
control that emits it. Inventing a fake detector here would put a clinical trigger behind
a stub, which is the one place in this system a stub is not acceptable.

**v1:** a single nurse-initiated endpoint on the **board** — the nurse-facing service;
the monitor has no API of its own (§1, §10) —
`POST /api/case/{case_id}/deteriorated {signal, actor_role}` — that emits
`DETERIORATION_DETECTED` into the graph with the nurse as the evidence source, the same
way the board's existing `/move` and `/release` (M2 of the board plan) already re-enter
the graph rather than going around it. That is honest (a human observed something), it
exercises the whole path (arrow 14 → 15), and it is what a real deployment would keep as
a manual override even after a vitals feed exists. The automatic detector stays an
explicit hole, listed in §16.

## 10. No API of its own — the board's surface grows instead

Because the monitor is a worker process, not a service, it exposes nothing over HTTP.
Everything an operator or a nurse needs to see about it is a read against the shared
triage store the board already opens, so it surfaces as **additions to the board's
existing API** (`board/board/api.py`) rather than a second API to keep in sync:

```
GET  /api/board                    counters gain "monitor degraded" + overdue count (§12)
GET  /api/case/{case_id}           timer history joins the existing audit trail
GET  /api/heartbeat                new — reads sweeper_heartbeats (§6) directly
POST /api/case/{case_id}/deteriorated   nurse-initiated deterioration (§9)
```

`/api/heartbeat` is one more query against infrastructure the board already owns — the
same relationship it already has with the checkpointer for `/api/board` itself — not a
new integration or a second store to reach into.

Forcing a sweep for tests needs no endpoint either: `tests/` calls the sweeper's
`run_once()` directly, the way tests elsewhere in the repo call graph nodes directly
rather than going through a server.

## 11. Capacity — the monitor must not become a source of λ

`SYSTEM_MODELING.md` §6 leaves the human approval queue at ρ = 1.2, already unstable, and
names lowering λ as the architectural lever. A reminder ladder aimed at the same two
approvers has to be read against that number.

- **Reminders do not add λ.** 20a and 20b re-alert on cases *already in* the gate queue;
  they create no new approval requests. They consume approver attention without adding
  arrivals, which is the intended trade: a reminder that converts a stalled case into a
  served one actually raises μ.
- **The one path that does add λ** is the safety-fail parking escalation
  (`SYSTEM_MODELING.md` §2.4): when correction rounds are exhausted the case escalates to
  a more senior clinician. That is a new arrival at a scarcer resource, so it is bounded
  by the correction-round limit and should be counted separately in the ρ metric rather
  than hidden inside it.
- **A notification budget per nurse per window.** The monitor's own version of ρ ≥ 1 is
  alarm fatigue: past some rate, additional alerts reduce the number acted upon. A bug in
  a timer loop must not be able to empty that channel, so the notification path is rate-
  limited and the limiter's own trips are an alert to the technician, not a silent drop.

## 12. Monitoring map

Same shape as §7 of the modelling doc — each metric guards something this plan made
fragile.

| # | Metric | Guards | Threshold | Response |
| - | ------ | ------ | --------- | -------- |
| 1 | Heartbeat age | Wait liveness, against the silent outage (§5b) | > k × sweep interval | Page on-call technician; board raises "monitor degraded"; on recovery, bounded catch-up sweep |
| 2 | Overdue backlog (timers past `due_at` + grace) | Reassessment bound (`F≤t`) | > N, or any timer overdue > Y | Investigate; individual stuck timers escalate out-of-band per §3 |
| 3 | Fires stuck or spiking in `UNKNOWN` | The decision/execution gap this plan models | > N at once, or one > Y seconds | Stuck fire → `ESCALATED_TO_HUMAN`; a spike means the graph or store is down → technician |
| 4 | Notification rate per recipient | The alert channel itself (§11) | above the per-window budget | Rate-limit, and alert on the limiter tripping — a flood is a bug, not traffic |
| 5 | `timer_gap` flags raised | Honesty of the trail after an outage | any | Surfaced per case on the board; a case that went unwatched is reviewed, not silently resumed |

Metrics 1 and 5 are the pair that exists only because of the silent-outage analysis; the
other three are the waiting-room equivalents of metrics already in the modelling doc.
The table describes the v1, single-sweeper case; with more than one sweeper process
(§6b) metric 1's threshold changes from "the one heartbeat is missing" to "fewer live
heartbeats than the expected replica count," since losing one of several is degraded
capacity, not a dead monitor.

## 13. Evidence to retain

| Evidence | Proves | Critical for |
| --- | --- | --- |
| `timer_record {timer_id, case_id, kind, cycle, due_at, scheduled_by_arrow}` | a timer existed and when it was owed | the reassessment bound |
| `fire_record {fire_id, attempt, dispatched_at, result: delivered \| refused \| timeout}` | what was sent and what came back, or that nothing did | why a fire entered `UNKNOWN` |
| `reconcile_record {queried_at, source, result: landed \| not_landed \| unresolved}` | that a check preceded a re-dispatch | justifying the re-dispatch — the one easy to omit, exactly as in §5 of the modelling doc |
| `escalation_record {raised_at, channel, recipient_class}` | the system raised the alert | wait liveness — the guarantee is the alert, not the human's action |
| `heartbeat + timer_gap` | the monitor was alive, or was not, and which cases were affected | reconstructing an outage window afterwards |

## 14. Wiring into the graph — which spec rows become code

| Spec row | Today | After this plan |
| --- | --- | --- |
| `monitoring` on entry → `start_reassessment_timer` (13) | one audit line, no timer | writes a `timers` row; audit line unchanged |
| `monitoring` + `REASSESSMENT_TIMEOUT` ∨ `DETERIORATION_DETECTED` → `reassessment_required` (14) | unreachable; `State.REASSESSMENT_REQUIRED` is in `UNIMPLEMENTED_STATES` | a real node; state leaves `UNIMPLEMENTED_STATES` and `tests/test_edges.py` starts enforcing it |
| `reassessment_required` → `parsing` (15) | absent | re-entry into the front door, one nurse-re-filed submission |
| `awaiting_human_approval` + `GATE_TIMER_ASSIGNED_NURSE` (20a) | absent | ladder rung 1 — notify, no state change |
| `awaiting_human_approval` + `GATE_TIMER_ESCALATE_ANY_CHARGE` (20b) | absent | ladder rung 2 — widen to any charge nurse |
| Safety-fail parking SLA (`SYSTEM_MODELING.md` §2.5) | absent | a third timer kind on the parked case |

Two consequences for existing code worth flagging before the work starts: the board's
`reassessment_required` column gains its writer (milestone M3 of the board plan unblocks),
and `terminal.py::monitoring` stops being a terminal — it acquires an outgoing edge, which
is a change to the graph's shape and not just an added node.

## 15. Tests

Offline pytest, no network, no key, added to `triage-app`'s existing suite rather than a
fourth project's own — `tests/test_timers.py`, `tests/test_sweeper.py`.

- **timeout is not failure:** a dispatch that times out lands in `UNKNOWN`, never `FAILED`
- **no blind re-fire:** from `UNKNOWN`, the only reachable next states are `RECONCILING`
  and (budget spent) `ESCALATED_TO_HUMAN`; assert `DISPATCHING` is unreachable
- **reconcile → delivered:** a fire whose ack was lost but whose audit record exists
  resolves to `DELIVERED` and does **not** notify twice
- **reconcile → failed → re-dispatch** carries the *same* `fire_id`
- **idempotent acceptance:** delivering the same `fire_id` twice moves the case once
- **budget spent escalates out-of-band:** with the graph unreachable throughout, the
  escalation is still raised — the wait-liveness property holds through total control-plane
  failure
- **crash recovery:** kill the sweeper mid-`DISPATCHING`; on restart the lease expires into
  `RECONCILING`, not a re-dispatch
- **catch-up sweep:** N overdue timers after a simulated outage fire at the bounded rate,
  cases that moved on are `CANCELLED` not fired, and affected cases carry `timer_gap`
- **the clock does not re-sort:** advancing every timer changes no `order_key` (this one
  already exists in the board's suite and should be asserted here too, against the writer)
- **authority:** the monitor's own writes never touch `acuity`, `clinical_status`, or
  `order_key` — assert over the diff of case state across a fire
- **concurrent claim is race-safe (§6b):** two sweeper instances racing the claim
  statement on the same overdue row — exactly one succeeds, the other matches zero rows;
  never two dispatches for the same cycle

## 16. Milestones

| # | Deliverable | Depends on | Rough size |
| - | ----------- | ---------- | ---------- |
| **M0** | `timers` + `sweeper_heartbeats` tables in the shared store, `app/monitor/timers.py` (claim, lease), `app/monitor/sweeper.py` loop and heartbeat write. Fires nothing yet. | nothing | ~half day |
| **M1** | The fire state machine end to end: dispatch, ack, `UNKNOWN`, `RECONCILING`, `fire_id` de-dupe, budgets. Reassessment timer only. | M0 | ~2 days |
| **M2** | Graph side: `reassessment_required` node, arrows 14 / 15, `State.REASSESSMENT_REQUIRED` out of `UNIMPLEMENTED_STATES`. Board column M3 lights up. | M1 | ~1 day |
| **M3** | Gate ladder 20a / 20b + safety-fail parking SLA; notification rate limiting. | M1 | ~1 day |
| **M4** | Recovery sweep, `timer_gap`, `/api/heartbeat` + degraded banner on the board, the five metrics. | M1 | ~1 day |
| **M5** | Nurse-initiated deterioration endpoint on the board (§9). | M2 | ~half day |
| **M6** | Multi-replica sweeper (§6b): `worker_id`-scoped heartbeats, `busy_timeout` + retry on claim contention. Only if one sweeper process becomes a throughput or availability problem in practice — not scheduled by default. | M1 | ~half day |

M0 and M1 are unblocked today and depend on nothing in `STATUS.md` items 1–4. M0 is
smaller than the other three services' own skeleton milestones, because there is no
FastAPI app, no static UI, and no new deployment unit to stand up — it is a table, a
lease query, and a loop. **M4 is not optional polish** — without the recovery sweep and
the heartbeat, the monitor's silent failure mode is undetectable, which is worse than
not shipping it, because a monitor people believe in is a monitor they stop
double-checking.

## 17. Open questions — resolved

1. **Does the reassessment timer keep running while a case sits in `human_review`?**
   **Decision: yes, it keeps running, and the fire is deferred, not refused.** `order_key`
   already persists across state changes on the same reasoning — the patient is still
   physically waiting regardless of which queue they sit in. If the sweeper dispatches
   into a thread suspended at an `interrupt()`, the graph reports "not resumable right
   now" (a third ack outcome alongside `ack_applied` / `ack_refused`); the fire stays in
   `DISPATCHING` and is retried on the *same* `fire_id` once the thread resumes, rather than
   entering `UNKNOWN` or `FAILED` for a case the system knows perfectly well is fine. This
   is neither of the two evidence outcomes §4 already has — it is immediate, positive
   evidence that nothing can be decided yet — so it gets its own edge rather than being
   forced into `ack_refused`.
2. **Reassessment intervals per band** — clinical sign-off, per §8. **Decision: the
   Medical Director signs.** Tracked in `STATUS.md` § Four loose ends as pending; the
   placeholder values in `app/budgets.py` ship and run under a `# REQUIRES_SIGNOFF`
   marker so M0/M1 are not blocked on a governance meeting.
3. **Does a gate ladder rung count against `MAX_CORRECTION_ROUNDS`?** **Decision: no.** A
   reminder re-notifies; it does not re-attempt a correction. `SPECIFICATION.md`'s budget
   definition gets a one-line note that reminder dispatches are excluded from the count.
4. **What watches a case parked in `reassessment_required` that is never re-filed?**
   **Decision: the same ladder mechanism, as a third timer kind.** `kind='reassessment_reminder'`
   reuses the existing `timers` schema and fire state machine unchanged — no new
   mechanism, just a third row alongside `gate_reminder` and `safety_park` in §6's `kind`
   column, widening from the assigned nurse to any charge nurse on the same two-rung
   pattern as 20a → 20b.
5. **Terminal `ESCALATED_TO_HUMAN` — who exactly?** **Decision: split by cause, not by
   timer kind.** `escalation_record.recipient_class` (§13) is set from *why* the budget
   was spent: a clinical cause (deterioration, safety-park SLA, reassessment overdue)
   routes to the charge nurse; an infrastructure cause (store/graph unreachable through
   the whole reconcile budget) routes to the on-call technician. Both draw from the same
   `ESCALATED_TO_HUMAN` state — the split is in the payload, not a new state.
6. **Grace window for `timer_gap`.** **Decision: acuity-scaled, not one constant.** Same
   sourcing as the reassessment interval it shadows (§8: clinical policy, signed off with
   it, not measured) — an ESI-2 grace window is short, an ESI-5 one is longer, so the
   trail doesn't flag a gap for a patient it never mattered for and doesn't stay quiet for
   one it did.
7. **Write order on arrow 13.** **Decision: write the timer row *before* the node
   returns.** The alternative — writing after the checkpoint commits — reopens exactly the
   residual gap §3 describes as *rare*; writing first turns the failure mode into "timer
   row exists, case never reached `monitoring`," which is a new case the recovery sweep
   (§5b.3) must additionally treat as `CANCELLED` on catch-up, cheaper than a patient with
   no timer at all. §3's residual-gap paragraph and §5b.3's sweep both stand as written;
   this just settles which side of the gap is the covered one.
