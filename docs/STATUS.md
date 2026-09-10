# Triage Guard — where things stand

**Last updated:** 2026-09-10 (after the LangGraph migration, branch `feat/langgraph-migration`)

Yes, you can start building for real. The mocked parts are each one function you can
replace on their own. But three things in your docs have no code behind them yet, and
one of them is important enough that it shouldn't surprise you later.

---

## Where things stand

Think of the project as three layers.

**The plumbing is finished.** The thing that decides what happens next, in what order,
and what's allowed — that's done and tested. Cases flow through it. The four intake
outcomes work. The pause for a charge nurse genuinely pauses and genuinely resumes.
Refusing an unauthorised action genuinely refuses. The audit trail is real. You won't
need to rebuild any of this.

**Five things are fake, but only on the inside.** Each one is a single function that
returns pretend data. Swapping any of them for the real thing means editing that one
function — nothing else in the project changes. That's the whole point of how it's
built, and it's the main reason we're ready.

Those five:

- [ ] the AI that proposes an acuity level (the code to call a real model is written,
      it's just never been run with a key) — `app/actors/acuity_classifier.py`
- [ ] the safety validator — right now it reads "pass" out of a file instead of
      actually checking anything — `app/actors/safety.py`
- [ ] the text scorer that reads distress and pain from the patient's own words —
      `app/actors/normalizer.py:score_urgency`
- [ ] the privacy check — currently a hardcoded list of forbidden field names, where it
      should be a real policy engine — `app/deterministic.py:verify_no_identifiers`
- [ ] the output checker — it validates the shape of what an agent returns, but not yet
      whether the values make sense together — `app/verification.py`

**Three things don't exist at all.** Not stubbed — simply absent.

- [ ] **The treatment move.** What happens when the system actually moves a patient into
      treatment. `docs/SYSTEM_MODELING.md` spends its entire second half on this: what
      if you send the instruction and never hear back? Did the move happen or not? The
      docs are emphatic that you must *check* before trying again, because retrying
      blindly could start treatment on the same patient twice. There is currently no
      code for any of it.
- [ ] **The waiting-room timers** that flag a patient who's been waiting too long.
- [ ] **The board** that shows staff the current queue.

---

## Are we ready to build properly?

Yes — with one thing worth saying plainly.

Replacing the five fake pieces is straightforward work. The place each one plugs into
already exists and is already tested from both sides. Work can start tomorrow and
nothing needs redesigning first.

But the treatment-move piece is different in kind. It isn't filling in a blank — it's a
second machine with its own states and its own rules, and it's the one place where an
ordinary bug becomes a patient-safety problem. It deserves the same kind of design
conversation we just had about LangGraph, before anyone writes code. The risk is that it
gets treated as a leftover and discovered late.

---

## What to do, in order

- [ ] **1. Make the safety validator real.** This is the biggest gap between what the
      documents promise and what the system does. Right now the sentence "the model
      proposes, the symbolic layer decides" isn't true — the symbolic layer is a text
      file that always says yes. Everything around it is ready and waiting.
- [ ] **2. Turn on the real AI model.** Smallest job on the list. The code is written;
      it needs an API key and one test that actually calls it.
- [ ] **3. Make the privacy and output checks real.** Related to step 1, same kind of
      work.
- [ ] **4. Design and build the treatment-move machine.** Its own project. Design first.
- [ ] **5a.** The waiting-room timers.
- [ ] **5b.** The release / discharge step.
- [ ] **5c.** The board.

---

## Three loose ends

Three numbers are currently educated guesses, clearly marked as such in the code
(`app/budgets.py`):

- [ ] how many times to retry a failing component
- [ ] how many times a nurse can correct a case before it escalates
- [ ] how unsure the AI has to be before a human double-checks it (currently 0.70)

None of these block anything. All three need real data before going live, and that data
can't be gathered until step 2.

There's also one genuine hole in the spec itself:

- [ ] `escalation_needed` mentions a "policy hit" as a reason to involve a human, but
      nothing anywhere defines what a policy hit *is*. Worth deciding at some point.

---

## Suggested next step

**Start with the safety validator.** It's the most valuable single thing left, nothing
needs designing first, and finishing it makes the central claim about the system
actually true.

The open question for that piece: which checks belong to which engine (OPA / Z3 /
Prolog / Datalog), and what rules it actually needs to enforce.
