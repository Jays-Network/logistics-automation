-- Unified execution log for the Orchestrator (Phase 5), 2026-09-15.
--
-- Every job -- new (approval_router family) or legacy, cron-triggered or
-- manually triggered via the orchestrator API -- gets tracked here
-- identically once its crontab entry (or manual invocation) is routed
-- through orchestrator/run_wrapper.py. This is deliberately separate from
-- automation_job_run_log (which only the 4 new approval_router-family
-- jobs write to, from inside their own code) -- this table captures
-- execution at the PROCESS level (did it start, did it exit 0, what did
-- it print) regardless of whether the job itself has any internal
-- logging at all. That's what makes it work uniformly across legacy and
-- dormant scripts with zero code changes to them.
CREATE TABLE IF NOT EXISTS orchestrator_job_run_log (
    id              bigserial PRIMARY KEY,
    job_name        text NOT NULL,          -- module path used to invoke it, e.g. "jobs.legacy.Sync_ADARS"
    trigger_source  text NOT NULL DEFAULT 'cron' CHECK (trigger_source IN ('cron', 'manual', 'api')),
    started_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at     timestamptz,
    exit_code       integer,
    status          text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    stdout_tail     text,                   -- bounded tail, not the full output -- avoid unbounded growth
    stderr_tail     text,
    error_message   text
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_job_run_log_job_name
    ON orchestrator_job_run_log (job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_orchestrator_job_run_log_status
    ON orchestrator_job_run_log (status)
    WHERE status = 'running';  -- fast lookup for "what's currently in-flight / possibly stuck"

GRANT SELECT ON orchestrator_job_run_log TO grafana_reader;