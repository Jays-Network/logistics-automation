"""GSM + Gold Vale -- country-leg copies (DRC/ZAM/BOTS) + final move to SOUTH AFRICA.

Both fully diverted BEFORE ROUTE matching (2026-09-16), no ROUTE filtering.
"""

import logging

from jobs.approval_router.copier import copy_row, CopierError
from jobs.approval_router.mover import move_row

logger = logging.getLogger("approval_router.route_resolver")

# GSM/Gold Vale country-leg copy+move, 2026-09-16 (per Jay: "same logic
# completely" as Bridge's multi-leg treatment, applied to both clients at
# once). Deliberately simpler than Bridge's version -- NO ROUTE filtering:
# every row on these two clients' main sheets goes through the same
# DRC -> ZAM -> BOTS -> SA sequence, since (per Jay) "no conditions needed
# for GSM and GoldVale". Both clients' old per-route invoicing sheets
# (GSM-DRC-DBN, GSM-KAZ-DBN, GSM-MOKAMBO-DBN, GSM-GRB-DBN,
# GOLDVALE-COMMUS-DBN, GOLDVALE-KAMOA MINE-DBN, GOLDVALE-LCS-DBN) were
# deleted the same day these new country sheets were created -- so unlike
# Bridge (which is now also fully diverted before ROUTE matching, as of
# 2026-09-23), GSM and Gold Vale ROUTE values never reach
# resolve_target_sheet_id() at all anymore; there's nothing left in
# their invoicing folders for it to match against.
#
# SA is deliberately NOT one of the three country-leg approval columns
# here (unlike DRC/ZAM/BOTS) -- per Jay, the existing final-delivery
# approval columns (GSM's "Leron Wagner approval", Gold Vale's
# "Siphemandla Hleza approval") stay exactly as they are, unrenamed, with
# their own pre-existing Smartsheet automations gating them to Approved.
# This function only WATCHES that final column to trigger the move; it
# doesn't touch how the column gets set.
GSM_GOLDVALE_CONFIG: dict[str, dict] = {
    "gsm": {
        "country_approval_columns": {
            "DRC": ("DRC REGION: Approval", 455902156246916),      # GSM/DRC
            "ZAM": ("ZAM REGION: Approval", 5241320357711748),     # GSM/ZAMBIA
            "BOTS": ("BOTS REGION: Approval", 2707092084576132),   # GSM/BOTSWANA
        },
        "final_approval_column": "Leron Wagner approval",
        "final_destination_sheet_id": 8760307322408836,            # GSM/SOUTH AFRICA
    },
    "goldvale": {
        "country_approval_columns": {
            "DRC": ("DRC REGION: Approval", 175320532733828),      # Gold Vale/DRC
            "ZAM": ("ZAM REGION: Approval", 1998345171324804),     # Gold Vale/ZAMBIA
            "BOTS": ("BOTS REGION: Approval", 7210691711946628),   # Gold Vale/BOTSWANA
        },
        "final_approval_column": "Siphemandla Hleza approval",
        "final_destination_sheet_id": 4256303968112516,            # Gold Vale/SOUTH AFRICA
    },
}


def _gsm_goldvale_country_already_copied(conn, source_sheet_id: int, source_row_id: int, country: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM gsm_goldvale_country_copy_log "
            "WHERE source_sheet_id = %s AND source_row_id = %s AND country = %s AND status = 'copied'",
            (source_sheet_id, source_row_id, country),
        )
        return cur.fetchone() is not None


def _upsert_gsm_goldvale_country_copy_log(conn, client_slug, source_sheet_id, source_row_id, country,
                                           target_sheet_id, target_row_id, status, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gsm_goldvale_country_copy_log "
            "(client_slug, source_sheet_id, source_row_id, country, target_sheet_id, target_row_id, status, error_message, copied_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (source_sheet_id, source_row_id, country) DO UPDATE SET "
            "target_sheet_id = EXCLUDED.target_sheet_id, target_row_id = EXCLUDED.target_row_id, "
            "status = EXCLUDED.status, error_message = EXCLUDED.error_message, "
            "copied_at = EXCLUDED.copied_at",
            (client_slug, source_sheet_id, source_row_id, country, target_sheet_id, target_row_id, status, error_message),
        )
    conn.commit()


def process_gsm_goldvale_country_leg_row(conn, ss_client, client_slug: str, main_sheet_id: int, row,
                                           columns_by_id: dict, stats: dict) -> bool:
    """
    Handles GSM/Gold Vale's country-leg copy+move treatment. Returns True
    if client_slug is one of the two configured in GSM_GOLDVALE_CONFIG
    (handled here, regardless of whether anything actually fired this
    call -- caller must NOT also run normal resolve_target_sheet_id() for
    this row), False for every other client.
    """
    config = GSM_GOLDVALE_CONFIG.get(client_slug)
    if config is None:
        return False

    cells_by_col = {cell.column_id: cell for cell in row.cells}
    name_to_id = {title: col_id for col_id, title in columns_by_id.items()}

    for country, (approval_col_name, target_sheet_id) in config["country_approval_columns"].items():
        approval_col_id = name_to_id.get(approval_col_name)
        if approval_col_id is None:
            continue  # column not found -- e.g. not yet created on this sheet
        cell = cells_by_col.get(approval_col_id)
        value = (cell.display_value or cell.value) if cell else None
        if not value or str(value).strip().casefold() != "approved":
            continue
        if _gsm_goldvale_country_already_copied(conn, main_sheet_id, row.id, country):
            continue
        try:
            target_row_id = copy_row(ss_client, main_sheet_id, row.id, target_sheet_id)
            _upsert_gsm_goldvale_country_copy_log(conn, client_slug, main_sheet_id, row.id, country,
                                                   target_sheet_id, target_row_id, "copied")
            stats["gsm_goldvale_country_copies"] = stats.get("gsm_goldvale_country_copies", 0) + 1
            logger.info("%s country leg: copied row %s to %s sheet %s", client_slug, row.id, country, target_sheet_id)
        except CopierError as exc:
            _upsert_gsm_goldvale_country_copy_log(conn, client_slug, main_sheet_id, row.id, country,
                                                   target_sheet_id, None, "error", str(exc))
            logger.warning("%s country leg: copy to %s failed for row %s: %s", client_slug, country, row.id, exc)

    final_col_id = name_to_id.get(config["final_approval_column"])
    if final_col_id is not None:
        cell = cells_by_col.get(final_col_id)
        value = (cell.display_value or cell.value) if cell else None
        if value and str(value).strip().casefold() == "approved":
            route_col_id = name_to_id.get("ROUTE")
            route_cell = cells_by_col.get(route_col_id) if route_col_id else None
            route_value = (route_cell.display_value or route_cell.value) if route_cell else None
            move_row(
                conn, ss_client,
                source_sheet_id=main_sheet_id,
                source_row=row,
                columns_by_id=columns_by_id,
                target_sheet_id=config["final_destination_sheet_id"],
                client_slug=client_slug,
                route_value=str(route_value) if route_value else None,
            )
            stats["rows_moved"] = stats.get("rows_moved", 0) + 1
            logger.info("%s: final delivery approved, row %s moved to SOUTH AFRICA sheet", client_slug, row.id)

    return True
