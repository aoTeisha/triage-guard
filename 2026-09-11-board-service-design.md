# Board service — design and build plan

**Date:** 2026-09-11
**Status:** proposal, not yet built
**Closes:** `docs/STATUS.md` item **5c — "The board that shows staff the current queue."**

---

## 1. What this is

A nurse-facing kanban board: one card per live case, one column per World-plane
`clinical_status`, sorted by the `order_key` the control plane already assigned.
Click a card → detail panel with the case's state, its arrow trail, and the two
status changes a nurse is allowed to make.

It is the **third** standalone service in the repo, alongside `crm-stub` (:8000)
and `intake-channel` (:8001). Same shape: its own `uv` project, FastAPI, a path
dependency on `triage-app`, static front-end, `uv run board` on **:8002**.

```
crm-stub        :8000   patient history
intake-channel  :8001   the front door — one case in
board           :8002   the room — all cases out        ← this plan
triage-app              the control plane both call into
```

## 2. Scope

**In:** read the persisted state of every case, group by `clinical_status`, sort by
`order_key`, show queue position, show the audit trail per case, and issue the two
manual transitions the spec permits (`treatment_started`, release).

**Out:** the board never computes triage. It never writes `TriageState` directly, never
computes or re-computes `order_key`, never decides acuity. Every write goes through the
graph, exactly as `intake-channel` does. The board is a *view* plus two *requests*.

**Non-goals for v1:** multi-user auth, real-time push, the treatment-move execution
machine (`SYSTEM_MODELING.md` second half — its own project), real timers.

## 2b. Storage decisions (settled)

**The CRM stays a separate service with its own database.** `crm-stub` keeps its own
SQLite file and its own container, reached only over HTTP. Not merged into the triage
store, now or when it is replaced by a real CRM. Three reasons, in order of weight: the
`db_error` degrade path stops being simulatable the moment the CRM cannot be down
independently; the contract seam is what lets a real hospital CRM drop in unchanged; and
identifiers stay behind one narrow gate, which is what makes `verify_no_identifiers`
defensible. No query, ever, spans both stores.

**Everything inside the triage system shares one store.** Checkpoints, the board
projection, the reassessment timers (5a) and the treatment-move outbox (4) live together
— SQLite now, Postgres when it hurts. The board card and the case state must be written
in one transaction; split them and there is a window where a card shows a status the case
has already left. On a clinical board that is not a display bug.

**The board never calls the CRM.** A card needs a human-readable patient label, and the
naive route is one CRM call per card per refresh — dozens of calls every five seconds, and
a board that goes blank when the CRM does. Instead: `resolving_identity` already fetches
the record once and parks it on `patient_history`. Freeze a `patient_label` there as a
snapshot at intake time, and let `CaseCard` read only from case state. The board then
keeps rendering through a CRM outage, which is the behaviour the degrade path promises.

## 3. The one real blocker: there is no way to list cases

`app/runner.py` gives `snapshot(case_id)` and `history(case_id)`. Both need a case_id
you already know. Nothing enumerates cases. A board is exactly an enumeration.

Two ways to close it.

**Option A — enumerate the checkpointer (MVP).**
`SqliteSaver` stores one row-set per `thread_id`, and `thread_id == case_id`. List the
distinct thread ids, then `snapshot()` each.

```python
# board/repo/checkpoint_repo.py
def case_ids() -> list[str]:
    with sqlite3.connect(DB_PATH) as c:
        return [r[0] for r in c.execute("SELECT DISTINCT thread_id FROM checkpoints")]
```

Zero new writes, no schema to keep in sync, works today. Costs one state load per case
per refresh and cannot filter server-side. Fine to ~100 cases; this is a demo board.

**Option B — a projection table (when A hurts).**
A small `board_cards` table written once at the end of every `start_case` / `resume_case`
in `app/runner.py`: `case_id, clinical_status, acuity, bucket_rank, arrival_time,
patient_label, flags, updated_at`. Queryable, sortable, indexable, and the natural place
to hang timers later.

**Recommendation:** build A, behind an interface, so B is a swap and not a rewrite.

```python
# board/repo/__init__.py
class BoardRepo(Protocol):
    def cards(self) -> list[CaseCard]: ...
    def card(self, case_id: str) -> CaseCard | None: ...
```

`CaseCard` is a DTO built from `TriageState` — it holds only what the board renders, so
the board never ships raw state to the browser.

