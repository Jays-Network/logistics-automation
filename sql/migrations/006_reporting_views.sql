CREATE OR REPLACE VIEW automation_job_health AS
SELECT DISTINCT ON (job_name)
    job_name, status, started_at, finished_at,
    EXTRACT(EPOCH FROM (finished_at - started_at)) AS duration_seconds,
    rows_processed, error_message
FROM automation_job_run_log
ORDER BY job_name, started_at DESC;

CREATE OR REPLACE VIEW approval_pipeline_daily AS
SELECT client_slug, date_trunc('day', created_at) AS day, status, count(*) AS row_count
FROM approval_router_row_move_log
GROUP BY client_slug, date_trunc('day', created_at), status;

CREATE OR REPLACE VIEW approval_router_stuck_moves AS
SELECT id, client_slug, source_sheet_id, source_row_id, status, updated_at,
       now() - updated_at AS stuck_for
FROM approval_router_row_move_log
WHERE status IN ('backed_up', 'copied')
  AND updated_at < now() - interval '30 minutes';

-- fleet_status still not built — same gap flagged earlier: no job mirrors full
-- sheet state, so there's nothing to build this view from yet. Unchanged decision
-- needed: new mirror job, or Grafana/frontend query Smartsheet directly.

GRANT SELECT ON automation_job_health TO grafana_reader;
GRANT SELECT ON approval_pipeline_daily TO grafana_reader;
GRANT SELECT ON approval_router_stuck_moves TO grafana_reader;