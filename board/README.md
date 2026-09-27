# board — the nurse-facing service (:8002)

The one web service a nurse uses: the live queue board and the intake front
door, on one page. It creates cases, answers every pause a nurse or charge
nurse resolves, and projects the state of every open case — all without ever
computing triage itself.

```
crm-stub        :8000   patient history
board           :8002   the whole nurse-facing service — intake in, queue out
triage-app              the control plane the board calls into
```

The intake form and the queue board are one process on one origin: the
browser makes no cross-origin request, and there is no CORS configuration
anywhere in this service.

## Run

```bash
uv sync
uv run board            # http://127.0.0.1:8002
uv run tests            # offline, no key
```

Needs the shared Postgres running (`docker compose -f db/docker-compose.yml up
-d postgres` from the repo root) — that is where every case's checkpointed
state lives. For patient lookups it also needs the CRM stub on port 8000
(`cd crm-stub && uv run crm`); without it, every lookup reports `db_error` and
the board keeps rendering — a CRM outage never blocks intake. Once a case
reaches `monitoring` it has a reassessment timer scheduled against it; nothing
fires that timer unless the sweeper is also running (`cd triage-app && uv run
sweeper`).

## What it is, and what it is not

**A projection plus a set of requests, not a control plane.** The board
computes no triage, assigns no acuity, and re-computes no ordering. Every
write — including case creation — re-enters the case's own LangGraph run
rather than writing case state directly. Read endpoints derive two display
values from persisted state (queue position and wait time) and nothing else.

**One function owns every write that re-enters a paused case.**
`board/board/commands.py::answer_pause` takes the case lock, checks that the
case is genuinely at the pause the caller expects, invokes the graph, and
hands back an `Outcome`. All nine pause-answering endpoints below call it;
none of them touch case state directly. Case creation (`POST /api/submit`) is
the one write that does not go through it — starting a new LangGraph thread is
a different operation from re-entering a paused one, and it takes its own
locks (one scoped to the patient, so two submissions for the same patient
serialize; one scoped to the new thread).

**A refusal is not an HTTP error.** When a guard inside the graph refuses an
action, that is a normal, successfully-processed outcome, not a failure —
the graph recorded it as a `BLK` row in the case's audit trail. Two shapes for
it, depending on which half of the API answered:

- The four board-originated action endpoints return HTTP 200 with
  `{"status": "denied", "detail": "<the guard's own words>"}`. This is the
  contract that lets the page tell "accepted" apart from "refused" instead of
  treating every 200 as success — it is deliberately unchanged since before
  the merge, and no test's expected value was edited to keep it that way.
