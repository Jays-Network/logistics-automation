"""
route_resolver — approval_router, Part 1: Main sheet -> Invoicing

Watches each client's main tracking sheet for rows whose ROUTE column is set
and whose "<Manager Name> approval" column reads "Approved", then hands them
to mover.py's move_row() to actually move them into the right invoicing sheet.

This module owns ONLY the "which row, which target, is it approved" decision.
It does not touch Smartsheet's row data directly (mover.py does the backup +
move + verify) and it has no concept of Part 2 (Invoicing -> Archive), which
is a structurally different problem (checkbox trigger, cell-limit rotation)
and lives in its own module once built.

Matching algorithm (confirmed live against all 12 clients, 2026-09-13 — see
route-resolver-logic.md for the full audit):
    1. Split the ROUTE value on its first "-" to get a candidate client prefix.
    2. Match that prefix case-insensitively against the INVOICING workspace's
       folder names (not the main-tracking side — they often differ, e.g.
       "Bridge" vs "Steinweg Bridge" on the main-tracking side, but the
       invoicing folder really is "BRIDGE").
    3. Within that folder, match the ENTIRE raw ROUTE value (not just the
       suffix), case-insensitively and whitespace-trimmed, against real sheet
       names. This is deliberately not a "parse the suffix" approach — real
       ROUTE data has typos/spacing/casing inconsistencies that make suffix
       parsing fragile; matching the whole string against real sheet names
       sidesteps that.
    4. RELOAD is structurally different (see RELOAD_LOCAL_ROUTE_MAP below).
    5. One case that WAS a many-to-one exception is no longer one: Bridge's
       "Sicomine Mine-DBN" and "Sicomine Mine-DAR" ROUTE values used to both
       point at one combined sheet. Jay asked (2026-09-13) for these split
       into two separate sheets instead — done, so this now resolves via
       the normal algorithm with no override needed. KNOWN_ROUTE_OVERRIDES
       below is kept as a real (currently empty) mechanism in case a
       similar merge turns up for another client later.
    6. No match at step 2 or 3 -> skip the row and log it for review. Never
       guess/fuzzy-match past this point.
    7. GSM and Gold Vale (2026-09-16) are now fully diverted BEFORE this
       algorithm ever runs — see GSM_GOLDVALE_CONFIG below. Their old
       per-route invoicing sheets (GSM-DRC-DBN, GOLDVALE-COMMUS-DBN, etc.)
       were deleted the same day their new country sheets were created, so
       this algorithm has nothing left to match against for them. Bridge
       (2026-09-23) joined this same "fully diverted before ROUTE matching"
       group — see _process_bridge_country_leg_row below.

The plain "Glencore" folder (main-tracking id 5600395335624580) is a
genuinely different, actively-used operation with its own sheet structure
(no single main-sheet + ROUTE + approval pattern) — confirmed with Jay
2026-09-13 that it's out of scope. It's simply not in MAIN_SHEETS below.

Layout (split from the old single route_resolver.py):
    __main__.py        single entry point:  python -m jobs.approval_router.route_resolver
    runner.py          main(): lock, DB connection, loop over clients
    processor.py       process_sheet(): per-sheet row loop + diversion to client flows
    resolver.py        ROUTE -> target sheet (overrides, RELOAD, IXM, folder match)
    config.py          MAIN_SHEETS registry, JOB_NAME, lock settings
    clients/           one file per client's special rules and sheet IDs
    util.py alerts.py db.py lock.py   shared helpers
"""

from jobs.approval_router.backup import get_connection  # noqa: F401  (kept for diag scripts: rr.get_connection)

from .config import MAIN_SHEETS  # noqa: F401  (jobs/main_to_contractor.py imports this from here)
from .runner import main

__all__ = ["main", "MAIN_SHEETS", "get_connection"]
