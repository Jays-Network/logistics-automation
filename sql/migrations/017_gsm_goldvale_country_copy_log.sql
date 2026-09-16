-- Dedup log for GSM/Gold Vale's country-leg approval copies (DRC/ZAM/BOTS
-- REGION: Approval -> copy_row to that region's invoicing sheet). Same
-- purpose and shape as bridge_country_copy_log (migration 016) -- without
-- it, every 10-min tick would re-copy the same row for as long as its
-- approval column stays "Approved", since there's no ROUTE-based
-- filtering here to naturally narrow the scan the way Bridge's
-- KNOWN_ROUTE_OVERRIDES does.
--
-- Column shape deliberately mirrors bridge_country_copy_log exactly
-- (source_sheet_id, source_row_id, country, target_sheet_id,
-- target_row_id, status, error_message, copied_at) so both tables read
-- the same way in Grafana/ad-hoc queries -- client_slug is the one
-- addition, since this single table covers two clients rather than one.
--
-- The unique index on (source_sheet_id, source_row_id, country) IS the
-- dedup check: route_resolver.py's _upsert_gsm_goldvale_country_copy_log
-- upserts on this exact conflict target, same pattern as
-- _upsert_bridge_country_copy_log.
CREATE TABLE IF NOT EXISTS gsm_goldvale_country_copy_log (
    id               bigserial PRIMARY KEY,
    client_slug      text NOT NULL,        -- 'gsm' | 'goldvale'
    source_sheet_id  bigint NOT NULL,
    source_row_id    bigint NOT NULL,
    country          text NOT NULL,        -- 'DRC' | 'ZAM' | 'BOTS'
    target_sheet_id  bigint NOT NULL,
    target_row_id    bigint,               -- null on a failed copy (status='error')
    status           text NOT NULL,        -- 'copied' | 'error'
    error_message    text,
    copied_at        timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gsm_goldvale_country_copy_log_dedup
    ON gsm_goldvale_country_copy_log (source_sheet_id, source_row_id, country);

CREATE INDEX IF NOT EXISTS idx_gsm_goldvale_country_copy_log_client
    ON gsm_goldvale_country_copy_log (client_slug);

GRANT SELECT ON gsm_goldvale_country_copy_log TO grafana_reader;
