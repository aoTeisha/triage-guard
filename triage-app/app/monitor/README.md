# The waiting-room monitor — how it works

This explains the code in `app/monitor/` plus the two graph nodes it talks to
(`app/graph/nodes/terminal.py` and `app/graph/nodes/gate.py`). Plain language,
no jargon you haven't seen already in this file.

## What problem this solves

Once a patient is triaged and put in the queue, someone has to make sure they
get looked at again after a while (a "reassessment"). Nobody is sitting there
watching a clock for every patient — a background process has to do it.

That background process is the **monitor**. It's really just three things:

1. A **timer store** — a database table of "check on patient X again at time Y."
2. A **sweeper** — a loop that wakes up every few seconds and asks "which timers
  are due right now?"
3. A **fire state machine** — the logic that decides what "due" actually means:
  did the timer's event really reach the patient's case, or did something go
   wrong and it needs to be retried?



## The three files

```
app/monitor/timers.py    the database table + basic reads/writes
app/monitor/fire.py      "a timer is due — now what?" logic
app/monitor/sweeper.py   the loop that ties the two together
```

`timers.py` doesn't know anything about patients, graphs, or reassessment.
It just knows how to store rows like `{case_id, kind, due_at, fire_state}` and
how to safely say "give me every row that's overdue" without two different
processes grabbing the same row at once.

`fire.py` is where the actual decisions live: "this timer fired, but is the
case still waiting? did the event actually get delivered? should we try again?"

`sweeper.py` is the dumb loop: every few seconds, ask `timers.py` what's
due, hand each one to `fire.py`, repeat forever. `uv run sweeper` runs this as
its own process, separate from the main app.

Tests for all three live in `tests/monitor/` (`test_timers.py`, `test_fire.py`,
`test_sweeper.py`), same split as the code.

## The key idea: the case is "paused," not "finished"

Before this code existed, once a case reached the queue (`monitoring`), the
graph run was just... over. Nothing was watching it anymore.

Now, reaching the queue means the graph **pauses and waits** — like a phone call
on hold, not a phone call that ended. It sits there, checkpointed, until
someone calls back with an event: either the sweeper says "your reassessment
timer went off" or a nurse says "this patient got worse."

This pause is implemented with LangGraph's `interrupt()`. When a node calls
`interrupt()`, the whole run freezes and gets saved. Later, someone calls
`graph.invoke(Command(resume=some_data), config)` on that exact same case, and
the run picks back up with `some_data` as the answer to whatever it was
waiting for.

## Walking through one reassessment, start to finish

1. A case gets triaged and clears to the queue. The `monitoring` node runs:
  it writes "cleared to queue" to the audit trail, and calls
   `timers.schedule(...)` to insert a row saying "check this case again in
   30 minutes" (the exact number depends on how urgent the patient is).
2. The very next node, `awaiting_reassessment`, calls `interrupt()`. The run
  freezes. The case just sits in the queue, "paused," for however long.
3. Meanwhile, `uv run sweeper` is looping in the background. Every 5 seconds
  it asks the timers table: "anything due?" Once 30 minutes pass, that row
   shows up.
4. The sweeper claims the row (so no other sweeper process also grabs it) and
  hands it to `fire.dispatch()`.
5. `fire.dispatch()` calls `graph.invoke(Command(resume={"event":
  "REASSESSMENT_TIMEOUT", ...}), config)`on that case. This "wakes up" the  frozen`awaiting_reassessment` node with the event as its answer.
6. The case moves on: `reassessment_required` → back to `parsing`, and the
  whole pipeline re-runs on the case (in this skeleton, it just re-checks the
   same intake data — a real nurse re-filing new data is a future feature).
7. If the patient is still fine, the case clears to the queue again, and a
  brand new timer gets scheduled for the *next* reassessment. This loops for
   as long as the patient is waiting.

That's the whole cycle. The interesting part is everything that can go wrong
along the way.

## What can go wrong, and the fire states

