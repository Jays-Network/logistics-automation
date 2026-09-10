CREATE TABLE IF NOT EXISTS approval_router_row_backup (
    id                bigserial PRIMARY KEY,
    source_sheet_id   bigint NOT NULL,
    source_row_id     bigint NOT NULL,
    client_slug       text NOT NULL,
    row_data          jsonb NOT NULL,
    backed_up_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_router_row_backup_source
    ON approval_router_row_backup (source_sheet_id, source_row_id);
CREATE INDEX IF NOT EXISTS idx_approval_router_row_backup_client
    ON approval_router_row_backup (client_slug);
CREATE INDEX IF NOT EXISTS idx_approval_router_row_backup_backed_up_at
    ON approval_router_row_backup (backed_up_at);

GRANT SELECT ON approval_router_row_backup TO grafana_reader;