-- Seeds approval_router_archive_state with the confirmed Archive sheet for
-- each of the 12 clients. Found live 2026-09-14: every invoicing folder
-- already had a bare "<CLIENT>" sheet sitting at the top (previously
-- assumed to be some kind of index/placeholder) -- Jay renamed all 12 to
-- "<CLIENT> Archive" in a single batch to make their purpose explicit,
-- confirming they've been the intended archive destinations all along.
--
-- One is_active=true row per client -- archive_router.py (Part 2) looks
-- these up directly rather than discovering them via folder-browsing or
-- naming heuristics at runtime.
INSERT INTO approval_router_archive_state (client_slug, sheet_id, sheet_name, is_active) VALUES
    ('alistair',               3585932048420740, 'ALISTAIR Archive', true),
    ('bridge',                 8390292397838212, 'BRIDGE Archive', true),
    ('fh_bertling',            771182281314180,  'FH BERTLING Archive', true),
    ('glencore_international', 1128742603673476, 'GLENCORE INTERNATIONAL Archive', true),
    ('goldvale',               4856896623169412, 'GOLDVALE Archive', true),
    ('gsm',                    7748094660661124, 'GSM Archive', true),
    ('ixm',                    7179872634883972, 'IXM Archive', true),
    ('mittal',                 8307299402600324, 'MITTAL Archive', true),
    ('sls_africa',             3659921450028932, 'SLS AFRICA Archive', true),
    ('sls_trading',            6339218686037892, 'SLS TRADING Archive', true),
    ('zalawi',                 6622194753818500, 'ZALAWI Archive', true),
    ('reload',                 4575928452599684, 'RELOAD Archive', true)
ON CONFLICT DO NOTHING;