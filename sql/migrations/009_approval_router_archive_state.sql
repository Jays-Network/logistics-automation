-- Tracks which archive sheet is currently "active" (the move target) for
-- each client, for route_resolver.py's Part 2 (Invoicing -> Archive).
--
-- Why a DB table instead of discovering the active sheet from Smartsheet
-- folder/naming conventions at runtime: the rename of a full sheet (moving
-- it out of the way once a new blank copy takes over) happens as part of
-- the same automated rotation flow, but relying on naming patterns to
-- infer "which one is current" is fragile -- this table is the single
-- source of truth instead, updated atomically by route_resolver.py the
-- moment it creates a replacement sheet.
CREATE TABLE IF NOT EXISTS approval_router_archive_state (
    id              bigserial PRIMARY KEY,
    client_slug     text NOT NULL,
    sheet_id        bigint NOT NULL,
    sheet_name      text NOT NULL,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deactivated_at  timestamptz
);

-- Enforces at most one active archive sheet per client at a time -- a
-- safety net against a bug ever creating two "current" targets.
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_router_archive_state_active_client
    ON approval_router_archive_state (client_slug)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_approval_router_archive_state_client
    ON approval_router_archive_state (client_slug);

GRANT SELECT ON approval_router_archive_state TO grafana_reader;