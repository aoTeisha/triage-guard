# Triage Guard — where things stand

**Last updated:** 2026-09-17 (a nurse can move a patient to treatment and release them, from the board — see *Recent changes*)

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

- [x] **Reassessment now genuinely waits for the nurse.** Was: a fired reassessment
      timer replayed the same stale `raw_payload` straight through parsing, so nobody
      actually looked at the patient again. Fixed, 2026-09-16: `reassessment_required`
      commits and starts a reminder timer, then a new pause node
      (`awaiting_reassessment_submission`, `triage-app/app/graph/nodes/reassessment.py`)
      genuinely freezes the case until a nurse submits fresh vitals and a chief
      complaint. `POST /reassess/{case_id}` (`intake-channel/channel/api.py`) answers
      it; the board's case panel has a form that calls it directly. `REASSESSMENT
      REQUIRED` on the board now actually populates (`clinical_status` is written on
      entry, which it wasn't before) and a case parked there for 15 minutes with no
      re-file nudges a charge nurse (`reassessment_reminder` timer,
      one rung — see `docs/plans/2026-09-15-waiting-room-service-design.md` §17 Q4's
      2026-09-16 amendment for why not two). `/resume/{case_id}` also gained a guard it
      was missing before this: it now refuses a case parked at the re-filing pause
      instead of silently corrupting it.

      Known, unrelated: `intake-channel/tests/test_api.py::test_a_clean_submission_runs_the_whole_pipeline`
      and `::test_a_charge_nurse_can_resolve_the_gate_and_the_case_completes` fail today
      because the already-merged waiting-room monitor made `/submit`'s reported
      `status` become `"awaiting_human_approval"` for *any* pending pause, not just a
      human gate — `app/views.py`'s `case_view()` never got updated for that. Predates
      this fix, no file it touched overlaps. Needs its own decision (what should a
      non-human waiting-room pause report as `status`?) before those two tests can be
      fixed correctly rather than papered over.

- [x] **The board** that shows staff the current queue. *Built read-only (`board/`, :8002,
      milestones M0 + M1 of `2026-09-11-board-service-design.md`): it lists every case,
      sorts by the persisted `order_key`, shows queue position and the arrow trail. Five of
      its six columns have a writer today (`waiting`, `human_review`,
      `reassessment_required`, `treatment_started`, `patient_released` as of 2026-09-17);
      only `formal_validation` still renders empty, waiting on item 4's full execution
      machine. The two manual moves exist as a minimal version (2026-09-17, no Tool
      Gateway/idempotency/reconciliation) — see item 5c below.*

---

## Recent changes

**2026-09-17 — a nurse can move a waiting patient into treatment, and release
them, from the board.** Minimal version, wired only from the waiting-room pause
(`awaiting_reassessment`) — see *Where things stand* above and
`docs/superpowers/plans/2026-09-17-treatment-move-and-release/findings.md` for
why the human-approval gate and the reassessment re-file pause are out of
scope here. Two new board endpoints (`POST /api/case/{case_id}/move-to-treatment`,
`POST /api/case/{case_id}/release`) re-enter a case's paused LangGraph run with
`Command(resume=...)` — the same mechanism intake-channel's own `/resume` uses
— rather than writing case state directly; the in-graph guards
`move_authorized` / `release_authorized` (`app/deterministic.py`) do the actual
authorization check, and a refusal is a normal 200 response
(`status: "denied"`), not an HTTP error. `board.js` gained two new buttons
("Start treatment", "Release patient") in the case detail panel. Full
design/build notes, scope cuts, and the post-implementation review rounds:
`docs/superpowers/plans/2026-09-17-treatment-move-and-release/`.

**2026-09-16 — reassessment genuinely waits for a nurse to re-file.** Closes the
"reassessment skips the nurse" gap this file used to list above. `reassessment_required`
(`triage-app/app/graph/nodes/reassessment.py`) is now split into a commit node and a
real pause (`awaiting_reassessment_submission`), the same two-node shape
`monitoring`/`awaiting_reassessment` already used — a fired timer no longer replays
stale intake data. `POST /reassess/{case_id}` in `intake-channel` answers the pause; a
form on the board's case detail panel calls it directly (cross-origin, one CORS
allow-list entry added to `intake-channel/channel/api.py`). A `reassessment_reminder`
timer (one rung, straight to any charge nurse — see the 2026-09-16 amendment to
`docs/plans/2026-09-15-waiting-room-service-design.md` §17 Q4) nudges staff if nobody
re-files. `/resume/{case_id}` also picked up a guard it was missing: it now refuses a
case parked at the re-filing pause instead of silently corrupting it, a gap this
change exposed in code nobody had touched otherwise. Full design/build notes:
`docs/superpowers/plans/2026-09-16-reassessment-nurse-refiling/`.

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
- [x] **5b.** The release / discharge step. **Minimal version, 2026-09-17:** a charge
      nurse can release a case (any reason of discharge/ama/transfer/admit) via
      `release_authorized`, re-entering the waiting-room pause with `Command(resume=...)`
      — no Tool Gateway, no idempotency key, no `PENDING/CONFIRMED/FAILED/UNKNOWN`
      execution states, no reconciliation. Those exist to protect against a downstream
      hospital system this project doesn't have; item 4's full execution machine below is
      still not built. Only wired from the waiting-room pause, not the human-approval gate
      or the reassessment re-file pause — see
      `docs/superpowers/plans/2026-09-17-treatment-move-and-release/findings.md`.
- [x] **5c.** The board. Read-only board built (`board/`, :8002). Its write half —
      `POST /api/case/{case_id}/move-to-treatment` and `/release` — now exists (2026-09-17,
      minimal version, see item 5b above), re-entering the paused graph run rather than
      writing case state directly. Item 4's full treatment-move execution machine is still
      not built.

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
