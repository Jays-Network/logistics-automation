"""North Star -- client file: country legs (DRC -> ZAM -> BOTS copies) + final move to SOUTH AFRICA.

Fully diverted BEFORE ROUTE matching (same flow as Gold Vale, set up 2026-11-11 per Jay). Every row on this client's main sheet follows the same
sequence; the mechanics live in shared/country_legs.py, the config below is this
client's own. Route: DRC -> Zambia -> Botswana -> South Africa. Sheets were copied from Gold Vale. The final
approval column ("Siphemandla Hleza approval") is watched here to trigger the move to SOUTH AFRICA.

Failure tracing: log lines come from logger 'approval_router.route_resolver.north_star'.
"""

from ..shared import country_legs
from ..util import client_logger

CLIENT_SLUG = "north_star"
MAIN_SHEET_ID = 2091711124950916
INVOICING_FOLDER_ID = 386498362140548

log = client_logger(CLIENT_SLUG)

CONFIG = {
    "country_approval_columns": {
        "DRC": ("DRC REGION: Approval", 6455140199649156),   # North Star/DRC
        "ZAM": ("ZAM REGION: Approval", 3553331445452676),   # North Star/ZAMBIA
        "BOTS": ("BOTS REGION: Approval", 5953462249672580),   # North Star/BOTSWANA
    },
    "final_approval_column": "Siphemandla Hleza approval",
    "final_destination_sheet_id": 3781228852629380,            # North Star/SOUTH AFRICA
}

SELFTEST_CASES: list = []   # never resolves a ROUTE to one sheet


def process_row(conn, ss_client, main_sheet_id, row, columns_by_id, stats, **ctx) -> bool:
    """Country-leg flow. Always handles the row (returns True)."""
    return country_legs.process_country_leg_row(
        conn, ss_client, CLIENT_SLUG, CONFIG, log, main_sheet_id, row, columns_by_id, stats)


def resolve(route_value, local_route_value, commodity_value, folder_cache):
    """Never reached: process_row() handles every row."""
    return None, "North Star uses the country-leg flow, not ROUTE matching"
