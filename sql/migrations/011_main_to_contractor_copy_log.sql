-- Tracks every row copied from a main sheet to a contractor sheet
-- (main_to_contractor.py). The unique index is the idempotency
-- mechanism: since a copy doesn't remove/uncheck anything on the source
-- (unlike the move-based pipelines), re-scanning the same row on every
-- cron tick would otherwise re-copy it forever.
CREATE TABLE IF NOT EXISTS main_to_contractor_copy_log (
    id                bigserial PRIMARY KEY,
    source_sheet_id   bigint NOT NULL,
    source_row_id     bigint NOT NULL,
    region_code       text NOT NULL,
    target_sheet_id   bigint,
    target_row_id     bigint,
    client_slug       text NOT NULL,
    status            text NOT NULL DEFAULT 'copied' CHECK (status IN ('copied','error')),
    error_message     text,
    copied_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_main_to_contractor_unique
    ON main_to_contractor_copy_log (source_sheet_id, source_row_id, region_code)
    WHERE status = 'copied';

CREATE INDEX IF NOT EXISTS idx_main_to_contractor_client
    ON main_to_contractor_copy_log (client_slug);

GRANT SELECT ON main_to_contractor_copy_log TO grafana_reader;