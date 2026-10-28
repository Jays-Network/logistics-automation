"""Country-leg engine used by GSM and Gold Vale (their files hold the config;
this holds the mechanics). Logic is unchanged from the old gsm_goldvale.py.

Per country: when "<X> REGION: Approval" reads Approved, COPY the row to that
country's sheet (once, de-duplicated through gsm_goldvale_country_copy_log).
When the client's final-delivery approval column reads Approved, MOVE the row
to the SOUTH AFRICA sheet. No ROUTE filtering: every row follows the sequence.
"""

from jobs.approval_router.copier import copy_row, CopierError
from jobs.approval_router.mover import move_row


def _already_copied(conn, source_sheet_id: int, source_row_id: int, country: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM gsm_goldvale_country_copy_log "
            "WHERE source_sheet_id = %s AND source_row_id = %s AND country = %s AND status = 'copied'",
            (source_sheet_id, source_row_id, country),
        )
        return cur.fetchone() is not None


def _upsert_copy_log(conn, client_slug, source_sheet_id, source_row_id, country,
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


def process_country_leg_row(conn, ss_client, client_slug: str, config: dict, log, main_sheet_id: int, row,
                            columns_by_id: dict, stats: dict) -> bool:
    """Runs one row through the country-leg flow described by `config`
    (country_approval_columns / final_approval_column / final_destination_sheet_id).
    Always returns True: the caller must not also try normal ROUTE matching."""
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
        if _already_copied(conn, main_sheet_id, row.id, country):
            continue
        try:
            target_row_id = copy_row(ss_client, main_sheet_id, row.id, target_sheet_id)
            _upsert_copy_log(conn, client_slug, main_sheet_id, row.id, country,
                             target_sheet_id, target_row_id, "copied")
            stats["gsm_goldvale_country_copies"] = stats.get("gsm_goldvale_country_copies", 0) + 1
            log.info("country leg: copied row %s to %s sheet %s", row.id, country, target_sheet_id)
        except CopierError as exc:
            _upsert_copy_log(conn, client_slug, main_sheet_id, row.id, country,
                             target_sheet_id, None, "error", str(exc))
            log.warning("country leg: copy to %s failed for row %s: %s", country, row.id, exc)

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
            log.info("final delivery approved, row %s moved to SOUTH AFRICA sheet", row.id)

    return True
