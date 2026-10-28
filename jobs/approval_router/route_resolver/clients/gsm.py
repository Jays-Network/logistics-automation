"""GSM -- client file: country legs (DRC -> ZAM -> BOTS copies) + final move to SOUTH AFRICA.

Fully diverted BEFORE ROUTE matching (2026-09-16, per Jay: "no conditions needed
for GSM and GoldVale"). Every row on this client's main sheet follows the same
sequence; the mechanics live in shared/country_legs.py, the config below is this
client's own. The final-delivery approval column ("Leron Wagner approval") keeps its own
pre-existing Smartsheet automation; this code only WATCHES it to trigger the move.

Failure tracing: log lines come from logger 'approval_router.route_resolver.gsm'.
"""

from ..shared import country_legs
from ..util import client_logger

CLIENT_SLUG = "gsm"
MAIN_SHEET_ID = 5438660737453956
INVOICING_FOLDER_ID = 5399770350020484

log = client_logger(CLIENT_SLUG)

CONFIG = {
    "country_approval_columns": {
        "DRC": ("DRC REGION: Approval", 455902156246916),   # GSM/DRC
        "ZAM": ("ZAM REGION: Approval", 5241320357711748),   # GSM/ZAMBIA
        "BOTS": ("BOTS REGION: Approval", 2707092084576132),   # GSM/BOTSWANA
    },
    "final_approval_column": "Leron Wagner approval",
    "final_destination_sheet_id": 8760307322408836,            # GSM/SOUTH AFRICA
}

SELFTEST_CASES: list = []   # never resolves a ROUTE to one sheet


def process_row(conn, ss_client, main_sheet_id, row, columns_by_id, stats, **ctx) -> bool:
    """Country-leg flow. Always handles the row (returns True)."""
    return country_legs.process_country_leg_row(
        conn, ss_client, CLIENT_SLUG, CONFIG, log, main_sheet_id, row, columns_by_id, stats)


def resolve(route_value, local_route_value, commodity_value, folder_cache):
    """Never reached: process_row() handles every row."""
    return None, "GSM uses the country-leg flow, not ROUTE matching"
