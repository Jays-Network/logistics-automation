-- Delta-sync state for main_to_contractor.py. Two-tier check per Jay's
-- design (2026-09-14): (1) sheet-level -- has this sheet changed at all
-- since we last looked, using Smartsheet's version number (a cheap call,
-- no row data fetched); (2) row-level -- once a sheet HAS changed, only
-- rows modified since our last check get evaluated at all, using each
-- row's own modified_at (no extra API call -- it's already part of the
-- row data we fetch once the sheet-level check says something changed).
CREATE TABLE IF NOT EXISTS main_to_contractor_sheet_state (
    main_sheet_id    bigint PRIMARY KEY,
    last_version     bigint,
    last_checked_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

GRANT SELECT ON main_to_contractor_sheet_state TO grafana_reader;