A "fire state machine" is just a fancy way of saying: every timer has a
status, and there are rules for which status can become which other status.


| Status               | Plain meaning                                                                                |
| -------------------- | -------------------------------------------------------------------------------------------- |
| `SCHEDULED`          | Waiting for its time to come.                                                                |
| `DUE`                | Its time came; the sweeper picked it up.                                                     |
| `DISPATCHING`             | The sweeper is actively trying to deliver the event right now.                               |
| `DELIVERED`          | It worked. Done.                                                                             |
| `FAILED`             | We got a clear "no" — the case is closed, or doesn't exist. Safe to retry later, or give up. |
| `UNKNOWN`            | We don't know if it worked. The connection could have died mid-attempt.                      |
| `RECONCILING`        | We're re-checking the case's own history to find out what actually happened.                 |
| `ESCALATED_TO_HUMAN` | We tried and checked enough times and still don't know — page a human.                       |
| `CANCELLED`          | The reminder doesn't matter anymore (only used for gate reminders, see below).               |


The one rule that matters most: `UNKNOWN` **never jumps straight back to**
`DISPATCHING`**.** If we're not sure whether something happened, we don't just try
again blindly — a second, unnecessary delivery could re-notify a nurse twice,
or worse. Instead we go through `RECONCILING` first: read the case's own audit
trail and check "does it already show this exact event landed?" Only if the
answer is a clear "no" do we retry.

This is why every event carries a `fire_id` — a fingerprint made from
`(case_id, timer kind, cycle, due_at)`. It's how reconciliation can search the
audit trail for "did *this specific* fire already happen" instead of just
"did *a* reassessment happen."

## The gate reminder ladder (a simpler, different case)

There's a second kind of timer: gate reminders. When a case is sitting at the
human-approval gate waiting for a charge nurse to decide something, two
reminder timers get scheduled at the same time: one that pings the assigned
nurse after 10 minutes, another that widens to *any* charge nurse after 20.

These are simpler than reassessment timers because **they don't change
anything about the case** — they just send a notification. So they skip the
whole `DISPATCHING`/`UNKNOWN`/`RECONCILING` dance entirely. `fire.notify()` just
checks "is the gate still open?" — if yes, send the notification (once per
reason, and only if we haven't already sent too many to this recipient
recently); if no, mark it `CANCELLED`, since the nurse already answered and a
late reminder would be pointless.

## The heartbeat: watching the watcher

If the sweeper process itself crashes, nothing calls it to tell it something's
wrong — it just silently stops working, and every patient's reassessment
timer quietly stops firing. That's the scariest failure mode here, because
nothing *looks* broken.

The fix: every tick, the sweeper writes a "heartbeat" row — basically a
timestamp saying "I'm alive as of right now." Something *else* (the board,
via `/api/heartbeat`) watches that timestamp from the outside. If it gets too
old (older than a few sweep intervals), the board shows a "monitor degraded"
banner. A monitor that could report its own death wouldn't actually be dead,
so this has to be checked from outside, not by the monitor itself.

## Two bugs we actually hit while building this (worth knowing about)

**The cycle number.** Every timer's ID is built from
`case_id:kind:cycle`. The first version of this code always used `cycle=0`,
which meant after the *first* reassessment fired, scheduling the *next* one
reused the exact same ID and the exact same due-time forever — the timer
would never actually move forward. Fix: `reassessment_cycle` is a number
stored on the case itself, incremented every time a reassessment fires.

**Matching on "no fingerprint."** The duplicate-detection check was
`rec.get("fire_id") == fire_id`. A nurse-reported "patient got worse" event
has no `fire_id` (a human isn't a timer). But plenty of *other*, unrelated
audit rows also have no `fire_id` — so `None == None` made the code think a
brand new deterioration report was a duplicate of some random earlier row,
and silently dropped it. Fix: only compare fingerprints when there actually
is one to compare (`if fire_id and ...`).

Both are the kind of bug that only shows up once you actually run the cycle
more than once — a good reminder that "it worked in the first test" and "it
works" are different claims.