# intake-channel

A standalone nurse-facing intake UI for Triage Guard. It stands in for the
real website intake form described in the main `SPECIFICATION.md`
("Input Channel"), so the pipeline can be exercised end-to-end before a real
webform exists.

It is a small independent HTTP service, the same pattern as `crm-stub/`: it looks
up a patient over HTTP, builds an intake payload, and runs that payload through
the **real LangGraph control plane** in `triage-app/`. It depends on that package
(an editable path dependency) but the dependency only points one way — the graph
never calls back into the channel.

## `intake-channel` vs. the board

They are opposite in direction and are separate services:

| | `intake-channel` (this) | `board` (later stage) |
| --- | --- | --- |
| Data direction | **in** — creates a new case | **out** — projects existing state |
| What it shows | a form: lookup + submission type + submit | a kanban of existing cases |
| Writes state? | no — only sends an event | no — only reads a read-model |

## What it does in this stage

1. Nurse types a `stable_patient_id` and clicks **Lookup** — a thin proxy to
   the CRM stub's `GET /patients/{id}`, distinguishing `found` / `not_found`
   / `db_error`.
2. Nurse picks one of four mock submission types (clean / missing / failed /
   injection) and clicks **Submit**.
3. The service builds the payload and invokes the graph. The response carries the
   real control state, the settled acuity, and the full audit trail with the
   spec's arrow labels.
4. If the case pauses at a gate, the UI renders it and the nurse resolves it —
   `POST /resume/{case_id}` continues the checkpointed case.

### Submission types

| Type | Exercises |
| --- | --- |
| clean | the happy path through to `monitoring` |
| missing | arrow 16 — missing fields, no acuity ever guessed |
| failed | arrow 17 — nothing usable |
| gap | arrow 9c — nurse and system disagree by ≥2, **pauses for a charge nurse** |
| injection | arrow 18 — rejected before anything reaches the model |

`gap` is the interesting one: it suspends the case to a checkpoint and waits. Try
resolving it as `nurse` rather than `charge_nurse` to see a `BLK` refusal that
leaves the case exactly where it was.

### Endpoints

| | |
| --- | --- |
| `GET /lookup/{id}` | CRM proxy — found / not_found / db_error all return 200 |
| `POST /submit` | build a case and run it through the graph |
| `POST /resume/{case_id}` | answer a human gate |
| `GET /case/{case_id}` | read the checkpointed state and audit trail |

See `docs/SPECIFICATION.md` for the intake outcomes and the gate.

## Running

Managed with [uv](https://docs.astral.sh/uv/).

```bash
# from intake-channel/
uv sync
uv run intake-channel        # serves on CHANNEL_PORT (default 8001)
```

Requires the CRM stub running alongside it for patient lookups:

```bash
# from crm-stub/, in a separate terminal
uv run crm                   # serves on CRM_PORT (default 8000)
```

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `CRM_BASE_URL` | `http://127.0.0.1:8000` | Where to reach the CRM stub |
| `CHANNEL_HOST` | `127.0.0.1` | Bind host for this service |
| `CHANNEL_PORT` | `8001` | Bind port for this service |

## Tests

```bash
uv run tests
```
