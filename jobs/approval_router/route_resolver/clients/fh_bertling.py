"""FH BERTLING -- client file: ids, ROUTE rules and self-test cases for this client only.

Plain folder-match client. Sheet names must start with 'FH Bertling-' so the prefix matches the ROUTE picklist.
2026-10-28 (per Jay): added 'FH Bertling-MMG-DRC-DBN' (no code needed). Country legs were scrapped by Jay; if they are ever wanted it is a booked build (like GSM / Gold Vale).

If something breaks for this client, the log line carries the logger name
'approval_router.route_resolver.fh_bertling' and the failing step is one of the
numbered steps in resolve() below.
"""

from ..shared import folder_match
from ..util import client_logger

CLIENT_SLUG = "fh_bertling"
MAIN_SHEET_ID = 636041135345540
INVOICING_FOLDER_ID = 2127623745759108
ROUTE_PREFIXES = ('fh bertling',)   # normalized text before the first '-'

log = client_logger(CLIENT_SLUG)

# Offline/live self-test (python -m jobs.approval_router.route_resolver.selftest):
# (route, local_route, commodity, expected invoicing sheet_id). Snapshot of the
# live folder on 2026-10-28.
SELFTEST_CASES = [
    ('FH Bertling-DRC-DBN', None, None, 3176951859859332),
    ('FH Bertling-GRB-DBN', None, None, 8926881184173956),
    ('FH Bertling-MMG-DRC-DBN', None, None, 2020143858405252),
]


def process_row(conn, ss_client, main_sheet_id, row, columns_by_id, stats, **ctx) -> bool:
    """No custom per-row flow for this client."""
    return False


def resolve(route_value, local_route_value, commodity_value, folder_cache):
    """Returns (sheet_id_or_None, reason)."""
    # 1. Whole ROUTE value must equal a sheet name in this client's folder.
    sheet_id, reason = folder_match.resolve_in_folder(
        CLIENT_SLUG, ROUTE_PREFIXES, INVOICING_FOLDER_ID, route_value, folder_cache)
    if sheet_id is None:
        log.debug("step 1 (folder match) failed: %s", reason)
    return sheet_id, reason
