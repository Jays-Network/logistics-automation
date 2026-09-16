-- Tracks every row copied from Bridge's main tracking sheet to a
-- country sheet (DRC/Zambia/Botswana) as part of the 14-route
-- multi-leg consolidation, 2026-09-16. Same pattern as
-- main_to_contractor_copy_log: an upsert keyed on
-- (source_sheet_id, source_row_id, country) -- a copy doesn't
-- remove/uncheck anything on the source, so re-scanning the same row
-- on every cron tick would otherwise re-copy it forever once its
-- REGION: Approval column is set to Approved.
CREATE TABLE IF NOT EXISTS bridge_country_copy_log (
    id                bigserial PRIMARY KEY,
    source_sheet_id   bigint NOT NULL,
    source_row_id     bigint NOT NULL,
    country           text NOT NULL CHECK (country IN ('DRC', 'ZAMBIA', 'BOTSWANA')),
    target_sheet_id   bigint,
    target_row_id     bigint,
    status            text NOT NULL DEFAULT 'copied' CHECK (status IN ('copied','error')),
    error_message     text,
    copied_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_sheet_id, source_row_id, country)
);

GRANT SELECT ON bridge_country_copy_log TO grafana_reader;
