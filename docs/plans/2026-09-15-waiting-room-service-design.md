# Waiting-room service — design and build plan

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

This plan builds it as the **fourth standalone service**, same shape as the other three:
its own `uv` project, FastAPI, a path dependency on `triage-app`, `uv run waiting-room`
on **:8003**.

```
crm-stub        :8000   patient history
intake-channel  :8001   the front door — one case in
board           :8002   the room — all cases out
waiting-room    :8003   the clock — nobody is forgotten in the room   ← this plan
triage-app              the control plane all three call into
```

The service is a **clock and a signaller**. It holds durable per-case timers, notices
when one comes due, and delivers an event into the graph. It never decides anything
clinical and never writes case state — the same authority split the board obeys, for the
same reason.

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
source for it today and this plan does not invent one), multi-replica sweepers, push
notification transports beyond the board's existing notification strip, per-nurse
routing rules.

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
2. **The forbidden transition is narrower.** `UNKNOWN → FIRING` is still forbidden. But
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

## 4. The fire state machine

One instance per **fire attempt**, not per timer. A timer that fires, is delivered, and
reschedules produces one machine per cycle.

### States

| State                | Meaning                                                                       | Final?  |
| -------------------- | ----------------------------------------------------------------------------- | ------- |
| `SCHEDULED`          | Timer row exists with a `due_at` in the future. Nothing to do.                | no      |
| `DUE`                | `due_at` has passed; claimed by the sweeper under a lease.                    | no      |
| `FIRING`             | Fire dispatched to the control plane, awaiting acknowledgement.               | no      |
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
| `dispatch`             | DUE → FIRING                     | Monitor sweeper     | —                                  |
| `ack_applied`          | FIRING → DELIVERED               | Flow (graph)        | the case's own state write         |
| `ack_refused`          | FIRING → FAILED                  | Flow (graph)        | `ACTION_DENIED` / closed case      |
| `dispatch_timeout`     | FIRING → UNKNOWN                 | Monitor sweeper     | its own timer                      |
| `reconcile_result`     | RECONCILING → DELIVERED / FAILED | Monitor sweeper     | checkpoint + audit log (`fire_id`) |
| `reconcile_unresolved` | RECONCILING → UNKNOWN            | Monitor sweeper     | — (store unreachable)              |
| `redispatch`           | FAILED → FIRING                  | Monitor sweeper     | its own attempt counter            |
| `budget_spent`         | FAILED / UNKNOWN → ESCALATED_TO_HUMAN | Monitor sweeper | its own attempt counter          |
| `cancel`               | any non-final → CANCELLED        | Monitor (on case event) | graph state (closed / re-filed) |

Same split as §4 of the modelling doc: everything derived from state the sweeper holds
(clock, attempt counter, reconciliation outcome) is the sweeper's to produce; only
`ack_applied` / `ack_refused` originate outside it, and they come from the Flow, which is
the component that actually applied the event.

### State owner

`fire_state` has a single owner: **the sweeper loop**, one writer, claiming rows under a
lease (§6). The graph never writes `fire_state`; it reports facts (the ack), exactly as
the Tool Gateway reports receipts and the single Flow step turns them into state. Two
sweepers writing the same row would race to fire the same timer twice, which is the
duplicate-notification hazard arriving through the back door.

### Forbidden transition

**`UNKNOWN → FIRING` directly.** From `UNKNOWN` the machine passes through `RECONCILING`;
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
  kind         TEXT NOT NULL,      -- reassessment | gate_reminder | safety_park
  cycle        INTEGER NOT NULL,   -- 0,1,2... per case+kind; the ladder rung
  due_at       TEXT NOT NULL,      -- ISO8601 UTC
  fire_state   TEXT NOT NULL,      -- §4
  fire_id      TEXT,               -- idempotency key of the current attempt
  attempts     INTEGER NOT NULL DEFAULT 0,
  lease_until  TEXT,               -- claim lease; NULL when unclaimed
  last_error   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX timers_due ON timers (fire_state, due_at);
```

**Claiming.** One statement, so the single-owner property survives a second process
started by accident:

```sql
UPDATE timers SET fire_state='DUE', lease_until=:now_plus_lease, updated_at=:now
 WHERE fire_state='SCHEDULED' AND due_at <= :now
   AND (lease_until IS NULL OR lease_until < :now)
 RETURNING timer_id, case_id, kind, cycle;
