-- finance_sync_state.sql
-- Tracks per-sheet last-synced modified_at so Sync_Finance.py can skip
-- unchanged sheets on delta runs (every minute) and only do full row
-- processing when a sheet actually changed, or during the once-daily
-- full rescan at 00:00.

CREATE TABLE IF NOT EXISTS finance_sync_sheet_state (
    sheet_id                BIGINT PRIMARY KEY,
    sheet_name               TEXT,
    kind                        TEXT,   -- 'ledger' | 'request_live' | 'request_archived'
    category                       TEXT,
    last_synced_modified_at            TIMESTAMPTZ,
    last_synced_at                          TIMESTAMPTZ DEFAULT now()
);