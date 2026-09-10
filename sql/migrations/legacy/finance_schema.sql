-- finance_schema.sql
-- Built from actual sheet structures in Smartsheet workspace 3486055002335108.
-- Completely separate from adars_tracking / worldrisk_tracking — no FK to those tables.

-- =========================================================================
-- 1. Cross Border Finance ledger (daily expense movements)
--    Source sheets: "Cross Border Finance" (live) + "Arcive of Cross Border
--    Finance" (archive) — same column shape, distinguished by source_sheet.
-- =========================================================================
CREATE TABLE IF NOT EXISTS finance_expenses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    row_id              BIGINT UNIQUE NOT NULL,      -- Smartsheet row_id (natural key for upsert)
    sheet_id            BIGINT NOT NULL,
    source_sheet        TEXT NOT NULL,               -- 'Cross Border Finance' | 'Archive of Cross Border Finance'

    expense_date        DATE,                         -- "DAILY EXPENSES"
    ref_tracking         TEXT,                         -- "Ref / Tracking #" (auto ID, e.g. ADARSCB#1)
    country               TEXT,                         -- Botswana / Mozambique / Zimbabwe / Zimbabwe (Beitbridge)
    requested_by            TEXT,
    is_casual                 BOOLEAN,
    reason                      TEXT[],                 -- multi-picklist: BBR OPPS, Escourt, Vehicle R&M, etc.
    reg_number                    TEXT,                 -- "Detail: (Reg. No)" — loosely cross-referenceable to adars_tracking.reg_number, NOT an FK
    funds_for                        TEXT,               -- Accommodation / Fuel & Tolls / Salaries / etc.
    currency                            TEXT,             -- 'P-BWP' | '$-Dollars' | 'ZAR - SA Rand' — NOTE: multi-currency, do not sum across rows without conversion
    amount                                 NUMERIC(14,2),
    number_of_trucks                          SMALLINT,
    number_of_escorts                            SMALLINT,
    number_of_days                                  SMALLINT,
    ob_number                                          TEXT,
    client_name                                          TEXT,
    odo_open                                                NUMERIC(12,2),
    odo_close                                                NUMERIC(12,2),
    kms_driven                                                  NUMERIC(12,2),   -- prefer computing odo_close - odo_open in queries; column kept for source-of-truth comparison
    litres                                                         NUMERIC(12,2),
    comment                                                           TEXT,
    is_archived                                                         BOOLEAN DEFAULT false,

    raw_data             JSONB,           -- full row as fallback for anything not flattened
    row_modified_at      TIMESTAMPTZ,
    last_synced_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_finance_expenses_country ON finance_expenses(country);
CREATE INDEX IF NOT EXISTS idx_finance_expenses_client ON finance_expenses(client_name);
CREATE INDEX IF NOT EXISTS idx_finance_expenses_date ON finance_expenses(expense_date);
CREATE INDEX IF NOT EXISTS idx_finance_expenses_reg_number ON finance_expenses(reg_number);
CREATE INDEX IF NOT EXISTS idx_finance_expenses_currency ON finance_expenses(currency);


-- =========================================================================
-- 2. Payment Requests (parent) — the 5 form sheets: Airtime/Data, Food,
--    Fuel, Transport, Other. Same template, differ by Type of Request /
--    which sheet they came from. request_category records which sheet.
-- =========================================================================
CREATE TABLE IF NOT EXISTS finance_payment_requests (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    row_id                  BIGINT UNIQUE NOT NULL,
    sheet_id                BIGINT NOT NULL,
    request_category        TEXT NOT NULL,     -- 'Airtime or Data' | 'Food' | 'Fuel' | 'Transport' | 'Other' (source sheet)

    finance_ref              TEXT,               -- "ARM-PR# 0000039760" style auto ID
    date_submitted             TIMESTAMPTZ,
    request_date                  DATE,
    type_of_request                  TEXT,        -- picklist value on the row itself (Fuel/Other/Cross-Border $/CPO/Food Money/Transport money)
    requested_by                        TEXT,
    payee_name                             TEXT,
    description                               TEXT,
    item_or_vehicle_count                        TEXT,   -- picklist is messy: "1".."8" or "1 Vehicles".."8 Vehicles" — kept as text, parse in query layer
    amount                                          NUMERIC(14,2),
    total_zar                                          NUMERIC(14,2),   -- formula column in source
    total_usd                                             NUMERIC(14,2),
    conversion_rate                                          NUMERIC(14,6),
    is_vatable                                                  BOOLEAN,

    paid_status                TEXT,             -- 'Yes' | 'Hold' | 'No'
    paid_by                        TEXT,
    is_high_priority                  BOOLEAN,
    standard_approval                    TEXT,   -- Submitted | Approved | Declined
    executive_manager                       TEXT,
    executive_approval                         TEXT,   -- Submitted | Approved | Declined
    return_approval                               TEXT,   -- Submitted | Approved | Declined

    signature                  TEXT,
    signature_authoriser          TEXT,

    raw_data                JSONB,             -- full row, fallback for anything not flattened
    row_modified_at         TIMESTAMPTZ,
    last_synced_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_finance_requests_category ON finance_payment_requests(request_category);
CREATE INDEX IF NOT EXISTS idx_finance_requests_paid_status ON finance_payment_requests(paid_status);
CREATE INDEX IF NOT EXISTS idx_finance_requests_approval ON finance_payment_requests(standard_approval, executive_approval);
CREATE INDEX IF NOT EXISTS idx_finance_requests_date ON finance_payment_requests(request_date);
CREATE INDEX IF NOT EXISTS idx_finance_requests_payee ON finance_payment_requests(payee_name);


-- =========================================================================
-- 3. Payment Request line items (unpivoted) — each request has up to 8
--    repeating blocks: Posting Code N / Vehicle Reg N / Odometer Reading N
--    / Description N / Amount N. Unpivoted into one row per line item
--    instead of 40 flat columns.
-- =========================================================================
CREATE TABLE IF NOT EXISTS finance_payment_request_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID NOT NULL REFERENCES finance_payment_requests(id) ON DELETE CASCADE,
    line_number     SMALLINT NOT NULL,     -- 1..8
    posting_code    TEXT,
    vehicle_reg     TEXT,                   -- cross-referenceable to adars_tracking.reg_number, NOT an FK
    odometer_reading NUMERIC(12,2),
    description     TEXT,
    amount          NUMERIC(14,2),

    UNIQUE(request_id, line_number)
);

CREATE INDEX IF NOT EXISTS idx_finance_items_vehicle_reg ON finance_payment_request_items(vehicle_reg);


-- =========================================================================
-- Read access for Grafana, matching existing grafana_reader convention
-- =========================================================================
GRANT SELECT ON finance_expenses TO grafana_reader;
GRANT SELECT ON finance_payment_requests TO grafana_reader;
GRANT SELECT ON finance_payment_request_items TO grafana_reader;