- The five endpoints that create or continue a case (originally the intake
  service's) return the bare case view, with the refusal visible as the
  trail's last `BLK` row and, for a gate refusal, `gate` still populated so
  the same form can be answered again by someone authorized.

A request the API accepts can therefore still come back refused — the real
authorization checks (`move_authorized`, `release_authorized`, a gate's
resolver-role check, and so on) run inside the graph node that receives the
resume, not in this API layer.

**A released card is not removed — it moves to the drawer.** `patient_released`
cases stay reachable through `/api/board` (an unsigned-but-departed case must
stay open, per the spec's AMA rule); the frontend renders that column
separately from the main queue (`DRAWER_COLUMN` in `static/labels.js`) instead of
inline with active cases.

**A case suspended at the gate is on the board.** `gate.py` writes
`clinical_status = human_review` when it *returns*, and a run paused at the
acuity gate has not returned — its committed state has no World-plane status at
all. The board reads the checkpoint's pending task (`repo.at_gate`) and shows
those cases in `human_review`, because the case a charge nurse is needed for is
the last one that should be invisible. That is display, like the position badge:
no state is written, and the real World-plane write still lands on resume.

**No CRM call from the board's own read path, ever.** A card's label is
derived from the `crm_status` already on the case (`Registered` / `New
patient` / `CRM down`) — never a name, never a per-card fetch. The only place
the board talks to the CRM stub is `GET /api/lookup/{national_id}`, which the
new-case panel calls while a nurse is filling in the intake form.

## Read endpoints

| | |
|---|---|
| `GET /` | the page |
| `GET /api/board` | columns, cards, counters, notifications — one call per refresh |
| `GET /api/case/{case_id}` | detail panel: `{view, card, checkpoints, history_safety}` |
| `GET /api/health` | |
| `GET /api/heartbeat` | is the background sweeper still alive? |
| `GET /api/lookup/{national_id}` | CRM proxy for the intake form |

`GET /api/lookup/{national_id}` always answers HTTP 200 with a `status` field
(`found` / `not_found` / `db_error`) and a `record` — all three are outcomes
the page renders, not errors to branch on. A new patient and an unreachable
CRM both mean "continue on intake-only data," so neither stops a submission.

The page polls `/api/board` every 5s.

## Write endpoints

Nine endpoints, one writer. Each re-enters the case's paused LangGraph run
through `commands.answer_pause`; the table says which pause each one answers
and who is meant to call it.

| Endpoint | Answers | Who |
| --- | --- | --- |
| `POST /api/submit` | creates a new case (not a pause answer) | the new-case panel |
| `POST /api/case/{id}/resume` | the human-approval gate | a charge nurse (a `nurse` resolver is refused) |
| `POST /api/case/{id}/reassess` | the reassessment re-filing pause | a nurse, after a reassessment timer fires |
| `POST /api/case/{id}/fields` | the missing-fields pause | a nurse completing an incomplete intake — no form on the page; HTTP only |
| `POST /api/case/{id}/recover` | the agent-recovery pause | a technician, after a halted agent recovers — no form on the page; HTTP only |
| `POST /api/case/{id}/deteriorated` | the waiting-room pause | a nurse reporting a worsening condition |
| `POST /api/case/{id}/move-to-treatment` | the waiting-room pause | a nurse moving a waiting case into treatment |
| `POST /api/case/{id}/treatment-complete` | the waiting-room pause | a nurse marking treatment done, before release |
| `POST /api/case/{id}/release` | any open pause | a charge nurse with a valid reason |

The last four confirm the audit log actually grew before reporting success
(`require_applied=True` in `commands.answer_pause`); the first five do not
(`require_applied=False`). Each endpoint's response shape stays exactly what
its callers depend on: no endpoint gains a refusal its contract doesn't
already document.

### Submission types

`POST /api/submit` takes a `submission_type`, one of five:

| Type | Exercises |
| --- | --- |
| clean | the happy path through to `monitoring` |
| missing | missing fields — no acuity is ever guessed |
| failed | nothing usable received |
| gap | a nurse/system acuity disagreement of two or more — pauses for a charge nurse |
| injection | rejected before anything reaches the model |

`gap` is the interesting one: it suspends the case at the human-approval gate
and waits. Resolving it as `nurse` rather than `charge_nurse` produces a `BLK`
refusal that leaves the case exactly where it was, at the same gate.

## Layout

```
board/
├── board/
│   ├── repo.py            BoardRepo protocol + CheckpointRepo — case enumeration for reads
│   ├── ordering.py        sort by order_key, derive position. No other sort logic exists.
│   ├── commands.py        answer_pause — the one function every write goes through
│   ├── intake.py          case creation and the five pauses answered from the intake side
│   ├── patient_lookup.py  CRM client for the intake form's lookup step
│   ├── mock_cases.py      builds the five demo submission payloads
│   ├── api.py             FastAPI app: mounts the intake router, serves the read endpoints
│   │                      and the board's own four write endpoints
│   └── static/            one HTML file, six plain scripts sharing one global scope, no build step
└── tests/
    ├── board/             the projection, the columns, the ordering, the four manual status changes
    └── intake/            case creation, the pauses answered from the intake side
```

`CaseCard`, `card_from_state` and `case_view` live in `triage-app/app/views.py`
— shared with the graph itself, so "what a case looks like to a UI" has one
definition.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `CRM_BASE_URL` | `http://127.0.0.1:8000` | where to reach the CRM stub |
| `BOARD_HOST` | `127.0.0.1` | bind host for this service |
| `BOARD_PORT` | `8002` | bind port for this service |
| `TRIAGE_CHECKPOINT_DB` | (see `triage-app`) | the checkpoint store this service reads and writes |

## Tests

```bash
uv run tests
```

Needs a running Postgres (throwaway databases are created and dropped per
test). The suite is split into two directories, `tests/board/` and
`tests/intake/`, each with its own autouse database fixtures — flattening them
into one directory would make every test build two or three throwaway
databases at once, with the graph patched to one and the duplicate-case check
reading another.

## Known ceilings

- `CheckpointRepo` loads one state per case per refresh and cannot filter
  server-side. Fine to ~100 cases. Past that, write a `board_cards` projection
  row at the end of `start_case`/`resume_case` and implement `BoardRepo` over
  it — that is why the Protocol exists.
- `BOARD_RED_AFTER_MINUTES` in `triage-app/app/budgets.py` is a placeholder: no
  wait-time threshold is defined anywhere in the spec yet.
- Move-to-treatment and release are the minimal version (2026-09-17): no Tool
  Gateway, no idempotency key, no `PENDING/CONFIRMED/FAILED/UNKNOWN` execution
  states, no reconciliation — those exist to protect against a downstream
  hospital system this project doesn't have. Both are also only wired from the
  waiting-room pause, not from the human-approval gate or the reassessment
  re-file pause.
- `formal_validation` still has no writer — this minimal version releases
  straight from `treatment_started`, skipping it (release is state-independent
  per spec, so this is spec-legal, not a gap).
