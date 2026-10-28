"""MITTAL -- client file: ids, ROUTE rules and self-test cases for this client only.

Plain folder-match client: ROUTE 'MITTAL-SA-<city>' must equal an invoicing sheet name.

If something breaks for this client, the log line carries the logger name
'approval_router.route_resolver.mittal' and the failing step is one of the
numbered steps in resolve() below.
"""

from ..shared import folder_match
from ..util import client_logger

CLIENT_SLUG = "mittal"
MAIN_SHEET_ID = 4646970969771908
INVOICING_FOLDER_ID = 8812654442637188
ROUTE_PREFIXES = ('mittal',)   # normalized text before the first '-'

log = client_logger(CLIENT_SLUG)

# Offline/live self-test (python -m jobs.approval_router.route_resolver.selftest):
# (route, local_route, commodity, expected invoicing sheet_id). Snapshot of the
# live folder on 2026-10-28.
SELFTEST_CASES = [
    ('MITTAL-SA-HARARE', None, None, 8554507519610756),
    ('MITTAL-SA-LUSAKA', None, None, 2780508105691012),
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
