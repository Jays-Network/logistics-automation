-- Seeds approval_router_archive_state for the new client North Star
-- (2026-11-11). Archive sheet created by copying GOLDVALE Archive into the new
-- NORTH STAR invoicing folder (386498362140548).
INSERT INTO approval_router_archive_state (client_slug, sheet_id, sheet_name, is_active) VALUES
    ('north_star', 565923993046916, 'NORTH STAR Archive', true)
ON CONFLICT DO NOTHING;
