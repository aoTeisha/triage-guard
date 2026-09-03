# intake-channel

A standalone nurse-facing intake UI for Triage Guard. It stands in for the
real website intake form described in the main `SPECIFICATION.md`
("Input Channel"), so the pipeline can be exercised end-to-end before a real
webform exists.

It is **not** part of the crewAI crew — it never appears in `app/crew.jsonc`
and never calls an agent directly. It is a small independent HTTP service,
the same pattern as `crm-stub/`: it looks up a patient over HTTP, builds a
mock intake payload, and hands that payload to the Intake Parser task the
same way `app/main.py` does today.

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
3. The service builds the corresponding payload and runs it through the
   `parse_intake` task (still mock output, no LLM call yet — same as
   `app/main.py`), emitting a Langfuse span.

No state machine yet: this stage proves the pipeline from UI to agent works
and is visible in the trace. See `docs/SPECIFICATION.md` for the four intake
outcomes and `../STAGE1_INTAKE_CHANNEL_PLAN.md` for the full implementation
plan, including what's deliberately deferred (the missing-fields completion
screen needs the state machine from the next stage).

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
