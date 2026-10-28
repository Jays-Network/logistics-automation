"""ZALAWI -- client file: ids, ROUTE rules and self-test cases for this client only.

Plain folder-match client: ROUTE 'ZALAWI-<mine>-GRB' must equal an invoicing sheet name.

If something breaks for this client, the log line carries the logger name
'approval_router.route_resolver.zalawi' and the failing step is one of the
numbered steps in resolve() below.
"""

from ..shared import folder_match
from ..util import client_logger

CLIENT_SLUG = "zalawi"
MAIN_SHEET_ID = 3698247053823876
INVOICING_FOLDER_ID = 6490485884774276
ROUTE_PREFIXES = ('zalawi',)   # normalized text before the first '-'

log = client_logger(CLIENT_SLUG)

# Offline/live self-test (python -m jobs.approval_router.route_resolver.selftest):
# (route, local_route, commodity, expected invoicing sheet_id). Snapshot of the
# live folder on 2026-10-28.
SELFTEST_CASES = [
    ('ZALAWI-BROTHER MINING-GRB', None, None, 3487957938818948),
    ('ZALAWI-CDM-GRB', None, None, 7988894686465924),
    ('ZALAWI-COMIKA-GRB', None, None, 810183268716420),
    ('ZALAWI-COMMUS-GRB', None, None, 3061983082401668),
    ('ZALAWI-LAMIKAL-GRB', None, None, 2922345105674116),
    ('ZALAWI-LCS-GRB', None, None, 3556814854508420),
    ('ZALAWI-LUILU MINE-GRB', None, None, 7565582709772164),
    ('ZALAWI-SICO MINE-GRB', None, None, 7338258680729476),
    ('ZALAWI-STL-GRB', None, None, 1586902334394244),
    ('ZALAWI-TFM-GRB', None, None, 7569293561515908),
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
