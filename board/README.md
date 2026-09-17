# board — the queue board (:8002)

The nurse-facing view of the World plane: one card per live case, one column per
`clinical_status`, sorted by the `order_key` the control plane already assigned.
Click a card for its state, its acuity block, and its arrow trail.

```
crm-stub        :8000   patient history
intake-channel  :8001   the front door — one case in
board           :8002   the room — all cases out
triage-app              the control plane all three call into
```

## Run

```bash
uv sync
uv run board            # http://127.0.0.1:8002
uv run tests            # offline, no key, no crm-stub
```

The board has no data of its own and no way to create any: it only ever shows
cases that arrived through `intake-channel` and landed in the shared checkpoint
store. Nothing in this repo can put a case on the board except a real submission.

`BOARD_HOST` / `BOARD_PORT` override the bind address. The board reads the same
checkpoint store as `triage-app` (`TRIAGE_CHECKPOINT_DB`), read-only.

## What it is, and what it is not

**A view plus nothing.** The board never computes triage, never writes
`TriageState`, never re-computes `order_key`, and never decides acuity. It reads
persisted state and derives two display values — queue position and wait time.

**Move-to-treatment and release, minimal version (2026-09-17).** A nurse can
now click "Start treatment" and "Release patient" on a case's detail panel.
Both re-enter that case's paused LangGraph run with `Command(resume=...)` —
the same mechanism `/deteriorated` already used — rather than writing case
state directly; the real authorization check (`move_authorized` /
`release_authorized`) runs inside the graph node, not in this API layer.
This is deliberately **not** the full treatment-move machine from
`docs/STATUS.md` item 4: no Tool Gateway, no idempotency key, no
`PENDING/CONFIRMED/FAILED/UNKNOWN` execution states, no reconciliation —
those exist to protect against a downstream hospital system that this
project doesn't have. It's also only wired from the waiting-room pause
(`control_state == monitoring`), not from the human-approval gate or the
reassessment re-file pause — see
`docs/superpowers/plans/2026-09-17-treatment-move-and-release/findings.md`
for the reasoning and what's out of scope.

**A released card is not removed — it moves to the drawer.** `patient_released`
cases stay reachable through `/api/board` (an unsigned-but-departed case must
stay open, per the spec's AMA rule); the frontend renders that column
separately from the main queue (`DRAWER_COLUMN` in `board.js`) instead of
inline with active cases.

**One column is still empty on purpose.** `formal_validation` has no writer —
this minimal version releases straight from `treatment_started`, skipping it
(release is state-independent per spec, so this is spec-legal, not a gap).

**A case suspended at the gate is on the board.** `gate.py` writes
`clinical_status = human_review` when it *returns*, and a run paused at the
acuity gate has not returned — its committed state has no World-plane status at
all. The board reads the checkpoint's pending task (`repo.at_gate`) and shows
those cases in `human_review`, because the case a charge nurse is needed for is
the last one that should be invisible. That is display, like the position badge:
no state is written, and the real World-plane write still lands on resume.

**No CRM call, ever.** A card's label is derived from the `crm_status` already on
the case (`Registered` / `New patient` / `CRM down`) — never a name, never a
per-card fetch. Stop `crm-stub` and the board keeps rendering, which is the
behaviour the degrade path promises.

## Layout

```
board/
├── board/
│   ├── repo.py        BoardRepo protocol + CheckpointRepo — the case enumeration
│   ├── ordering.py    sort by order_key, derive position. No other sort logic exists.
│   ├── api.py         FastAPI: /api/board, /api/case/{id}, /api/health
│   └── static/        one HTML file, one JS file, no build step
└── tests/             offline; each test names the spec rule it enforces
```

`CaseCard`, `card_from_state` and `case_view` live in `triage-app/app/views.py` —
shared with `intake-channel`, so "what a case looks like to a UI" has one
definition.

## Endpoints

| | |
|---|---|
| `GET /` | the board |
| `GET /api/board` | columns, cards, counters, notifications — one call per refresh |
| `GET /api/case/{case_id}` | detail panel: the shared case view, its card, its trail |
| `GET /api/health` | |
| `GET /api/heartbeat` | is the background sweeper still alive? |
| `POST /api/case/{case_id}/deteriorated` | nurse reports a worsening condition while waiting |
| `POST /api/case/{case_id}/move-to-treatment` | nurse moves a waiting case into treatment |
| `POST /api/case/{case_id}/release` | nurse releases a case (reason: discharge/ama/transfer/admit) |

The page polls `/api/board` every 5s. That is one SQLite read and the whole repo
runs on one box; SSE is a later swap behind the same payload.

## Known ceilings

- `CheckpointRepo` loads one state per case per refresh and cannot filter
  server-side. Fine to ~100 cases. Past that, write a `board_cards` projection row
  at the end of `start_case`/`resume_case` and implement `BoardRepo` over it —
  that is why the Protocol exists.
- `RED_AFTER_MIN` in `api.py` is a placeholder: no wait-time threshold is defined
  anywhere in the spec yet.
