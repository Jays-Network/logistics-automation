"""process_sheet(): one client's main sheet -> approved, routed rows get moved."""

import logging

from jobs.approval_router.mover import move_row, get_move_log, SHEET_FULL_ERROR_CODE

from .alerts import alert_unresolvable_route
from .clients import bridge, gsm_goldvale
from .db import (get_unresolved_rows, upsert_unresolved, clear_unresolved,
                 get_sheet_delta_state, update_sheet_delta_state)
from .lock import touch_lock
from .resolver import resolve_target_sheet_id
from .util import find_column_id, find_approval_column, FolderSheetCache

logger = logging.getLogger("approval_router.route_resolver")


def process_sheet(conn, ss_client, folder_cache: "FolderSheetCache", client_slug: str, main_sheet_id: int,
                   known_bad_targets_this_run: set) -> dict[str, int]:
    """
    Checks one client's main sheet for approved, routed rows and moves them.
    Returns a small stats dict for logging. Never raises for a single bad
    row — logs and continues, same philosophy as mover.py.

    DELTA (2026-09-14): skips the full sheet fetch entirely if the sheet's
    version is unchanged since last run AND there are no rows in
    approval_router_unresolved_route_log for this sheet (those need
    re-checking regardless -- the fix for an unresolvable route is usually
    external, e.g. creating the missing target sheet, and never touches
    the source row/sheet). Once fetched, only rows that changed OR are in
    that pending-unresolved set get evaluated.

    known_bad_targets_this_run: shared across all clients in one run --
    if a target sheet turns out to be full, every other row destined for
    it this run is skipped immediately instead of individually retried.

    Per-client multi-leg/country-leg check (2026-09-16, Bridge rebuilt
    2026-09-23): Bridge's country-leg treatment and GSM/Gold Vale's
    country-leg treatment each do their OWN internal per-column approval
    gating (DRC/ZAM/ZIM/BOTS/final REGION: Approval columns, checked
    individually inside those functions) -- so they run BEFORE the single
    generic approval_value gate below, not after. The generic gate
    assumes one approval column per sheet; these clients now have
    several, so applying the generic gate first would filter every row on
    whichever REGION: Approval column find_approval_column() happens to
    pick, silently skipping rows that still need earlier-leg processing
    before that column is ever set. See find_approval_column()'s
    docstring for the related fix this pairs with.
    """
    stats = {"rows_checked": 0, "rows_moved": 0, "rows_skipped_no_route": 0,
              "rows_skipped_not_approved": 0, "rows_failed_resolve": 0, "skipped_known_bad_target": 0}

    unresolved_rows = get_unresolved_rows(conn, main_sheet_id)
    current_version = ss_client.Sheets.get_sheet_version(main_sheet_id).version
    state = get_sheet_delta_state(conn, main_sheet_id)
    last_version, last_checkpoint_at = state if state else (None, None)

    if last_version is not None and current_version == last_version and not unresolved_rows:
        logger.info("%s: unchanged (version %s), no pending unresolved rows — skipping", client_slug, current_version)
        return stats

    sheet = ss_client.Sheets.get_sheet(main_sheet_id)
    columns_by_id = {c.id: c.title for c in sheet.columns}

    route_col_id = find_column_id(columns_by_id, "ROUTE") or find_column_id(columns_by_id, "Routes")
    local_route_col_id = find_column_id(columns_by_id, "LOCAL ROUTE")
    commodity_col_id = find_column_id(columns_by_id, "COMMODITY")
    approval_col_id = find_approval_column(columns_by_id)

    if route_col_id is None:
        logger.error("No ROUTE/Routes column found on %s main sheet (sheet_id=%s) — skipping", client_slug, main_sheet_id)
        return stats
    if approval_col_id is None:
        logger.error("No approval column found on %s main sheet (sheet_id=%s) — skipping", client_slug, main_sheet_id)
        return stats

    ambiguous_bridge: list = []
    rows_errored = 0

    for row in sheet.rows:
        touch_lock()  # heartbeat: a live run is never mistaken for a zombie
        row_changed = last_checkpoint_at is None or (row.modified_at and row.modified_at > last_checkpoint_at)
        if not row_changed and row.id not in unresolved_rows:
            continue

        stats["rows_checked"] += 1

        # One bad row must never abort the whole client (the docstring has
        # always promised this): log it, roll back any half-finished DB
        # transaction so later rows aren't poisoned, remember the row so it
        # is retried next run, and carry on.
        try:
            cells_by_col = {cell.column_id: cell for cell in row.cells}

            route_cell = cells_by_col.get(route_col_id)
            route_value = (route_cell.display_value or route_cell.value) if route_cell else None
            if not route_value or not str(route_value).strip():
                stats["rows_skipped_no_route"] += 1
                continue
            route_value = str(route_value)

            # Bridge country-leg and GSM/Gold Vale country-leg handling both
            # run BEFORE the generic approval gate -- see this function's
            # docstring and find_approval_column()'s docstring for why.
            if client_slug == "bridge" and bridge.process_bridge_country_leg_row(
                conn, ss_client, main_sheet_id, row, columns_by_id, stats,
                route_value=route_value, ambiguous=ambiguous_bridge, unresolved_rows=unresolved_rows,
            ):
                continue
            if gsm_goldvale.process_gsm_goldvale_country_leg_row(
                conn, ss_client, client_slug, main_sheet_id, row, columns_by_id, stats
            ):
                continue

            approval_cell = cells_by_col.get(approval_col_id)
            approval_value = (approval_cell.display_value or approval_cell.value) if approval_cell else None
            if not approval_value or str(approval_value).strip().casefold() != "approved":
                stats["rows_skipped_not_approved"] += 1
                continue

            local_route_value = None
            if local_route_col_id is not None:
                local_cell = cells_by_col.get(local_route_col_id)
                local_route_value = (local_cell.display_value or local_cell.value) if local_cell else None
                local_route_value = str(local_route_value) if local_route_value else None

            commodity_value = None
            if commodity_col_id is not None:
                commodity_cell = cells_by_col.get(commodity_col_id)
                commodity_value = (commodity_cell.display_value or commodity_cell.value) if commodity_cell else None
                commodity_value = str(commodity_value) if commodity_value else None

            target_sheet_id, reason = resolve_target_sheet_id(route_value, local_route_value, folder_cache, commodity_value)
            if target_sheet_id is None:
                stats["rows_failed_resolve"] += 1
                logger.warning(
                    "Could not resolve target for row %s on %s (ROUTE=%r, LOCAL ROUTE=%r, COMMODITY=%r): %s",
                    row.id, client_slug, route_value, local_route_value, commodity_value, reason,
                )
                alert_unresolvable_route(conn, client_slug, route_value, local_route_value, reason)
                upsert_unresolved(conn, main_sheet_id, row.id, client_slug, route_value, reason)
                continue

            clear_unresolved(conn, main_sheet_id, row.id)

            if target_sheet_id in known_bad_targets_this_run:
                stats["skipped_known_bad_target"] += 1
                continue

            move_log_id = move_row(
                conn, ss_client,
                source_sheet_id=main_sheet_id,
                source_row=row,
                columns_by_id=columns_by_id,
                target_sheet_id=target_sheet_id,
                client_slug=client_slug,
                route_value=route_value,
                mine_value=local_route_value,
            )
            move_log = get_move_log(conn, move_log_id)
            if move_log and move_log["status"] == "error" and move_log.get("error_code") == SHEET_FULL_ERROR_CODE:
                known_bad_targets_this_run.add(target_sheet_id)
                logger.warning("Target sheet %s marked bad for the rest of this run", target_sheet_id)
            stats["rows_moved"] += 1
            logger.info("Row %s on %s moved (ROUTE=%r) -> move_log_id=%s", row.id, client_slug, route_value, move_log_id)
        except Exception as exc:
            rows_errored += 1
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error("Row %s on %s failed unexpectedly (skipping, will retry next run): %s",
                         row.id, client_slug, exc, exc_info=True)
            try:
                upsert_unresolved(conn, main_sheet_id, row.id, client_slug, "", f"unexpected error: {exc}"[:500])
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

    stats["rows_errored"] = rows_errored

    if ambiguous_bridge:
        try:
            bridge.alert_bridge_final_destination_ambiguous(conn, ambiguous_bridge)
        except Exception as exc:
            conn.rollback()
            logger.warning("Bridge ambiguous-row summary alert failed: %s", exc)

    update_sheet_delta_state(conn, main_sheet_id, client_slug, sheet.name, current_version)
    return stats