Related cleanup: `intake-channel/channel/api.py::_view` is already the "state → browser"
mapper, and the board needs the same thing for the detail panel. Move it to
`triage-app/app/views.py` and import it from both services. One definition of what a case
looks like to a UI.

## 4. Columns: what exists, and what does not

The six World-plane statuses in `SPECIFICATION.md` are the columns. Today only two of them
are ever written by any code:

| Column                  | Written by                           | Exists? | Needs |
| ----------------------- | ------------------------------------ | ------- | ----- |
| `waiting`               | `graph/nodes/terminal.py::monitoring` | ✅ yes  | — |
| `human_review`          | `graph/nodes/gate.py`                 | ✅ yes  | — |
| `reassessment_required` | nothing                               | ❌ no   | STATUS 5a (timers) |
| `treatment_started`     | nothing                               | ❌ no   | STATUS 4 (move machine) |
| `formal_validation`     | nothing                               | ❌ no   | STATUS 5b (release) |
| `patient_released`      | nothing                               | ❌ no   | STATUS 5b (release) |

This is the honest starting point, and it decides the build order. **The board is worth
building before 4 / 5a / 5b land** — it renders all six columns, the two live ones fill
with real cases, and the other four stay empty with their actions disabled. Each later
milestone lights one up. What the board must *not* do is write those statuses itself to
look complete; that would put a second writer on the World plane and break the single
invariant the whole design rests on.

`case_closed` is not a column. Per the spec the card **leaves the board** — filter it out
of `cards()`, keep it reachable by direct link for the audit trail.

## 5. Rules the board has to obey

Straight from `SPECIFICATION.md`; each one is a test, not a comment.

1. **Sort only by the persisted `order_key`** — `(bucket_rank, arrival_time)`. The board
   has no sort logic of its own beyond `sorted(cards, key=...)`.
2. **Position is display, not state.** Position number = index+1 within the `waiting`
   column. It is derived at render time and never stored.
3. **A card keeps its place while it sits in another column.** Going to `human_review` or
   `reassessment_required` does not move a patient in line. So show the position badge on
   those cards too, computed against the same global ordering — not per-column.
4. **Emergent is never behind queued.** `G(¬(queued.order_key < emergent.order_key))` is a
   stated invariant; assert it on every board render in a test.
5. **The clock never re-sorts.** A long-waiting card may turn red; it does not move up.
6. **Manual-edit rule.** The UI enables exactly two moves: `waiting → treatment_started`,
   and any state → release with a `release_reason` ∈ {discharge, ama, transfer, admit}.
   Every other drag is disabled in the UI *and* refused server-side (`BLK`), because a UI
   that only disables is not an authorization layer.
7. **An unsigned-but-departed card is still open** (AMA consequence) — do not let a
   "released" filter hide cards that no nurse has signed.
8. **No CRM reads.** Everything on a card comes from case state. `patient_label` is the
   snapshot frozen at `resolving_identity`; if the patient's name changed in the CRM since
   intake, the card is allowed to be stale, and a CRM outage must not blank the board.

## 6. API surface

```
GET  /                      the board itself (static)
GET  /api/board             one payload, one render: columns, cards, counters
GET  /api/case/{case_id}    detail panel: full view + audit trail (shared app/views.py)
POST /api/case/{case_id}/move      {target_status, actor_role}       → graph
POST /api/case/{case_id}/release   {release_reason, actor_role}      → graph
POST /api/case/{case_id}/reassess  dev-only trigger until 5a exists  → graph
GET  /api/health
```

`/api/board` returns everything the page needs in one call:

```jsonc
{
  "counters": { "waiting": 7, "human_review": 3, "emergent": 2, "avg_wait_min": 18 },
  "columns": ["waiting", "human_review", "reassessment_required",
              "treatment_started", "formal_validation"],
  "cards": [
    { "case_id": "C-1042", "patient_id": "P-4455", "patient_label": "Patient, Registered",
      "status": "waiting", "position": 1, "acuity": 2, "bucket": "emergent",
      "acuity_source": "human_confirmed", "arrival_time": "...", "waited_min": 41,
      "flags": ["cross-check off"], "degraded": [], "gate_pending": false }
  ]
}
```

**Refresh:** poll `/api/board` every 5s for v1. It is one SQLite read and the whole
repo runs on one box. SSE is a later swap behind the same payload — don't start there.

**The two writes go through the graph, not around it.** `/move` and `/release` build the
event and re-enter the case's thread; whether the action is permitted is decided by the
same authorization path that produces `action_denied`. When it refuses, the board shows
the refusal and the Prolog-style explanation on the card. A refusal rendered in the UI is
one of the better things this board can demo.

