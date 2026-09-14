-- Per-sheet delta state for archive_router.py (2026-09-14). archive_router
-- scans EVERY non-archive sheet in a client's invoicing folder, not just
-- one main sheet like route_resolver.py, so state is tracked per
-- individual sheet_id rather than per client.
--
-- No "pending unresolved" tracking table needed here the way Part 1 and
-- main_to_contractor got one: the one known failure mode (a full archive
-- destination) already self-heals via automatic rotation within the same
-- call, so nothing is left "waiting on an external fix" across runs.
-- Known, accepted gap: a row whose Archive checkbox is checked but which
-- fails to move for some OTHER, rarer reason will not auto-retry once
-- delta is live, since nothing about the row changes afterward. Flagged
-- honestly rather than silently accepted -- can be hardened the same way
-- as Part 1 if this turns out to matter in practice.
CREATE TABLE IF NOT EXISTS archive_router_sheet_state (
    sheet_id            bigint PRIMARY KEY,
    client_slug         text NOT NULL,
    last_version        bigint,
    last_checkpoint_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

GRANT SELECT ON archive_router_sheet_state TO grafana_reader;