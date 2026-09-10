CREATE TABLE IF NOT EXISTS automation_job_run_log (
    id              bigserial PRIMARY KEY,
    job_name        text NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    status          text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'success', 'failed')),
    rows_processed  integer NOT NULL DEFAULT 0,
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_automation_job_run_log_job_started
    ON automation_job_run_log (job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_job_run_log_status
    ON automation_job_run_log (status);

GRANT SELECT ON automation_job_run_log TO grafana_reader;