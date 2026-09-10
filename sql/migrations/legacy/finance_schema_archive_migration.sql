-- finance_schema_archive_migration.sql
-- Run this AFTER finance_schema.sql (tables already have live data — this is
-- additive, not a rebuild). Safe to run with psql, which auto-commits DDL.

-- =========================================================================
-- Archive tracking on finance_payment_requests
--
-- Paid requests move OUT of the 5 live category sheets and INTO the
-- "Archive sheets" folder (one always-live main archive sheet, plus
-- historical dated chunks created whenever the main one fills up — the
-- sync script discovers these dynamically, they are not hardcoded).
--
-- is_archived distinguishes "still pending in a live category sheet" from
-- "payment completed, now sitting in the archive". archive_source_sheet
-- records which physical archive sheet a row came from, since there can be
-- several (main + N historical chunks) and new ones appear over time.
-- =========================================================================
ALTER TABLE finance_payment_requests
    ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS archive_source_sheet TEXT;

CREATE INDEX IF NOT EXISTS idx_finance_requests_is_archived ON finance_payment_requests(is_archived);
CREATE INDEX IF NOT EXISTS idx_finance_requests_archive_source ON finance_payment_requests(archive_source_sheet);


-- =========================================================================
-- Calendar-month bucketing ("all August payments in August, all June
-- payments in June"). This is a plain calendar month, NOT the 25th-24th
-- invoicing_period convention used elsewhere in the project — deliberately
-- different because this is payment/expense tracking, not truck invoicing.
-- request_date is the row's own "Date :" field; falls back to
-- date_submitted if request_date is ever null so nothing drops out of
-- every monthly bucket.
-- =========================================================================
ALTER TABLE finance_payment_requests
    ADD COLUMN IF NOT EXISTS payment_month DATE GENERATED ALWAYS AS (
        date_trunc('month', COALESCE(request_date, date_submitted::date))::date
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_finance_requests_payment_month ON finance_payment_requests(payment_month);

ALTER TABLE finance_expenses
    ADD COLUMN IF NOT EXISTS payment_month DATE GENERATED ALWAYS AS (
        date_trunc('month', expense_date)::date
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_finance_expenses_payment_month ON finance_expenses(payment_month);