## 7. The UI

Dark kanban, close to the attached mock — that layout is right and doesn't need changing.

**Top bar:** the counters — total in queue, longest wait, emergent count, awaiting
approval, reassessment due. These are the numbers a charge nurse scans first.

**Columns:** the five live ones across; `patient_released` as a collapsed "left the board
today" drawer rather than a full column, since cards there are terminal.

**Card:** position badge (large, left), patient id + label, acuity chip colored by bucket
(emergent = red, queued = neutral), wait timer, and small state chips for `degraded` /
`gate_pending` / `red_flag_fired`. Keep it to those; a card that shows everything shows
nothing.

**Detail panel** (click a card, per the note on the Whimsical board): the case's control
state and World status, the acuity block (nurse proposed / system proposed / gap / final /
source), the manual status control, and the **arrow trail** — the `audit_log` rendered as
`at · arrow · action · explanation`, which is already exactly the shape `deterministic.audit()`
writes. This is the panel that makes the paths legible.

**Notifications strip:** items 15–20 from the board diagram — reassessment needed, missing
fields, scan failed, erroneous file, transition accepted, approval requested. Source them
from the audit records' arrows rather than inventing a second notification store.

**Front-end stack:** plain HTML + one JS file, no framework — `intake-channel/channel/static/`
is the precedent and the board is not complex enough to justify a build step. If it grows
past ~400 lines of JS, revisit.

## 8. Demo paths the board should make visible

The paths listed on the Whimsical board, and where each becomes visible here:

| Path | On the board |
| --- | --- |
| clean entry `1a → … → 13` | card appears in `waiting` with a position |
| with approval `1a → … 12 → 20 → 1b.z → 2 → 13` | card sits in `human_review`, then lands in `waiting` **at its original position** — the demo that proves rule 3 |
| missing fields `16 → 1b.x` | notification, no card until the resubmission parses |
| injection `18` | notification + refusal record, no card |
| reassessment `14 → 15 → …` | card moves to `reassessment_required` and keeps its place |
| move to treatment `1b.y → 2 → 9 → … → 12 → 19` | the only nurse-initiated column change |

A `board/seed_demo.py` that runs ~15 mock cases through `run_to_completion` gives a
populated board in one command. Worth having before the first screenshot.

## 9. Tests

Mirror the existing offline pytest setup (`uv run tests`, no network, no key):

- ordering: emergent always above queued, arrival breaks ties inside a bucket
- position stability: a case moved `waiting → human_review → waiting` keeps its index
- the clock does not reorder: advancing wait times changes no ordering
- column mapping: every `ClinicalStatus` maps to exactly one column; `case_closed` maps to none
- manual-edit rule: a `POST /move` to `formal_validation` is refused, and the case does not move
- projection: `CaseCard` carries no field the browser shouldn't see

## 10. Milestones

| # | Deliverable | Depends on | Rough size |
| - | ----------- | ---------- | ---------- |
| **M0** | `board/` service skeleton, `BoardRepo` over the checkpointer, `/api/board`, read-only UI with two live columns | nothing | ~1 day |
| **M1** | detail panel + audit trail + notifications strip, shared `app/views.py`, seed script | M0 | ~1 day |
| **M2** | `/move` and `/release` through the graph, refusals rendered | STATUS 4 + 5b design | after 4 |
| **M3** | `reassessment_required` column live | STATUS 5a (timers) | after 5a |
| **M4** | projection table (Option B) in the shared triage store, move to Postgres, SSE, wait-time SLA colouring | volume | later |

M0 + M1 are unblocked and can start now. M2 is the one that should **not** be rushed:
it touches the treatment move, which `STATUS.md` correctly calls a second machine with
its own states, and the board must not become the place where that machine gets
improvised.

## 11. Open questions

1. Does a case in `human_review` occupy a position in the `waiting` count shown to staff,
   or only in the ordering? (Ordering: yes, per rule 3. Display: decide.)
2. What is the wait-time threshold that colours a card red — and is it per acuity band?
   Related to the wait-liveness rule; no number is defined anywhere yet.
3. Board identity: which role is the browser assumed to be? `actor_role` drives the
   authorization result, so the board needs at least a role switcher (nurse / charge
   nurse) to demo the refusal path honestly.
4. Hebrew or English UI, given the intake form mock is Hebrew.
