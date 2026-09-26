# Triage Guard — where things stand

**Last updated:** 2026-09-25 (Langfuse traces now carry case, patient and outcome, and
the "Triage Guard" dashboard is seeded; treatment-move execution machine marked dropped,
per the 2026-09-19 decision; last full project scan 2026-09-24)

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
      should be a real policy engine — `app/deterministic.py:verify_no_identifiers`.
      Now also scans values: national ID, phone and email typed into free text are
      redacted from the model payload (`app/guards/identifiers.py`), and the verifier
      halts if any survive. Still regex, not a policy engine.
- [ ] the output checker — it validates the shape of what an agent returns, but not yet
      whether the values make sense together — `app/verification.py`

**One thing was deliberately dropped.** Not stubbed, not pending: decided against.

- [x] ~~**The treatment-move execution machine.**~~ **Won't build (decided 2026-09-19,
      recorded here 2026-09-25).** `docs/SYSTEM_MODELING.md` §4–5 describe a machine
      (Tool Gateway, idempotency key, `PENDING/CONFIRMED/FAILED/UNKNOWN`, reconcile
      before retry) whose only job is to cope with an external ward system that might
      never answer an order: did treatment start or not? This project has no such
      system and never will. Starting treatment is a nurse's click that moves the case
      to the `treatment_started` column, inside our own graph and database. That can't
      get lost in transit, so there's no `UNKNOWN` and nothing to reconcile. The real
      risks are already covered by **I5 No bypass** (no treatment without safety and
      any required approval) and **I6 Single treatment start** (at most once). The
      single-execution-writer invariant was dropped for the same reason. The design
      stays in `SYSTEM_MODELING.md`, marked "Future design", in case a real downstream
      system ever appears.

**Post-run trace check (2026-09-25).** `app/verification.py:check_trace` re-reads a
case's audit log and reports any break of I5 (no bypass), I6 (single treatment start),
I7 (correct, then revalidate), I8 (bounded correction loop), I18 (audit record
structure) or I20 (nothing changes after close), naming the exact record. It only
reports; the demo run prints it as `trace_safety`. Every open case view runs it now
too, not only the demo run: intake-channel's submit and resume responses and the
board's case detail panel all carry `trace_safety` and `trace_violations` on every
fetch, and both pages show a red banner naming each violation when one turns up.

**The waiting-room monitor is built.** `docs/plans/2026-09-15-waiting-room-service-design.md`
is now implemented, not just agreed: durable per-case timers, the sweeper that fires
them, and the failure/reconciliation model it was designed around
(`triage-app/app/monitor/`). It runs as its own process (`uv run sweeper`), separate
from any one `triage-guard` run or the other services. The VS Code "All services"
launch starts it (`.vscode/launch.json`); run separately, a case that reaches
`monitoring` with no sweeper running schedules a reassessment timer that never fires.
A dead sweeper shows as "monitor degraded" on the board (heartbeat, `board/api.py`).

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

      The two intake-channel tests once listed here as failing
      (`test_a_clean_submission_runs_the_whole_pipeline`,
      `test_a_charge_nurse_can_resolve_the_gate_and_the_case_completes`) pass as of
      2026-09-24.

- [x] **The board** that shows staff the current queue. *Built read-only (`board/`, :8002,
      milestones M0 + M1 of `2026-09-11-board-service-design.md`): it lists every case,
      sorts by the persisted `order_key`, shows queue position and the arrow trail. Five of
      its six columns have a writer today (`waiting`, `human_review`,
      `reassessment_required`, `treatment_started`, `patient_released` as of 2026-09-17);
      only `formal_validation` still renders empty, waiting on a `TREATMENT_COMPLETE`
      writer (see *What's left*). The two manual moves exist as a minimal version (2026-09-17, no Tool
      Gateway/idempotency/reconciliation) — see item 5c below.*

---

## What's left (scan 2026-09-24)

Every test suite is green: triage-app 368 (plus 1 deselected), board 50, intake-channel
48, crm-stub 15. Two triage-app tests (`test_a_crm_outage_degrades_and_continues`,
`test_clean_case_walks_the_documented_transitions_in_order`) fail only while the local
CRM stub is running on :8000, because they expect the CRM to be unreachable. With
`CRM_BASE_URL=http://127.0.0.1:1` they pass.
Done since the last update: symbolic engines in the waiting-room monitor (Prolog for
gate authorization, OPA for move/release, BPpy for dispatch/notify, Datalog for the
temporal sweeper pass), safety invariants I1–I18, release from any pause, the
correction loop escalating to a shift lead, and the Arrow → Transition rename.

**Core pieces still fake**

- [x] **Safety validator.** Six real rules over Prolog (`rules/safety.pl`, field
      contradictions) and Datalog (acuity provenance, required clinical fields),
      each failure carrying a reason that names the contradiction. Written up in
      SPECIFICATION.md § Safety validation rules. Deliberately holds no clinical
      rule: :727 reserves judgment for the nurse and the classifier. Fails closed.
- [ ] **Privacy check.** `app/deterministic.py:verify_no_identifiers` is a hardcoded
      list of five key names. Should be an OPA/Rego policy.
