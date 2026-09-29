-- 018_contractor_sheet_state.sql  (2026-09-29)
--
-- WHY: contractor sheets rotate ONCE a month (26th, 02:00). The code hardcoded
-- the contractor sheet IDs, so after a rotation both jobs still pointed at the
-- OLD sheet (renamed *_ARCHIVED_*):
--   (a) main_to_contractor kept copying new rows into the archived sheet, and
--   (b) contractors_router had a second, wrong trigger ("Archive checked
--       anywhere on the sheet") that kept firing on the old sheet, re-rotating
--       it every night (12 blank copies per sheet, 2026-09-17..09-29).
--       That trigger is removed; the router now rotates on the 26th only.
-- FIX: the ACTIVE sheet for each contractor lives in the database. Rotation
-- updates it; both jobs read it. IDs are never hardcoded again.
--
-- SEED (revised 2026-09-29 after the read-only schema pre-flight): the
-- currently LIVE sheets, i.e. the old data-bearing sheets, are seeded as
-- active. Applying this migration therefore changes NO behaviour: both jobs
-- keep using exactly the sheets they use today. The 8 clean-named sheets from
-- the accidental 09-17 rotations were found to be STALE (the old sheets have
-- since gained 3-39 columns), so they are NOT used. The catch-up rotation is
-- done afterwards with `python3 -m jobs.contractors_router --force`, which
-- copies the CURRENT layout into a fresh blank sheet and switches this table.
--
-- Safe to re-run: CREATE IF NOT EXISTS, seed is ON CONFLICT DO NOTHING (a
-- re-run will never overwrite state written by a later rotation).

CREATE TABLE IF NOT EXISTS contractor_sheet_state (
    slug              text PRIMARY KEY,
    region_code       text NOT NULL UNIQUE,
    base_name         text NOT NULL,          -- clean sheet name, e.g. 'SPS Zambia'
    active_sheet_id   bigint NOT NULL,
    last_rotated_on   date,                   -- idempotency guard: 1 rotation/day/sheet
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS contractor_sheet_rotation_log (
    id                bigserial PRIMARY KEY,
    slug              text NOT NULL REFERENCES contractor_sheet_state(slug),
    old_sheet_id      bigint NOT NULL,
    new_sheet_id      bigint NOT NULL,
    old_renamed_to    text NOT NULL,
    new_name          text NOT NULL,
    trigger_reason    text NOT NULL,
    -- pending_rename: routing already points at the new sheet, cosmetic
    -- renames not confirmed yet (contractors_router retries these each run)
    status            text NOT NULL DEFAULT 'pending_rename'
                      CHECK (status IN ('pending_rename', 'complete')),
    error_message     text,
    rotated_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at      timestamptz
);

CREATE INDEX IF NOT EXISTS idx_contractor_rotation_log_pending
    ON contractor_sheet_rotation_log (status) WHERE status = 'pending_rename';

INSERT INTO contractor_sheet_state (slug, region_code, base_name, active_sheet_id) VALUES
    ('a_track_mozambique', 'MOZ',  'A-Track Mozambique', 7071129797611396),
    ('adars_botswana',     'BOTS', 'ADARS Botswana',     4096231923994500),
    ('adars_sa',           'SA',   'ADARS SA',           8244727318007684),
    ('spd_drc',            'DRC',  'SPD DRC',            1413715040620420),
    ('sps_tanzania',       'TAN',  'SPS Tanzania',       992501116653444),
    ('sps_zambia',         'ZAM',  'SPS Zambia',         2879447154773892),
    ('wr_namibia',         'NAM',  'WR Namibia',         3887174390861700),
    ('zimbabwe',           'ZIM',  'Zimbabwe',           4599803954548612)
ON CONFLICT (slug) DO NOTHING;

GRANT SELECT ON contractor_sheet_state        TO grafana_reader;
GRANT SELECT ON contractor_sheet_rotation_log TO grafana_reader;

-- Verify after applying (expect 8 rows; ids are the current live/old sheets):
--   SELECT slug, region_code, base_name, active_sheet_id FROM contractor_sheet_state ORDER BY slug;