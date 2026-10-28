"""SLS TRADING -- client file: ids, ROUTE rules and self-test cases for this client only.

Plain folder-match client: ROUTE 'SLS TRADING-<from>-<to>' must equal an invoicing sheet name.

If something breaks for this client, the log line carries the logger name
'approval_router.route_resolver.sls_trading' and the failing step is one of the
numbered steps in resolve() below.
"""

from ..shared import folder_match
from ..util import client_logger

CLIENT_SLUG = "sls_trading"
MAIN_SHEET_ID = 4242048029773700
INVOICING_FOLDER_ID = 6701592117307268
ROUTE_PREFIXES = ('sls trading',)   # normalized text before the first '-'

log = client_logger(CLIENT_SLUG)

# Offline/live self-test (python -m jobs.approval_router.route_resolver.selftest):
# (route, local_route, commodity, expected invoicing sheet_id). Snapshot of the
# live folder on 2026-10-28.
SELFTEST_CASES = [
    ('SLS TRADING-DRC-BBR', None, None, 6158207826218884),
    ('SLS TRADING-DRC-DAR', None, None, 391733195001732),
    ('SLS TRADING-DRC-NDOLA', None, None, 3964475970375556),
    ('SLS TRADING-GRB-DBN', None, None, 4634387789336452),
    ('SLS TRADING-NDOLA-DAR', None, None, 2416088720560004),
    ('SLS TRADING-NDOLA-DBN', None, None, 7284107733061508),
    ('SLS TRADING-NDOLA-GRB', None, None, 1654608198848388),
    ('SLS TRADING-NDOLA-WALVIS BAY', None, None, 3307122635788164),
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
