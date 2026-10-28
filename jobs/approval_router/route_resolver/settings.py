"""Plain constants for route_resolver (no imports from the client files, so the
client modules can import this without a circular import)."""

INVOICING_WORKSPACE_ID = 1702905712535428

JOB_NAME = "route_resolver_part1"

LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/route_resolver.lock"
# Zombie threshold is measured from the lock's LAST HEARTBEAT (touched on
# every row), not from when the run started. Previously a long Reload
# backlog run (>60 min) was treated as a zombie, its lock was cleared, and
# a second run started on top of it (2026-10-09: 190 duplicate-move 404s).
LOCK_STALE_SECONDS = 1800
