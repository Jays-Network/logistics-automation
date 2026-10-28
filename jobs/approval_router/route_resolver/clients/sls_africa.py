"""SLS Africa -- NDOLA-DAR / NDOLA-GRB converge onto NDOLA-PORT."""

from ..shared import folder_match
from ..util import client_logger, norm

CLIENT_SLUG = "sls_africa"
MAIN_SHEET_ID = 3849847846162308
INVOICING_FOLDER_ID = 6807145233573764
ROUTE_PREFIXES = ("sls africa",)   # normalized text before the first '-'

log = client_logger(CLIENT_SLUG)


# route (normalized) -> invoicing sheet_id. Merged into the global override
# table by resolver.py.
ROUTE_OVERRIDES: dict[str, int] = {}

SLS_AFRICA_NDOLA_PORT = 4990485677690756

# SLS Africa NDOLA consolidation, 2026-09-16 (per Jay): NDOLA-DAR and
# NDOLA-GRB both now converge onto one NDOLA-PORT sheet. Both old sheets
# confirmed empty before deletion -- no data migration needed.
# NDOLA-NAKONDE removed entirely (not replaced): no override needed since
# it's no longer a selectable ROUTE option once Jay finishes removing it
# from the picklist (the sheet-deletion half of this cleanup ran live;
# the ROUTE-option removal is still pending on his end).
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): SLS_AFRICA_NDOLA_PORT for v in (
        "SLS Africa-NDOLA-DAR", "SLS Africa-NDOLA-GRB",
    )
})

# Added 2026-10-28 (per Jay): two new routes. NO code needed -- each resolves by
# the normal folder + exact-sheet-name match once the ROUTE picklist option is
# added with exactly the same text as the sheet name:
#   "SLS Africa-NDOLA - WALVIS BAY"  -> sheet_id 4806276258418564
#   "SLS Africa-SAKANIA - CDM MINE"  -> sheet_id 6356484574367620


def process_row(conn, ss_client, main_sheet_id, row, columns_by_id, stats, **ctx) -> bool:
    """No custom per-row flow for this client."""
    return False


SELFTEST_CASES = [(route, None, None, sheet_id) for route, sheet_id in ROUTE_OVERRIDES.items()] + [
    ("SLS Africa-NDOLA - WALVIS BAY", None, None, 4806276258418564),
    ("SLS Africa-SAKANIA - CDM MINE", None, None, 6356484574367620),
    ("SLS Africa- SAKANIA - EXCELLENT MINE", None, None, 5553750640119684),
    ("SLS Africa-DRC-NDOLA", None, None, 1514970222120836),
]


def resolve(route_value, local_route_value, commodity_value, folder_cache):
    """Returns (sheet_id_or_None, reason)."""
    normalized_route = norm(route_value)
    # 1. Known override table (NDOLA-DAR / NDOLA-GRB -> NDOLA-PORT).
    if normalized_route in ROUTE_OVERRIDES:
        return ROUTE_OVERRIDES[normalized_route], "known override"
    # 2. Otherwise the whole ROUTE must equal a sheet name in the folder.
    sheet_id, reason = folder_match.resolve_in_folder(
        CLIENT_SLUG, ROUTE_PREFIXES, INVOICING_FOLDER_ID, route_value, folder_cache)
    if sheet_id is None:
        log.debug("step 2 (folder match) failed: %s", reason)
    return sheet_id, reason