- [ ] **Output checker.** `app/verification.py` validates shape only, not whether the
      values make sense together.
- [ ] **Real LLM.** The acuity classifier defaults to the mock; live mode
      (`TRIAGE_LLM=live`) is written but needs a real run and a test that calls it.
- [x] **Z3.** Six design-time proofs in `app/symbolic/z3_proofs.py`: I4's bands
      (partition + the if-chain obeying them), I13's range half, and I1 three times
      (acuity order, the arrival tie-break, totality). Each has a mutation test that
      requires a counterexample, so no proof can pass by restating itself.

**Not built at all**

- [ ] **`formal_validation` board column** has no writer. Needs a "Treatment complete"
      board button that resumes the paused case, and a graph step that handles the
      `TREATMENT_COMPLETE` event (`app/events.py`, spec row `FV`) by moving
      `treatment_started` → `formal_validation`. Not blocked on anything. Planned for
      later. (The comment in `app/views.py` above `BOARD_COLUMNS` still says it waits on
      the execution machine. That's out of date.)
- [ ] **CRM write-back.** `patch_patient` exists in `app/crm_client.py` but nothing
      calls it; the deferred write-back and its reconciliation (I17) are missing.

**Spec holes and decisions**

- [ ] "Policy hit" in `escalation_needed` has no definition and is not wired
      (`app/graph/routers.py`).
- [ ] Correction scope: `CORRECTABLE_FIELDS = {"acuity"}` (`app/graph/nodes/gate.py`),
      but the spec also allows `clinical_status` and `safety_verdict`.
- [ ] Actor authorization checks role only. The spec (I14) also wants jurisdiction and
      data class, but no store holds shift, ward or clearance facts.
- [ ] The spec lags the code: the Waiting Room Monitor's tech is still "(to confirm)",
      and the `senior_reminder` / `reassessment_reminder` timers are missing from the
      state-variable table.
- [ ] The structured intake field list is still "roughly ten" — not specified.

**Placeholder numbers** (`app/budgets.py`) — retry budget, `MAX_CORRECTION_ROUNDS=3`,
`CONFIDENCE_THRESHOLD=0.70`, reminder timings, reassessment interval per acuity band.
See *Four loose ends* below.

**Housekeeping**

- [ ] Stale branches `feat-board`, `feat/langgraph-migration`, `langfuce`, `spec` —
      check whether each is merged, then delete.

---

## Recent changes

**2026-09-25 — Langfuse now shows each case, its patient and its outcome.** Before this,
every trace was called `LangGraph`, the Sessions page was empty, and there was no way to
find one patient's cases. Now:

- Each graph run on a case is one named trace: `case-start`, `case-resume`, `timer-fire`
  or `board-action`. The session id is the case id, so the Sessions page lists every
  case, and opening one shows its whole history in order.
- The patient appears as a user, but never under their real ID. The user id is a keyed
  hash such as `pt-0a96428e9c4b211a`, built with `TRIAGE_TRACE_SALT`. To find a
  patient, run `uv run python -m app.observability <patient id>` in `triage-app/`, then
  paste the result into the Users page or a filter. With no salt set, traces carry no
  user id at all.
- Tags and metadata record the operation, LLM mode (mock or live), intake channel and
  submission type. Environment and release come from `LANGFUSE_TRACING_ENVIRONMENT` and
  `LANGFUSE_RELEASE`. All of these can be filtered on.
- Nothing identifying leaves the process. Every input, output and metadata value passes
  through a mask that reuses `app/guards/identifiers.py` to redact national IDs, phone
  numbers and emails, including inside the LangGraph node spans and resume commands. In
  a live check, no raw ID reached Langfuse.
- Each trace's output lists the decisions that run made, taken from the audit log. It
  also gets five scores: `control_state` (where the case ended up), `acuity`,
  `acuity_gap` (nurse against model), `guardrail_blocks` (refusals added by this run
  only, so a refusal is not counted again on every later resume) and `trace_check`
  (whether the post-run safety check passed).
- A "Triage Guard" dashboard with 12 widgets comes with the project. It is defined in
  `langfuse/seed/triage-dashboard.sql` and loaded by a one-shot `langfuse-seed` service
  in `langfuse/docker-compose.yml`. The VS Code "All services" launch now starts
  Langfuse as well, so anyone who clones the repo gets the dashboard on first launch.
  The seed runs on every launch and restores the dashboard if it was deleted. The
  catch is that edits made to it in the UI are overwritten, so lasting changes belong
  in the SQL file.
- The same seed adds three saved views to the Views dropdown on the Tracing page:
  "Trace-check failures", "Guardrail blocks" and "Live LLM runs"
  (`langfuse/seed/triage-views.sql`). Each shows one row per case run. Langfuse's own
  "Errors Only" view covers failing steps. Looking up one case or one patient is done
  on the Sessions and Users pages, since those need a different id every time.
- A new skill, `skills/reset-langfuse-data`, wipes all Langfuse data for a clean demo.
  The API keys keep working afterwards.

Three new keys go in `.env` (repo root, and `triage-app/.env` if you keep one). Both
`.env.example` files list them. An existing `.env` does not pick them up by itself.

| Key | Example | What it does |
|---|---|---|
| `TRIAGE_TRACE_SALT` | a long random string (`openssl rand -hex 32`) | Secret key for the patient hash. Keep it stable, because changing it splits every patient's history in two. Leave it unset and traces carry no patient. |
| `LANGFUSE_TRACING_ENVIRONMENT` | `dev` | Shown as Environment in Langfuse, so dev runs can be filtered away from demo runs. |
| `LANGFUSE_RELEASE` | `triage-guard-0.1.0` | Version stamped on every trace. Bump it when prompts or the graph change. |

The dashboard seed also needs `LANGFUSE_INIT_PROJECT_ID` in `langfuse/.env`, which
`langfuse/.env.example` already sets to `triage-guard`. Without it the seed skips
itself and logs why.

The code is in `triage-app/app/observability.py` (tests in
`triage-app/tests/test_observability.py`), and each graph invoke in `app/runner.py`,
`app/monitor/fire.py` and `board/board/api.py` is wrapped in it. intake-channel's old
`intake-submission` span is gone, since the case trace replaces it. How to search,
filter and read the dashboard is in `langfuse/README.md`. The plan and review notes are
in `docs/superpowers/plans/2026-09-25-langfuse-organization/`.

The review's loose ends are closed too:

- Error text is masked. A failing node's error message used to reach Langfuse as is,
  because the mask only covers input, output and metadata. The node spans and the case
  run's root span now redact it the same way. The code that called the graph still gets
  the original error.
- Services no longer flush Langfuse on every request. The board and intake-channel did,
  which added a network call to each click. The SDK already sends data in the
  background and flushes when the process exits. The sweeper now treats a stop signal
  as a normal exit, so its last spans are sent too.
- The mask still redacts any run of nine digits, even one that isn't an ID. That is
  deliberate: a nurse can type an ID with no spaces around it, and a hidden number in
  telemetry costs far less than a leaked one. It only affects what Langfuse shows;
  the case data is untouched.

**2026-09-18 — SQLite is gone; one shared Postgres backs checkpoints, timers,
and the CRM stub.** `db/docker-compose.yml` (new) runs Postgres 17 on host port
5434 (5432 is already taken by a native `postgresql.service`), creating two
databases: `triage` (checkpoints + timers, via `db/init/`) and `crm` (patient
records). `triage-app/app/runner.py` swaps `SqliteSaver` for LangGraph's
`PostgresSaver` (`TRIAGE_CHECKPOINT_DB` is now a DSN, not a file path);
`triage-app/app/monitor/timers.py` points its own connection at the same
database (autocommit, 5s `lock_timeout` so a stuck row degrades the board
instead of hanging it) and its schema swapped SQLite-isms
(`strftime`/`AUTOINCREMENT`) for Postgres equivalents (`to_char`/`SERIAL`).
`board/board/repo.py`'s read-only enumeration does the same swap, catching
`UndefinedTable`/`OperationalError` instead of `sqlite3.OperationalError` for
the empty-board case. `crm-stub` moved off its own `patients.db` file (deleted)
to the shared server's `crm` database via `psycopg`; its `docker-compose.yml`
reaches the host's Postgres through `host.docker.internal:5434`. No behavior
change to any of the graph, monitor, or CRM contract logic — this is a storage
swap, same reads/writes, same schemas otherwise. `.vscode/tasks.json` (new)
and a `launch.json` tweak wire up `docker compose -f db/docker-compose.yml up`
as a launchable task.

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

The treatment-move execution machine, once the one piece that needed its own design
conversation, is off the list: there's no downstream ward system for it to guard (see
*Where things stand*). Starting treatment stays a guarded column change (I5, I6).

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
- [x] ~~**4. Design and build the treatment-move machine.**~~ Dropped, decided
      2026-09-19: no downstream ward system exists. See *Where things stand*.
- [ ] **5a.** The waiting-room timers. Design agreed
      (`docs/plans/2026-09-15-waiting-room-service-design.md`); M0/M1 unblocked today.
- [x] **5b.** The release / discharge step. **Minimal version, 2026-09-17:** a charge
      nurse can release a case (any reason of discharge/ama/transfer/admit) via
      `release_authorized`, re-entering the waiting-room pause with `Command(resume=...)`
      — no Tool Gateway, no idempotency key, no `PENDING/CONFIRMED/FAILED/UNKNOWN`
      execution states, no reconciliation. Those exist to protect against a downstream
      hospital system this project doesn't have, which is why item 4 was dropped. Only
      wired from the waiting-room pause, not the human-approval gate or the reassessment
      re-file pause — see
      `docs/superpowers/plans/2026-09-17-treatment-move-and-release/findings.md`.
- [x] **5c.** The board. Read-only board built (`board/`, :8002). Its write half —
      `POST /api/case/{case_id}/move-to-treatment` and `/release` — now exists (2026-09-17,
      minimal version, see item 5b above), re-entering the paused graph run rather than
      writing case state directly. This is the final shape, not a stopgap: item 4 was
      dropped.

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
