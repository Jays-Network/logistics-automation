CREATE TABLE IF NOT EXISTS approval_router_row_move_log (
    id                bigserial PRIMARY KEY,
    backup_id         bigint NOT NULL REFERENCES approval_router_row_backup(id),
    source_sheet_id   bigint NOT NULL,
    source_row_id     bigint NOT NULL,
    target_sheet_id   bigint,
    target_row_id     bigint,
    client_slug       text NOT NULL,
    route_value       text,
    mine_value        text,
    status            text NOT NULL DEFAULT 'backed_up'
                        CHECK (status IN ('backed_up', 'copied', 'verified', 'deleted', 'error')),
    error_message     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_router_row_move_log_status
    ON approval_router_row_move_log (status);
CREATE INDEX IF NOT EXISTS idx_approval_router_row_move_log_client
    ON approval_router_row_move_log (client_slug);
CREATE INDEX IF NOT EXISTS idx_approval_router_row_move_log_source
    ON approval_router_row_move_log (source_sheet_id, source_row_id);
CREATE INDEX IF NOT EXISTS idx_approval_router_row_move_log_stuck
    ON approval_router_row_move_log (status, updated_at)
    WHERE status IN ('backed_up', 'copied');

GRANT SELECT ON approval_router_row_move_log TO grafana_reader;