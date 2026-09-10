-- frontend_reader — dedicated read-only role for the Next.js frontend.
-- Deliberately separate from grafana_reader: same read-only reach into the
-- reporting views, but a compromise or bug in one app can't act as the
-- other. Run this once, manually, as jaysnet_admin.

CREATE ROLE frontend_reader WITH LOGIN PASSWORD 'CHANGE_ME_SET_A_REAL_PASSWORD';

GRANT SELECT ON automation_job_health         TO frontend_reader;
GRANT SELECT ON approval_pipeline_daily       TO frontend_reader;
GRANT SELECT ON approval_router_stuck_moves   TO frontend_reader;
GRANT SELECT ON automation_alert_volume_daily TO frontend_reader;
