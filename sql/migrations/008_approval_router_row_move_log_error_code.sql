-- Adds a structured error_code column alongside the existing error_message
-- text field. Lets route_resolver.py's Part 2 (Invoicing -> Archive) logic
-- match on Smartsheet's own numeric errorCode (e.g. 5636 = cell-limit
-- exceeded, confirmed live 2026-09-10 -- see mover.py's SHEET_FULL_ERROR_CODE)
-- to detect a full sheet reliably, instead of parsing error_message text
-- (which includes an HTML help link that could change wording).
ALTER TABLE approval_router_row_move_log
    ADD COLUMN IF NOT EXISTS error_code integer;

CREATE INDEX IF NOT EXISTS idx_approval_router_row_move_log_error_code
    ON approval_router_row_move_log (error_code)
    WHERE error_code IS NOT NULL;