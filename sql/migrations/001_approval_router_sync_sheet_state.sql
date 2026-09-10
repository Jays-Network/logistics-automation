CREATE TABLE IF NOT EXISTS approval_router_sync_sheet_state (
    sheet_id            bigint PRIMARY KEY,
    client_slug         text NOT NULL,
    sheet_name          text NOT NULL,
    last_version        bigint NOT NULL DEFAULT 0,
    last_checkpoint_at  timestamptz NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_router_sync_sheet_state_client
    ON approval_router_sync_sheet_state (client_slug);

GRANT SELECT ON approval_router_sync_sheet_state TO grafana_reader;