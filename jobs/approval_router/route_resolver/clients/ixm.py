"""IXM -- ROUTE 'IXM-Lonshi-Dar' is split by the COMMODITY column."""

from ..shared import folder_match
from ..util import client_logger, norm

CLIENT_SLUG = "ixm"
MAIN_SHEET_ID = 6551095343009668
INVOICING_FOLDER_ID = 5012742257043332
ROUTE_PREFIXES = ("ixm",)

log = client_logger(CLIENT_SLUG)

# IXM's commodity split, 2026-09-16 (per Jay): ROUTE stays "IXM-Lonshi-Dar"
# unchanged -- the new COMMODITY column (PICKLIST, confirmed live: exactly
# "COPPER CATHODES" / "COPPER CONCENTRATE", no other options) picks which
# of two new sheets the row goes to instead of the old single sheet, which
# is being retired. Both new sheets created from the old sheet as template
# (same mechanism as every other sheet in this project) -- confirmed
# structurally identical, including the COMMODITY/ROUTE/approval columns.
IXM_TRIGGER_ROUTE = "ixm-lonshi-dar"
IXM_COPPER_CATHODES = 5937869698060164
IXM_COPPER_CONCENTRATE = 6012143507033988
IXM_COMMODITY_MAP: dict[str, int] = {
    "copper cathodes": IXM_COPPER_CATHODES,
    "copper concentrate": IXM_COPPER_CONCENTRATE,
}


def _resolve_commodity(commodity_value):
    """Returns (sheet_id_or_None, reason) -- same contract as resolve_target_sheet_id()."""
    if not commodity_value or not commodity_value.strip():
        return None, "IXM-Lonshi-Dar but COMMODITY column is blank"
    normalized_commodity = norm(commodity_value)
    sheet_id = IXM_COMMODITY_MAP.get(normalized_commodity)
    if sheet_id is None:
        return None, f"IXM COMMODITY value not in known map: {commodity_value!r}"
    return sheet_id, "IXM commodity map"


def process_row(conn, ss_client, main_sheet_id, row, columns_by_id, stats, **ctx) -> bool:
    """No custom per-row flow for this client."""
    return False


SELFTEST_CASES = [
    ("IXM-Lonshi-Dar", None, "COPPER CATHODES", IXM_COPPER_CATHODES),
    ("IXM-Lonshi-Dar", None, "COPPER CONCENTRATE", IXM_COPPER_CONCENTRATE),
]


def resolve(route_value, local_route_value, commodity_value, folder_cache):
    """Returns (sheet_id_or_None, reason)."""
    # 1. The one live ROUTE ("IXM-Lonshi-Dar") is split by the COMMODITY column.
    if norm(route_value) == IXM_TRIGGER_ROUTE:
        return _resolve_commodity(commodity_value)
    # 2. Any other ROUTE: whole value must equal a sheet name in the folder.
    sheet_id, reason = folder_match.resolve_in_folder(
        CLIENT_SLUG, ROUTE_PREFIXES, INVOICING_FOLDER_ID, route_value, folder_cache)
    if sheet_id is None:
        log.debug("step 2 (folder match) failed: %s", reason)
    return sheet_id, reason
