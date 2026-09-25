-- The "Triage Guard" dashboard: 12 widgets over the fields every case trace carries
-- (trace names case-start / case-resume / timer-fire / board-action, and the scores
-- control_state, acuity, acuity_gap, guardrail_blocks, trace_check).
--
-- Run by the `langfuse-seed` service in docker-compose.yml after Langfuse has created
-- the project. Idempotent: fixed ids + upsert, so re-running it restores the seeded
-- widgets and leaves any dashboards made in the UI alone.
--
-- Usage: psql -v ON_ERROR_STOP=1 -v project=<LANGFUSE_INIT_PROJECT_ID> -f triage-dashboard.sql
--
-- Queries use observations and scores views only: in Langfuse v4 events_only mode the
-- trace-level view is not queryable, and each case run's root span carries the trace
-- name, so counting root spans by name counts runs.

BEGIN;

INSERT INTO dashboard_widgets
  (id, project_id, name, description, view, dimensions, metrics, filters, chart_type, chart_config)
VALUES
  ('tg-w01', :'project', 'Case operations', 'How much load, and of which kind: case runs by trace name.',
   'OBSERVATIONS', '[{"field":"name"}]', '[{"agg":"count","measure":"count"}]',
   '[{"column":"name","operator":"any of","value":["case-start","case-resume","timer-fire","board-action"],"type":"stringOptions"}]',
   'BAR_TIME_SERIES', '{"type":"BAR_TIME_SERIES"}'),

  ('tg-w02', :'project', 'Case start latency p95', 'Is a new case triaged inside the 5 s intake target?',
   'OBSERVATIONS', '[]', '[{"agg":"p95","measure":"latency"}]',
   '[{"column":"name","operator":"=","value":"case-start","type":"string"}]',
   'LINE_TIME_SERIES', '{"type":"LINE_TIME_SERIES"}'),

  ('tg-w03', :'project', 'Slowest steps p95', 'Which graph node is the bottleneck?',
   'OBSERVATIONS', '[{"field":"name"}]', '[{"agg":"p95","measure":"latency"}]',
   '[{"column":"type","operator":"any of","value":["CHAIN"],"type":"stringOptions"}]',
   'HORIZONTAL_BAR', '{"type":"HORIZONTAL_BAR","row_limit":15}'),

  ('tg-w04', :'project', 'Where cases are', 'Control state at the end of each run: awaiting a human, monitoring, closed.',
   'SCORES_CATEGORICAL', '[{"field":"stringValue"}]', '[{"agg":"count","measure":"count"}]',
   '[{"column":"name","operator":"=","value":"control_state","type":"string"}]',
   'PIE', '{"type":"PIE","row_limit":20}'),

  ('tg-w05', :'project', 'Cases per urgency level (ESI 1-5)', 'Runs per settled acuity, 1 = most urgent, 5 = least. Most should be 3-4; a spike at 1-2 or one level only suggests a classifier or input problem.',
   'SCORES_NUMERIC', '[{"field":"value"}]', '[{"agg":"count","measure":"count"}]',
   '[{"column":"name","operator":"=","value":"acuity","type":"string"}]',
   'VERTICAL_BAR', '{"type":"VERTICAL_BAR","row_limit":5}'),

  ('tg-w06', :'project', 'Nurse vs model acuity gap', 'Average disagreement between nurse and model: is the classifier drifting?',
   'SCORES_NUMERIC', '[]', '[{"agg":"avg","measure":"value"}]',
   '[{"column":"name","operator":"=","value":"acuity_gap","type":"string"}]',
   'LINE_TIME_SERIES', '{"type":"LINE_TIME_SERIES"}'),

  ('tg-w07', :'project', 'Guardrail blocks', 'Actions a guard refused (each refusal counted once).',
   'SCORES_NUMERIC', '[]', '[{"agg":"sum","measure":"value"}]',
   '[{"column":"name","operator":"=","value":"guardrail_blocks","type":"string"}]',
   'BAR_TIME_SERIES', '{"type":"BAR_TIME_SERIES"}'),

  ('tg-w08', :'project', 'Trace-check failures', 'Runs whose audit log broke a safety ordering rule. Target: 0.',
   'SCORES_BOOLEAN', '[]', '[{"agg":"count","measure":"count"}]',
   '[{"column":"name","operator":"=","value":"trace_check","type":"string"},{"column":"booleanValue","operator":"=","value":false,"type":"boolean"}]',
   'NUMBER', '{"type":"NUMBER"}'),

  ('tg-w09', :'project', 'Errors by step', 'Which node fails (level ERROR)?',
   'OBSERVATIONS', '[{"field":"name"}]', '[{"agg":"count","measure":"count"}]',
   '[{"column":"level","operator":"any of","value":["ERROR"],"type":"stringOptions"}]',
   'HORIZONTAL_BAR', '{"type":"HORIZONTAL_BAR","row_limit":15}'),

  ('tg-w10', :'project', 'LLM cost by model', 'What live mode costs per model. Empty in mock mode.',
   'OBSERVATIONS', '[{"field":"providedModelName"}]', '[{"agg":"sum","measure":"totalCost"}]',
   '[]',
   'VERTICAL_BAR', '{"type":"VERTICAL_BAR","row_limit":10}'),

  ('tg-w11', :'project', 'New cases', 'Intake volume: one case-start per new case.',
   'OBSERVATIONS', '[]', '[{"agg":"count","measure":"count"}]',
   '[{"column":"name","operator":"=","value":"case-start","type":"string"}]',
   'BAR_TIME_SERIES', '{"type":"BAR_TIME_SERIES"}'),

  ('tg-w12', :'project', 'Timer fires', 'Are reassessment timers firing?',
   'OBSERVATIONS', '[]', '[{"agg":"count","measure":"count"}]',
   '[{"column":"name","operator":"=","value":"timer-fire","type":"string"}]',
   'BAR_TIME_SERIES', '{"type":"BAR_TIME_SERIES"}')
ON CONFLICT (id) DO UPDATE SET
  project_id = EXCLUDED.project_id, name = EXCLUDED.name, description = EXCLUDED.description,
  view = EXCLUDED.view, dimensions = EXCLUDED.dimensions, metrics = EXCLUDED.metrics,
  filters = EXCLUDED.filters, chart_type = EXCLUDED.chart_type,
  chart_config = EXCLUDED.chart_config, updated_at = CURRENT_TIMESTAMP;

-- Layout: 12-column grid, two widgets per row, in the order above.
INSERT INTO dashboards (id, project_id, name, description, definition)
SELECT 'tg-dashboard', :'project', 'Triage Guard',
       'Case load, latency, where cases stand, acuity, guardrail blocks and safety trace checks.',
       jsonb_build_object('widgets', jsonb_agg(jsonb_build_object(
         'id', 'tg-p' || lpad(n::text, 2, '0'),
         'type', 'widget',
         'widgetId', 'tg-w' || lpad(n::text, 2, '0'),
         'x', ((n - 1) % 2) * 6,
         'y', ((n - 1) / 2) * 5,
         'x_size', 6,
         'y_size', 5) ORDER BY n))
FROM generate_series(1, 12) AS n
ON CONFLICT (id) DO UPDATE SET
  project_id = EXCLUDED.project_id, name = EXCLUDED.name, description = EXCLUDED.description,
  definition = EXCLUDED.definition, updated_at = CURRENT_TIMESTAMP;

COMMIT;
