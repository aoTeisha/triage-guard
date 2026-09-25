-- Saved views ("Views" dropdown on the Tracing page) for the searches staff repeat.
--
-- Run by the `langfuse-seed` service in docker-compose.yml, after triage-dashboard.sql.
-- Idempotent: fixed ids + upsert, so re-running it restores these views and leaves
-- views made in the UI alone.
--
-- Usage: psql -v ON_ERROR_STOP=1 -v project=<LANGFUSE_INIT_PROJECT_ID> -f triage-views.sql
--
-- Every view keeps only root observations (`isRootObservation`), so the table shows one
-- row per case run (case-start, case-resume, timer-fire, board-action) instead of every
-- graph node. The filters use trace scores and tags written by app/observability.py.
--
-- Not seeded: an errors view (Langfuse ships "Errors Only"), and case or patient lookup
-- (they need a different id each time; use the Sessions and Users pages).

BEGIN;

INSERT INTO table_view_presets
  (id, project_id, name, table_name, filters, column_order, column_visibility, order_by)
VALUES
  ('tg-view-trace-check', :'project', 'Trace-check failures', 'observations-events',
   '[{"column":"isRootObservation","type":"boolean","operator":"=","value":true},
     {"column":"trace_score_booleans","type":"booleanObject","key":"trace_check","operator":"=","value":false}]',
   '[]', '{}', NULL),

  ('tg-view-guardrail', :'project', 'Guardrail blocks', 'observations-events',
   '[{"column":"isRootObservation","type":"boolean","operator":"=","value":true},
     {"column":"trace_scores_avg","type":"numberObject","key":"guardrail_blocks","operator":">","value":0}]',
   '[]', '{}', NULL),

  ('tg-view-live-llm', :'project', 'Live LLM runs', 'observations-events',
   '[{"column":"isRootObservation","type":"boolean","operator":"=","value":true},
     {"column":"traceTags","type":"arrayOptions","operator":"any of","value":["llm-live"]}]',
   '[]', '{}', NULL)
ON CONFLICT (id) DO UPDATE SET
  project_id = EXCLUDED.project_id, name = EXCLUDED.name, table_name = EXCLUDED.table_name,
  filters = EXCLUDED.filters, column_order = EXCLUDED.column_order,
  column_visibility = EXCLUDED.column_visibility, order_by = EXCLUDED.order_by,
  updated_at = CURRENT_TIMESTAMP;

COMMIT;
