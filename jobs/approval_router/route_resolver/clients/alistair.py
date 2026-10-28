"""ALISTAIR -- client file: ids, ROUTE rules and self-test cases for this client only.

Plain folder-match client: ROUTE 'ALISTAIR-<from>-<to>' must equal an invoicing sheet name. No overrides, no special columns.

If something breaks for this client, the log line carries the logger name
'approval_router.route_resolver.alistair' and the failing step is one of the
numbered steps in resolve() below.
"""

from ..shared import folder_match
from ..util import client_logger

CLIENT_SLUG = "alistair"
MAIN_SHEET_ID = 8253984251793284
INVOICING_FOLDER_ID = 5153479745398660
ROUTE_PREFIXES = ('alistair',)   # normalized text before the first '-'

log = client_logger(CLIENT_SLUG)

# Offline/live self-test (python -m jobs.approval_router.route_resolver.selftest):
# (route, local_route, commodity, expected invoicing sheet_id). Snapshot of the
# live folder on 2026-10-28.
SELFTEST_CASES = [
    ('ALISTAIR-DRC-DAR', None, None, 3960558960201604),
    ('ALISTAIR-DRC-DBN', None, None, 1237188917284740),
    ('ALISTAIR-GRB-DBN', None, None, 5527552008343428),
    ('ALISTAIR-ZAM - DAR', None, None, 1763504815296388),
    ('ALISTAIR-ZAM - DBN', None, None, 426034819452804),
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
