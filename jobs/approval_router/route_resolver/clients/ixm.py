"""IXM -- ROUTE 'IXM-Lonshi-Dar' is split by the COMMODITY column."""

from ..util import norm

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


def resolve(commodity_value):
    """Returns (sheet_id_or_None, reason) -- same contract as resolve_target_sheet_id()."""
    if not commodity_value or not commodity_value.strip():
        return None, "IXM-Lonshi-Dar but COMMODITY column is blank"
    normalized_commodity = norm(commodity_value)
    sheet_id = IXM_COMMODITY_MAP.get(normalized_commodity)
    if sheet_id is None:
        return None, f"IXM COMMODITY value not in known map: {commodity_value!r}"
    return sheet_id, "IXM commodity map"