```

A lease that expires with the row still in `FIRING` is exactly the `UNKNOWN` case and is
routed into `RECONCILING`, not re-dispatched — the crash-mid-dispatch path and the
lost-ack path are the same path, which is the point of modelling it as absence of
evidence rather than as a kind of error.

**Clock.** UTC, from the store's own `now`, not the process clock, so a container with
skewed time cannot mass-fire or mass-delay. Monotonic sleep between sweeps; `due_at`
comparisons are wall-clock because the durations are clinical, not computational.

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

So `waiting_room/budgets.py` carries one table, each entry marked with which of the three
sources must set it, and the reassessment intervals additionally marked as requiring
sign-off rather than a measurement. Starting values exist only so the service runs; the
band structure (a level-2 interval shorter than a level-4 one) is the part that is
designed, the minutes are the part that is provisional.

Note the shape this shares with the other two undefined budgets in the project (machine
retry limit, human correction rounds): finite, escalates on exhaustion, decided from
evidence rather than feel. That is now three of them, and `STATUS.md` § Three loose ends
should gain the fourth line.

## 9. `DETERIORATION_DETECTED` has no source, and this plan does not pretend otherwise

The spec lists it as an internal event from this monitor carrying "patient id + signal".
There is no signal. Intake is a one-shot structured form (per the 2026-09-13 classifier
design), there is no vitals stream, no bedside monitor integration, and no nurse-facing
control that emits it. Inventing a fake detector here would put a clinical trigger behind
a stub, which is the one place in this system a stub is not acceptable.

**v1:** a single nurse-initiated endpoint —
`POST /api/case/{case_id}/deteriorated {signal, actor_role}` — that emits
`DETERIORATION_DETECTED` into the graph with the nurse as the evidence source. That is
honest (a human observed something), it exercises the whole path (arrow 14 → 15), and it
is what a real deployment would keep as a manual override even after a vitals feed exists.
The automatic detector stays an explicit hole, listed in §16.

## 10. API surface

```
GET  /                                the monitor's own small status page
GET  /api/timers                      every live timer + fire_state (ops view)
GET  /api/timers/{case_id}            one case's timers and history
GET  /api/health                      liveness of the process
GET  /api/heartbeat                   last sweep time + overdue backlog + gap count
POST /api/case/{case_id}/deteriorated nurse-initiated deterioration (§9)
POST /api/sweep                       force one sweep — dev/test only, never in prod path
```

The board consumes `/api/heartbeat` for its degraded banner and overdue counters. It does
not consume `/api/timers` per card: a card's timer state comes from the case state the
board already loads, so a dead monitor blanks a banner, never the board.

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

Offline pytest, no network, no key, mirroring the other three services.

- **timeout is not failure:** a dispatch that times out lands in `UNKNOWN`, never `FAILED`
- **no blind re-fire:** from `UNKNOWN`, the only reachable next states are `RECONCILING`
  and (budget spent) `ESCALATED_TO_HUMAN`; assert `FIRING` is unreachable
- **reconcile → delivered:** a fire whose ack was lost but whose audit record exists
  resolves to `DELIVERED` and does **not** notify twice
- **reconcile → failed → re-dispatch** carries the *same* `fire_id`
- **idempotent acceptance:** delivering the same `fire_id` twice moves the case once
- **budget spent escalates out-of-band:** with the graph unreachable throughout, the
  escalation is still raised — the wait-liveness property holds through total control-plane
  failure
- **crash recovery:** kill the sweeper mid-`FIRING`; on restart the lease expires into
  `RECONCILING`, not a re-dispatch
- **catch-up sweep:** N overdue timers after a simulated outage fire at the bounded rate,
  cases that moved on are `CANCELLED` not fired, and affected cases carry `timer_gap`
- **the clock does not re-sort:** advancing every timer changes no `order_key` (this one
  already exists in the board's suite and should be asserted here too, against the writer)
- **authority:** the monitor's own writes never touch `acuity`, `clinical_status`, or
  `order_key` — assert over the diff of case state across a fire

## 16. Milestones

| # | Deliverable | Depends on | Rough size |
| - | ----------- | ---------- | ---------- |
| **M0** | `waiting-room/` skeleton, `timers` table, sweeper loop with lease, heartbeat, `/api/health` + `/api/heartbeat`. Fires nothing yet. | nothing | ~1 day |
| **M1** | The fire state machine end to end: dispatch, ack, `UNKNOWN`, `RECONCILING`, `fire_id` de-dupe, budgets. Reassessment timer only. | M0 | ~2 days |
| **M2** | Graph side: `reassessment_required` node, arrows 14 / 15, `State.REASSESSMENT_REQUIRED` out of `UNIMPLEMENTED_STATES`. Board column M3 lights up. | M1 | ~1 day |
| **M3** | Gate ladder 20a / 20b + safety-fail parking SLA; notification rate limiting. | M1 | ~1 day |
| **M4** | Recovery sweep, `timer_gap`, board degraded banner, the five metrics. | M1 | ~1 day |
| **M5** | Nurse-initiated deterioration endpoint (§9). | M2 | ~half day |

M0 and M1 are unblocked today and depend on nothing in `STATUS.md` items 1–4. **M4 is not
optional polish** — without the recovery sweep and the heartbeat, the service's silent
failure mode is undetectable, which is worse than not shipping it, because a monitor
people believe in is a monitor they stop double-checking.

## 17. Open questions

1. **Does the reassessment timer keep running while a case sits in `human_review`?**
   `order_key` explicitly persists across state changes, and the patient is still waiting,
   so the argument for "yes, it keeps running" is strong — but that means a gated case can
   fire a reassessment into a thread that is suspended at an `interrupt()`. Decide the
   interaction before M1, because it decides whether the fire is deferred or refused.
2. **Reassessment intervals per band** — clinical sign-off, per §8. Who signs?
3. **Does a gate ladder rung count against `MAX_CORRECTION_ROUNDS`?** It should not (a
   reminder is not a correction attempt) but nothing says so today.
4. **What watches a case parked in `reassessment_required` that is never re-filed?**
   Arrow 15 needs a nurse; nothing bounds the wait for one. This is the same hole the gate
   ladder fills for `awaiting_human_approval`, and it probably needs the same ladder.
5. **Terminal `ESCALATED_TO_HUMAN` — who exactly?** Charge nurse for the clinical case,
   technician for the infrastructure case; the split is clear in the modelling doc's
   metric 1 and should be explicit in the notification payload.
6. **Grace window for `timer_gap`.** How overdue is "the system was not watching"? A
   thirty-second sweep delay is not a gap; a four-minute one for an ESI-2 patient is.
