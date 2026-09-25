# Langfuse (self-hosted)

Observability backend for Triage Guard — traces, logs, and the eval pipeline.
The graph that sends traces here lives in [`../triage-app/app/`](../triage-app/app/); see the
[root README](../README.md) to run it.

## Run

```bash
cp .env.example .env
docker compose up -d
```

Web UI: http://localhost:3000 — login `admin@local.dev` / `changeme123`
(auto-bootstrapped via `.env`).

## Files

- `docker-compose.yml` — official Langfuse v4 self-host stack (postgres, clickhouse, redis, minio, web, worker)
- `.env.example` — template, copy to `.env` before first boot
- `.env` — bootstraps the org, project, user and API keys on first boot
- `seed/triage-dashboard.sql` — the Triage Guard dashboard, applied by the `langfuse-seed` service

## Notes

- **`LANGFUSE_INIT_*` only applies on first boot against an empty database.**
  Editing those values later renames nothing. To re-bootstrap, wipe the volumes:
  `docker compose down -v && docker compose up -d` — this destroys all stored
  traces. The API keys are pinned in `.env`, so they survive and the app's
  `../.env` keeps working.
- The keys in this file must match `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
  in the repo-root `.env`.
- This deployment runs in **v4 events_only mode**: the `/api/public/traces` read
  API is disabled and the legacy `observations` table stays empty. Trace data
  lives in the `events_core` / `events_full` ClickHouse tables. The UI is unaffected.

## What a case trace carries

Every graph run on a case is one trace. It is named after what started the run:

| Trace name | Started by |
|---|---|
| `case-start` | a new case from the intake form or the CLI demo |
| `case-resume` | a staff answer to a pause (human gate, reassessment re-file, release) |
| `timer-fire` | the sweeper firing a reassessment timer |
| `board-action` | move to treatment or release from the board |

On each trace:

- **Session** = the case id. One session holds every trace of one case, in order.
- **User** = the patient, as `pt-` plus a hash keyed by `TRIAGE_TRACE_SALT`. The raw
  patient ID is never sent. With no salt set, traces carry no patient at all.
- **Tags**: the trace name, `llm-mock` or `llm-live`, `channel-<channel>`, and the
  submission type when there is one.
- **Metadata**: `case_id`, `operation`, `llm_mode`, and `model` in live mode.
- **Environment / Release**: from `LANGFUSE_TRACING_ENVIRONMENT` and `LANGFUSE_RELEASE`.
- **Root span output**: `control_state`, `paused`, and `decisions`, the audit records this
  run added (action, explanation, transition, denying layer), which say what was
  decided and why.
- **Scores**:

| Score | Type | Meaning |
|---|---|---|
| `control_state` | categorical | where the case stood when the run ended |
| `acuity` | numeric | settled acuity (ESI 1-5), once there is one |
| `acuity_gap` | numeric | nurse vs model disagreement |
| `guardrail_blocks` | numeric | refusals added by this run only, so sums over a case do not double count |
| `trace_check` | boolean | the whole audit log passes the safety ordering rules |

National IDs, phone numbers and emails in any recorded value (including free text
and the graph's node inputs) are replaced with `[REDACTED_ID]`, `[REDACTED_PHONE]`
or `[REDACTED_EMAIL]` before they leave the app.

### Finding a case or a patient

- By case: Sessions → search the case id (`case-…`). The session holds every
  trace of that case: start, each resume, timer fires, board actions.
- By patient: `cd triage-app && uv run python -m app.observability <patient id>`
  prints `pt-…`; paste it in Users, or filter Tracing by User ID.

### Saved views (seeded)

The same `langfuse-seed` service also loads `seed/triage-views.sql`. It adds three views
to the **Views** dropdown on the Tracing page. Each one shows one row per case run
(root observations only), not every graph node.

| View | Filter | Use it to |
|---|---|---|
| Trace-check failures | trace score `trace_check` = false | Find runs that broke a safety ordering rule. Should stay empty. |
| Guardrail blocks | trace score `guardrail_blocks` > 0 | See which actions a guard refused. |
| Live LLM runs | trace tag `llm-live` | Look only at runs that called the real model. |

For failing steps, use Langfuse's built-in **Errors Only** view. To look up one case,
paste its id on the Sessions page. For one patient, paste their `pt-…` ref on the Users
page. These need a different id every time, so they are not saved views.

### The "Triage Guard" dashboard (seeded)

`docker compose up -d` also runs a one-shot `langfuse-seed` service. It waits until
`langfuse-web` has created the project (`LANGFUSE_INIT_PROJECT_ID`), then applies
`seed/triage-dashboard.sql`. Every machine that starts this stack gets the same
dashboard, with no clicking. The seed uses fixed ids and upserts, so re-running it
(`docker compose up langfuse-seed`) restores the seeded widgets and leaves
dashboards made in the UI alone. To change a widget, edit the SQL and re-run it.

| # | Widget | Query | Chart | Question |
|---|---|---|---|---|
| 1 | Case operations | observations, count by name, name in the four trace names | bar, time | How much load, and of which kind? |
| 2 | Case start latency p95 | observations, p95 latency, name = `case-start` | line, time | Inside the 5 s intake target? |
| 3 | Slowest steps p95 | observations, p95 latency by name, type = CHAIN | horizontal bar | Which graph node is the bottleneck? |
| 4 | Where cases are | categorical scores `control_state`, count by value | pie | How many await a human, are monitoring, are closed? |
| 5 | Cases per urgency level (ESI 1-5) | numeric scores `acuity`, count by value | bar | Most runs at ESI 3-4? A spike at 1-2 suggests a classifier or input problem |
| 6 | Nurse vs model acuity gap | numeric scores `acuity_gap`, average | line, time | Is the classifier drifting from the nurses? |
| 7 | Guardrail blocks | numeric scores `guardrail_blocks`, sum | bar, time | How often did a guard refuse an action? |
| 8 | Trace-check failures | boolean scores `trace_check` = false, count | number | Did any run break a safety ordering rule? Target 0. |
| 9 | Errors by step | observations, count by name, level = ERROR | horizontal bar | Which node fails? |
| 10 | LLM cost by model | observations, total cost by model | bar | What does live mode cost? Empty in mock mode. |
| 11 | New cases | observations, count, name = `case-start` | bar, time | Intake volume |
| 12 | Timer fires | observations, count, name = `timer-fire` | bar, time | Are reassessment timers firing? |

The widgets query observations and scores only: in v4 events_only mode the
trace-level view is not queryable, and every case run's root span carries the
trace name, so counting root spans by name counts runs.

Scores cannot be read through the public API in events_only mode; the UI and
dashboards read them fine. For a direct check:
`docker compose exec clickhouse clickhouse-client --user clickhouse --password clickhouse -q "SELECT name, value, string_value FROM scores WHERE trace_id='<id>'"`.
