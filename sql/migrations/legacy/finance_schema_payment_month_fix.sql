-- finance_schema_payment_month_fix.sql
-- Run this after finance_schema_archive_migration.sql partially failed on
-- the payment_month columns. Safe to re-run (IF NOT EXISTS everywhere).
--
-- Why the original failed:
-- 1. finance_payment_requests: COALESCE(request_date, date_submitted::date)
--    casts a TIMESTAMPTZ to DATE, which depends on the session's TimeZone
--    setting — Postgres correctly refuses to treat that as IMMUTABLE, which
--    GENERATED ALWAYS AS columns require. No way around this while
--    date_submitted (timestamptz) is part of the expression.
-- 2. finance_expenses: date_trunc('month', expense_date) SHOULD resolve to
--    the immutable timestamp overload via implicit cast from DATE, but
--    didn't in this environment — rather than rely on that resolution,
--    this version uses EXTRACT()/make_date() instead, which has no
--    volatility ambiguity at all.

ALTER TABLE finance_payment_requests
    DROP COLUMN IF EXISTS payment_month;

ALTER TABLE finance_payment_requests
    ADD COLUMN payment_month DATE GENERATED ALWAYS AS (
        CASE WHEN request_date IS NOT NULL
             THEN make_date(EXTRACT(YEAR FROM request_date)::int, EXTRACT(MONTH FROM request_date)::int, 1)
             ELSE NULL
        END
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_finance_requests_payment_month ON finance_payment_requests(payment_month);


ALTER TABLE finance_expenses
    DROP COLUMN IF EXISTS payment_month;

ALTER TABLE finance_expenses
    ADD COLUMN payment_month DATE GENERATED ALWAYS AS (
        CASE WHEN expense_date IS NOT NULL
             THEN make_date(EXTRACT(YEAR FROM expense_date)::int, EXTRACT(MONTH FROM expense_date)::int, 1)
             ELSE NULL
        END
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_finance_expenses_payment_month ON finance_expenses(payment_month);