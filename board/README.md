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
uv run seed-board       # ~15 mock cases through the real graph, for a populated board
uv run board            # http://127.0.0.1:8002
uv run tests            # offline, no key, no crm-stub
```

`BOARD_HOST` / `BOARD_PORT` override the bind address. The board reads the same
checkpoint store as `triage-app` (`TRIAGE_CHECKPOINT_DB`), read-only.

## What it is, and what it is not

**A view plus nothing.** The board never computes triage, never writes
`TriageState`, never re-computes `order_key`, and never decides acuity. It reads
persisted state and derives two display values — queue position and wait time.

**M2 is not built.** `POST /move` and `/release` wait on the treatment-move
machine (`docs/STATUS.md` item 4), which is a second state machine with its own
rules and deserves its own design. The UI renders those controls disabled and
there is no endpoint behind them, because a board that quietly became the second
writer to the World plane would break the invariant the whole design rests on.

**Four columns are empty on purpose.** Only `waiting` and `human_review` have a
writer today. `reassessment_required` (STATUS 5a), `treatment_started` (STATUS 4)
and `formal_validation` / `patient_released` (STATUS 5b) render empty with the
reason shown. Each later milestone lights one up.

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
│   ├── seed_demo.py   `uv run seed-board`
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

The page polls `/api/board` every 5s. That is one SQLite read and the whole repo
runs on one box; SSE is a later swap behind the same payload.

## Known ceilings

- `CheckpointRepo` loads one state per case per refresh and cannot filter
  server-side. Fine to ~100 cases. Past that, write a `board_cards` projection row
  at the end of `start_case`/`resume_case` and implement `BoardRepo` over it —
  that is why the Protocol exists.
- `RED_AFTER_MIN` in `api.py` is a placeholder: no wait-time threshold is defined
  anywhere in the spec yet.
