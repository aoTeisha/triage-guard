# Triage Guard — where things stand

**Last updated:** 2026-09-15 (waiting-room monitor built — see *Recent changes*)

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
- [ ] ~~the text scorer that reads distress and pain from the patient's own words~~ —
      `app/actors/normalizer.py:score_urgency`. **Switched off, 2026-09-13.** See
      *Recent changes*.
- [ ] the privacy check — currently a hardcoded list of forbidden field names, where it
      should be a real policy engine — `app/deterministic.py:verify_no_identifiers`
- [ ] the output checker — it validates the shape of what an agent returns, but not yet
      whether the values make sense together — `app/verification.py`

**One thing doesn't exist at all.** Not stubbed — simply absent.

- [ ] **The treatment move.** What happens when the system actually moves a patient into
      treatment. `docs/SYSTEM_MODELING.md` spends its entire second half on this: what
      if you send the instruction and never hear back? Did the move happen or not? The
      docs are emphatic that you must *check* before trying again, because retrying
      blindly could start treatment on the same patient twice. There is currently no
      code for any of it.

**The waiting-room monitor is built.** `docs/plans/2026-09-15-waiting-room-service-design.md`
is now implemented, not just agreed: durable per-case timers, the sweeper that fires
them, and the failure/reconciliation model it was designed around
(`triage-app/app/monitor/`). It runs as its own process (`uv run sweeper`), separate
from any one `triage-guard` run or the other services — nothing starts it
automatically, so a case that reaches `monitoring` and never gets a sweeper running
alongside it will schedule a reassessment timer that never fires.

- [ ] **Known gap: reassessment skips the nurse.** The spec says a fired reassessment
      timer should pause the case until a nurse re-files it with fresh observations
      (`docs/SPECIFICATION.md:273,486,533` — only that new submission may change
      acuity). The code doesn't do that yet: `reassessment_required`
      (`triage-app/app/graph/nodes/reassessment.py:9-11`, marked `ponytail:`) just
      replays the same stale `raw_payload` straight back through parsing, so a fired
      timer almost always re-triages a patient on hours-old data with nobody actually
      looking at them again. That's why `REASSESSMENT REQUIRED` on the board is always
      empty — the case passes through it instantly and lands back in the queue. Needs
      a real nurse re-filing endpoint (same shape as the board's existing
      `/deteriorated`) before this is clinically real.

- [x] **The board** that shows staff the current queue. *Built read-only (`board/`, :8002,
      milestones M0 + M1 of `2026-09-11-board-service-design.md`): it lists every case,
      sorts by the persisted `order_key`, shows queue position and the arrow trail. Three of
      its six columns have a writer today (`waiting`, `human_review`,
      `reassessment_required`); the rest wait on items 4 / 5a / 5b below. The
      two manual moves (M2) are deliberately not built — see item 4.*

---

## Recent changes

**2026-09-15 — the waiting-room monitor has a design.** Agreed, not yet built:
`docs/plans/2026-09-15-waiting-room-service-design.md`. Runs inside `triage-app` —
the sixth agent the spec's own Actors table already names, plus a small worker
process (`app/sweeper.py`), not a fourth standalone service. A network boundary was
considered and rejected: writing a timer row and moving a case into `monitoring` has
to land as one transaction, and a service call in between reopens the "nobody is
watching this patient" gap the design exists to close. It holds durable per-case
timers — the reassessment timer (13, 14, 15), the approval-gate reminder ladder
(20a, 20b), and the safety-fail parking SLA. Its core is the failure model: a fire
whose acknowledgement is lost goes to `UNKNOWN`, never `FAILED`, and is reconciled
against the checkpoint and audit log before any re-fire — the same discipline
`SYSTEM_MODELING.md` builds for the treatment move, applied to a second machine. It
differs in one deliberate way: under uncertainty the treatment move resolves toward
inaction, the waiting room toward escalation, because the hazards point in opposite
directions. The plan also names the monitor's worst failure — dying silently, since
nobody calls it — and makes it detectable (durable timers, dead-man's-switch
heartbeat, bounded catch-up sweep, `timer_gap` flags), with the schema already
shaped to add more than one sweeper process later without a rewrite.

**2026-09-14 — intake-channel UI got an accessibility and styling pass.** Focus-visible
outlines, viewport meta tag, design-token colors, dark-mode-ready structure. No behavior
change — same endpoints, same form fields. `board/board/seed_demo.py` (unused demo-data
script) was also deleted; README updated to match.

**2026-09-14 — board merged to dev.** `feat-board` branch merged in
(`70aa380`): read-only board (`board/`, :8002) covering M0 + M1 of
`2026-09-11-board-service-design.md` — see *Where things stand* above for what
it does and doesn't do yet.

**2026-09-13 — the urgency scorer is switched off.** Intake isn't going to carry a
free-text field, so there was nothing for the scorer to read. It was also the only
piece whose output nobody used: it produced three numbers — sentiment, distress, pain —
that got saved and then ignored by every step after it.

What changed, and how to undo it:

- `app/graph/nodes/redaction.py` no longer calls the scorer. The old call is commented
  out in place, not deleted.
- `app/actors/normalizer.py:score_urgency` is untouched and still works; nothing calls it.
- One test is skipped (`tests/test_failures.py`), the one covering what happens when the
  scorer breaks.

Bringing it back is un-commenting one block and removing one line. Everything else is
where it was.

**2026-09-13 - the acuity classifier has a design.** Agreed, not yet built. Intake
becomes structured only (about ten dropdowns and tick-boxes plus vitals, no prose box).
The model reads the case and proposes an ESI level on its own; one deterministic rule
runs after it and can raise a case to emergent when the vitals are in the danger zone.
The hand-written red-flag list is retired - it was brittle enough to miss its own demo
case. A disagreement of one level between nurse and model now settles to the nurse's
number rather than the more urgent one. Full reasoning, evidence and open items:
`docs/plans/2026-09-13-acuity-classifier-design.md`. The spec has been updated to match.

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
- [ ] **5a.** The waiting-room timers. Design agreed
      (`docs/plans/2026-09-15-waiting-room-service-design.md`); M0/M1 unblocked today.
- [ ] **5b.** The release / discharge step.
- [x] **5c.** The board. Read-only board built (`board/`, :8002). Its write half —
      `POST /move` and `/release` — stays blocked on item 4, so that the treatment-move
      machine does not get improvised inside a UI.

---

## Four loose ends

Four numbers are currently undecided. Three are educated guesses, clearly marked as such
in the code (`app/budgets.py`):

- [ ] how many times to retry a failing component
- [ ] how many times a nurse can correct a case before it escalates
- [ ] how unsure the AI has to be before a human double-checks it (currently 0.70)
- [ ] the reassessment interval per acuity band — unlike the three above this one is a
      *clinical policy* decision, not a measurement, so it needs sign-off rather than data
      (`docs/plans/2026-09-15-waiting-room-service-design.md` §8)

None of these block anything. The first three need real data before going live, and that
data can't be gathered until step 2. The fourth needs a clinician, not a measurement.

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
