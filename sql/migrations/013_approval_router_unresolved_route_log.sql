-- Tracks rows whose ROUTE could not be resolved to a target sheet, so
-- route_resolver.py's delta logic (added 2026-09-14) can force a full
-- re-check of these SPECIFIC rows every run, even when the source sheet's
-- version hasn't changed -- because the fix for an unresolvable route is
-- usually external (someone creates the missing sheet), which never
-- touches the source row/sheet at all. Without this, delta-skipping would
-- silently stop these rows from ever auto-recovering once fixed.
CREATE TABLE IF NOT EXISTS approval_router_unresolved_route_log (
    id                bigserial PRIMARY KEY,
    source_sheet_id   bigint NOT NULL,
    source_row_id     bigint NOT NULL,
    client_slug       text NOT NULL,
    route_value       text,
    reason            text,
    first_seen_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_checked_at   timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_router_unresolved_route_unique
    ON approval_router_unresolved_route_log (source_sheet_id, source_row_id);

GRANT SELECT ON approval_router_unresolved_route_log TO grafana_reader;