"""Bridge -- country-leg copies + final-destination move (rebuilt 2026-09-23).

Bridge is fully diverted BEFORE ROUTE matching: every Bridge row goes through
process_bridge_country_leg_row(), never resolve_target_sheet_id().
"""

import logging
import os

import smartsheet

from jobs.approval_router.backup import row_to_dict, backup_row
from jobs.approval_router.copier import copy_row, CopierError
from jobs.approval_router.mover import move_row, get_move_log

from ..alerts import send_telegram
from ..config import JOB_NAME
from ..db import upsert_unresolved, clear_unresolved

logger = logging.getLogger("approval_router.route_resolver")

BRIDGE_BOTSWANA = 7242925575720836
BRIDGE_SA = 2880608897552260
BRIDGE_ADHOC_TAGGING = 5307589689823108
BRIDGE_SPD_DRC = 4189968332443524
# Country-sheet rebuild, 2026-09-23 (per Jay: "convert it to all routes"):
# no more ROUTE-keyed consolidation sheets for Bridge -- the 12 old ones
# (formerly BRIDGE_KASUMBALESA, BRIDGE_IMPEX_NDOLA, BRIDGE_SAKANIA,
# BRIDGE_CCS_CHAMBISHI, BRIDGE_KCM, BRIDGE_LUANSHYA,
# BRIDGE_CHIBOMBO_KAZUNGULA, BRIDGE_SABLE_NAKONDE, plus 4 more with no
# real data) were retired live the same day this shipped -- 2 had real
# rows (IMPEX NDOLA 14, LUANSHYA 8), both confirmed Zambia by Jay and
# migrated into BRIDGE_ZAMBIA before deletion. Replaced by 8
# country-level sheets: 4 copy-stage (DRC/ZAM/ZIM/BOTS, below) and 4
# final-destination (TAN/NAM/SA/MOZ, below).
BRIDGE_ZAMBIA = 4259521972359044
BRIDGE_TANZANIA = 8997865855864708
BRIDGE_NAMIBIA = 2580636771766148
BRIDGE_ZIMBABWE = 1669528179396484
BRIDGE_MOZAMBIQUE = 6173127806766980

# Bridge consolidation, 2026-09-16 (per Jay): many ROUTE values now
# converge on far fewer invoicing sheets. All simple many-to-one direct
# overrides -- no secondary column needed, unlike RELOAD/IXM -- except
# Adhoc Tagging, which is a separate pre-check (see
# _bridge_adhoc_tagging_triggered below), and SPD-DRC, which simplifies
# an entire long DRC REGION: LEG value list down to one ROUTE trigger
# (Bridge-DRC-LOCAL) per Jay's explicit choice not to key off the leg.
# Audited live 2026-09-16: only BRIDGE-IMPEX-KATIMA had real data (6
# rows, migrated separately) -- every other old individual sheet these
# replace was empty.

# Adhoc Tagging: NOT keyed on ROUTE at all -- per Jay, TAGGING ONLY on
# ANY of the 9 region "...SERVICES REQUIRED" columns overrides normal
# ROUTE-based resolution and sends the row here instead. Checked before
# the normal resolve_target_sheet_id() call in process_client_rows.
BRIDGE_SERVICES_REQUIRED_COLUMNS = [
    "SOUTH AFRICA: SERVICES REQUIRED", "DRC: SERVICES REQUIRED",
    "ZAMBIA: SERVICES REQUIRED", "ZIMBABWE: SERVICES REQUIRED",
    "TAN: SERVICES REQUIRED", "NAM: SERVICES REQUIRED",
    "BOTSWANA: SERVICES REQUIRED", "MOZ: SERVICES REQUIRED",
    "MALAWI: SERVICES REQUIRED",
]


def _bridge_adhoc_tagging_triggered(cells_by_col: dict, columns_by_id: dict) -> bool:
    """True if ANY of Bridge's 9 region SERVICES REQUIRED columns is
    exactly 'TAGGING ONLY' on this row."""
    name_to_id = {title: col_id for col_id, title in columns_by_id.items()}
    for col_name in BRIDGE_SERVICES_REQUIRED_COLUMNS:
        col_id = name_to_id.get(col_name)
        if col_id is None:
            continue
        cell = cells_by_col.get(col_id)
        value = (cell.display_value or cell.value) if cell else None
        if value and str(value).strip().casefold() == "tagging only":
            return True
    return False


# Bridge country-sheet rebuild, 2026-09-23 (per Jay: "convert it to all
# routes" -- no ROUTE filtering at all anymore, same no-conditions
# approach GSM/Gold Vale already use). Every Bridge row goes through the
# same 4 independent copy-stage checks regardless of ROUTE, plus one
# final-delivery gate that now resolves to one of 4 destinations instead
# of 2 -- see process_bridge_country_leg_row() below.
#
# Four copy-stage countries: DRC, ZAM, ZIM, BOTS. Each has its own
# REGION: Approval column -- ZIM's was added live 2026-09-23 (previously
# missing; DRC/ZAM/BOTS already existed from the 2026-09-16 build).
# country -> (its own REGION: Approval column name, its own sheet_id)
# NOTE: these keys are written directly into bridge_country_copy_log.country,
# which has a CHECK constraint (migration 016, extended by migration 018)
# restricted to the FULL country names -- 'DRC', 'ZAMBIA', 'ZIMBABWE',
# 'BOTSWANA'. Do not shorten these to 'ZAM'/'ZIM'/'BOTS' (that abbreviated
# style is fine on gsm_goldvale_country_copy_log, which has no CHECK
# constraint at all, but would violate this one).
BRIDGE_COUNTRY_APPROVAL_COLUMNS = {
    "DRC": ("DRC REGION: Approval", BRIDGE_SPD_DRC),
    "ZAMBIA": ("ZAM REGION: Approval", BRIDGE_ZAMBIA),
    "ZIMBABWE": ("ZIM REGION: Approval", BRIDGE_ZIMBABWE),
    "BOTSWANA": ("BOTS REGION: Approval", BRIDGE_BOTSWANA),
}

# Final destination -- 4 possible sheets now (TAN/NAM/SA/MOZ), still
# gated by the single "Siphemandla Hleza delivery approval" column.
# Which of the 4 a row moves to is decided by checking which country's
# own REGION: ARRIVAL DATE is filled in -- confirmed by Jay 2026-09-23:
# "if sa has a date and other 3 are blank it moves to sa". Exactly one
# must be filled; zero or more than one is ambiguous and the row is
# skipped + flagged rather than guessed (see the fail-safe in
# process_bridge_country_leg_row below).
# country -> (its own REGION: ARRIVAL DATE column name, its own sheet_id)
BRIDGE_FINAL_DESTINATION_DATE_COLUMNS = {
    "TAN": ("TAN REGION: ARRIVAL DATE", BRIDGE_TANZANIA),
    "NAM": ("NAM REGION: ARRIVAL DATE", BRIDGE_NAMIBIA),
    "SA": ("SA REGION: ARRIVAL DATE", BRIDGE_SA),
    "MOZ": ("MOZ REGION: ARRIVAL DATE", BRIDGE_MOZAMBIQUE),
}


def _bridge_country_already_copied(conn, source_sheet_id: int, source_row_id: int, country: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM bridge_country_copy_log "
            "WHERE source_sheet_id = %s AND source_row_id = %s AND country = %s AND status = 'copied'",
            (source_sheet_id, source_row_id, country),
        )
        return cur.fetchone() is not None


def _upsert_bridge_country_copy_log(conn, source_sheet_id, source_row_id, country,
                                     target_sheet_id, target_row_id, status, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bridge_country_copy_log "
            "(source_sheet_id, source_row_id, country, target_sheet_id, target_row_id, status, error_message, copied_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (source_sheet_id, source_row_id, country) DO UPDATE SET "
            "target_sheet_id = EXCLUDED.target_sheet_id, target_row_id = EXCLUDED.target_row_id, "
            "status = EXCLUDED.status, error_message = EXCLUDED.error_message, "
            "copied_at = EXCLUDED.copied_at",
            (source_sheet_id, source_row_id, country, target_sheet_id, target_row_id, status, error_message),
        )
    conn.commit()


# What to do when Bridge's final delivery is Approved but NONE of the
# TAN/NAM/SA/MOZ arrival dates is filled (the journey ended in a copy-stage
# country -- Zambia, Botswana, Zimbabwe or DRC -- so there is no final
# destination sheet to move to). Added 2026-10-09 after 226 such rows
# were found stuck on the tracking sheet.
#   "flag"   -- (default, safe) leave the row where it is and alert once.
#   "remove" -- if the row was already copied to a country sheet AND that
#               copy is confirmed to still exist there, back the row up and
#               delete it from the tracking sheet. The country-sheet copy
#               becomes the invoicing record, so nothing is duplicated.
# Rows with no confirmed copy are ALWAYS flagged, never removed. More than
# one date filled is always flagged. Flip to "remove" once Jay confirms.
BRIDGE_ZERO_DATE_ACTION = "remove"   # set by Jay 2026-10-09, subject to the safety checks below

# Safety checks before ANY removal (all must pass, otherwise the row is
# left in place and flagged):
#   1. final delivery approval = Approved
#   2. none of the TAN/NAM/SA/MOZ arrival dates is filled (if one is, the
#      row is a final-destination row and is MOVED, never removed)
#   3. NO cell in any TAN/NAM/SA/MOZ "<X> REGION..." column has a value --
#      any activity there means the row may still be heading to final
#      approval, so it must not be removed
#   4. at least one country-sheet copy is logged AND still exists live
#   5. at most BRIDGE_MAX_REMOVALS_PER_RUN removals per run (blast-radius cap)
BRIDGE_MAX_REMOVALS_PER_RUN = 100
# 2026-10-11: country-sheet copies are archived/moved on by another job, so a
# logged copy is often legitimately gone by the time the row is approved.
# When True, a row is still removable if EVERY logged copy gives a definite
# Smartsheet 404 "Not Found" (not a timeout/other error), the copy log says
# status='copied' with a target_row_id, AND the pre-delete backup is read
# back and verified. Rows with no logged copy (or only error/null entries)
# are never removed.
BRIDGE_ACCEPT_ARCHIVED_COPIES = True


def _bridge_final_region_activity(cells_by_col: dict, columns_by_id: dict) -> list:
    """Names of TAN/NAM/SA/MOZ region columns that hold a value on this row."""
    prefixes = tuple(
        name.split(" REGION")[0].strip().upper() + " REGION"
        for name, _sid in BRIDGE_FINAL_DESTINATION_DATE_COLUMNS.values()
    )
    hits = []
    for col_id, title in columns_by_id.items():
        if not str(title).strip().upper().startswith(prefixes):
            continue
        cell = cells_by_col.get(col_id)
        value = (cell.display_value or cell.value) if cell else None
        if value is not None and str(value).strip():
            hits.append(title)
    return hits


def _bridge_confirmed_copies(conn, ss_client, source_sheet_id: int, source_row_id: int) -> list:
    """Returns [(country, target_sheet_id, target_row_id)] for this row's
    logged country-sheet copies that STILL EXIST on the country sheet
    (re-checked live -- a copy the archive job already moved on doesn't
    count as a safe record)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT country, target_sheet_id, target_row_id FROM bridge_country_copy_log "
            "WHERE source_sheet_id = %s AND source_row_id = %s AND status = 'copied' "
            "AND target_row_id IS NOT NULL",
            (source_sheet_id, source_row_id),
        )
        logged = cur.fetchall()
    confirmed = []
    for country, target_sheet_id, target_row_id in logged:
        try:
            resp = ss_client.Sheets.get_row(target_sheet_id, target_row_id)
        except Exception as exc:
            logger.warning("Bridge: could not verify %s copy of row %s: %s", country, source_row_id, exc)
            continue
        if isinstance(resp, smartsheet.models.Error) or getattr(resp, "id", None) is None:
            continue
        confirmed.append((country, target_sheet_id, target_row_id))
    return confirmed


def _is_not_found(resp) -> bool:
    if not isinstance(resp, smartsheet.models.Error):
        return False
    result = getattr(resp, "result", None)
    return getattr(result, "status_code", None) == 404 or getattr(result, "error_code", None) == 1006


def _bridge_archived_copies(conn, ss_client, source_sheet_id: int, source_row_id: int) -> list:
    """[(country, target_sheet_id, target_row_id)] for logged copies whose live
    check returned a definite 404. Returns [] unless there is at least one
    such copy AND no logged copy gave an uncertain result (timeout etc.)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT country, target_sheet_id, target_row_id FROM bridge_country_copy_log "
            "WHERE source_sheet_id = %s AND source_row_id = %s AND status = 'copied' "
            "AND target_row_id IS NOT NULL",
            (source_sheet_id, source_row_id),
        )
        logged = cur.fetchall()
    gone = []
    for country, target_sheet_id, target_row_id in logged:
        try:
            resp = ss_client.Sheets.get_row(target_sheet_id, target_row_id)
        except Exception as exc:
            logger.warning("Bridge: could not verify %s copy of row %s: %s", country, source_row_id, exc)
            return []
        if _is_not_found(resp):
            gone.append((country, target_sheet_id, target_row_id))
        elif isinstance(resp, smartsheet.models.Error):
            return []   # uncertain -> never treat as archived
        else:
            return []   # a live copy exists; caller should have used the confirmed path
    return gone


def _bridge_remove_row_after_copy(conn, ss_client, main_sheet_id: int, row, columns_by_id: dict,
                                   confirmed_copies: list, route_value: str | None) -> bool:
    """Backs the row up, deletes it from the tracking sheet, and records it
    in approval_router_row_move_log as status='deleted'. Only called when
    confirmed_copies is non-empty. Returns True only if the delete
    succeeded."""
    country, target_sheet_id, target_row_id = confirmed_copies[-1]
    row_data = row_to_dict(row, columns_by_id)
    backup_id = backup_row(conn, main_sheet_id, row.id, "bridge", row_data)
    conn.commit()

    # Read the backup back and make sure it really holds this row before the
    # delete. If it doesn't, leave the row alone.
    with conn.cursor() as cur:
        cur.execute("SELECT row_data FROM approval_router_row_backup WHERE id = %s", (backup_id,))
        stored = cur.fetchone()
    stored_data = stored[0] if stored else None
    if isinstance(stored_data, str):
        import json as _json
        stored_data = _json.loads(stored_data)
    if not stored_data or stored_data.get("INTERNAL REF:") != row_data.get("INTERNAL REF:"):
        logger.error("Bridge: backup %s for row %s failed read-back verification -- NOT deleting", backup_id, row.id)
        return False

    resp = ss_client.Sheets.delete_rows(main_sheet_id, [row.id])
    if isinstance(resp, smartsheet.models.Error):
        logger.error("Bridge: delete of row %s failed after backup_id=%s: %s", row.id, backup_id,
                     getattr(resp.result, "message", resp))
        return False

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO approval_router_row_move_log "
                "(backup_id, source_sheet_id, source_row_id, target_sheet_id, target_row_id, "
                " client_slug, route_value, status) VALUES (%s, %s, %s, %s, %s, 'bridge', %s, 'deleted')",
                (backup_id, main_sheet_id, row.id, target_sheet_id, target_row_id, route_value),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.warning("Bridge: row %s deleted (backup_id=%s) but move-log insert failed: %s",
                       row.id, backup_id, exc)
    logger.info("Bridge: final delivery approved, no final-country date -- row %s removed from tracking "
                "(record kept on %s sheet %s, backup_id=%s)", row.id, country, target_sheet_id, backup_id)
    return True


def _bridge_move_checked(conn, ss_client, main_sheet_id: int, row, columns_by_id: dict,
                          target_sheet_id: int, route_value: str | None) -> bool:
    """move_row() never raises for a post-backup failure -- it records
    status='error' and returns. Only report success if the move log says
    the move actually happened."""
    move_log_id = move_row(
        conn, ss_client,
        source_sheet_id=main_sheet_id,
        source_row=row,
        columns_by_id=columns_by_id,
        target_sheet_id=target_sheet_id,
        client_slug="bridge",
        route_value=route_value,
    )
    move_log = get_move_log(conn, move_log_id)
    ok = bool(move_log) and move_log["status"] in ("copied", "verified")
    if not ok:
        logger.error("Bridge: move of row %s to sheet %s did NOT succeed (move_log_id=%s, status=%s)",
                     row.id, target_sheet_id, move_log_id, move_log["status"] if move_log else None)
    return ok


def alert_bridge_final_destination_ambiguous(conn, ambiguous: list):
    """
    ONE summary alert per run (de-duped to once per 24h) listing every
    Bridge row whose final delivery is approved but whose final
    destination could not be determined. Previously this alerted once per
    row, which meant a bulk approval produced hundreds of Telegram
    messages. `ambiguous` is a list of (internal_ref, filled_countries,
    reason) tuples.
    """
    if not ambiguous:
        return
    dedupe_key = "bridge-final-dest-ambiguous-summary"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM automation_telegram_alert_log "
            "WHERE job_name = %s AND message LIKE %s AND sent_at > now() - interval '24 hours' LIMIT 1",
            (JOB_NAME, f"%{dedupe_key}%"),
        )
        if cur.fetchone():
            return

    sample = "\n".join(f"- {ref}: {reason}" for ref, _filled, reason in ambiguous[:10])
    more = f"\n...and {len(ambiguous) - 10} more" if len(ambiguous) > 10 else ""
    message = (
        f"\u26a0\ufe0f *route_resolver*: {len(ambiguous)} Bridge row(s) approved for final delivery "
        f"but the final destination can't be determined\n"
        f"{sample}{more}\n"
        f"Exactly one of TAN/NAM/SA/MOZ arrival dates must be filled (or the row must have a "
        f"confirmed country-sheet copy when removal is enabled).\n"
        f"`{dedupe_key}`"
    )
    delivered, delivery_error = send_telegram(message)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_telegram_alert_log "
            "(severity, job_name, chat_id, message, delivered, delivery_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("warning", JOB_NAME, os.getenv("ROUTE_RESOLVER_LOG_ID") or os.getenv("RISK_LOG_ID") or "",
             message, delivered, delivery_error),
        )
    conn.commit()


def process_bridge_country_leg_row(conn, ss_client, main_sheet_id: int, row, columns_by_id: dict,
                                     stats: dict, route_value: str | None = None,
                                     ambiguous: list | None = None,
                                     unresolved_rows: set | None = None) -> bool:
    """
    Bridge's country-sheet treatment, rebuilt 2026-09-23 (per Jay:
    "convert it to all routes"). Always returns True -- caller must NOT
    also run normal resolve_target_sheet_id() for Bridge rows anymore;
    there's nothing left in BRIDGE's invoicing folder for the old
    ROUTE-matching algorithm to match against (the 12 old consolidated
    sheets were retired the same day this shipped).

    Four copy-stage countries (DRC, ZAM, ZIM, BOTS): flipping that
    country's own REGION: Approval column to Approved copies the row to
    that country's sheet (row stays on the main sheet) -- same
    dedup-via-log mechanism as the original 2026-09-16 build, now with
    ZIM added as a fourth leg.

    Final destination (TAN, NAM, SA, MOZ): gated by the single
    "Siphemandla Hleza delivery approval" column. Which of the 4 a row
    moves to is decided by checking which country's own REGION: ARRIVAL
    DATE is filled in (per Jay: "if sa has a date and other 3 are blank
    it moves to sa"). Fail-safe: zero or more than one filled is treated
    as ambiguous -- the row is skipped and flagged for review rather
    than guessed, same "never guess" philosophy as
    resolve_target_sheet_id() elsewhere in this module.

    Adhoc Tagging stays exactly as it was (per Jay: "leave the adhoc
    sheet and route") -- TAGGING ONLY on any of Bridge's 9 region
    SERVICES REQUIRED columns still overrides the date-based
    final-destination check and sends the row to BRIDGE_ADHOC_TAGGING
    instead, gated by the same final approval column.
    """
    cells_by_col = {cell.column_id: cell for cell in row.cells}
    name_to_id = {title: col_id for col_id, title in columns_by_id.items()}

    for country, (approval_col_name, target_sheet_id) in BRIDGE_COUNTRY_APPROVAL_COLUMNS.items():
        approval_col_id = name_to_id.get(approval_col_name)
        if approval_col_id is None:
            continue  # column not found -- e.g. not yet created on this sheet
        cell = cells_by_col.get(approval_col_id)
        value = (cell.display_value or cell.value) if cell else None
        if not value or str(value).strip().casefold() != "approved":
            continue
        if _bridge_country_already_copied(conn, main_sheet_id, row.id, country):
            continue
        try:
            target_row_id = copy_row(ss_client, main_sheet_id, row.id, target_sheet_id)
        except CopierError as exc:
            try:
                _upsert_bridge_country_copy_log(conn, main_sheet_id, row.id, country, target_sheet_id, None, "error", str(exc))
            except Exception as log_exc:
                conn.rollback()
                logger.error("Bridge: copy to %s failed for row %s AND the error log write failed: %s", country, row.id, log_exc)
            logger.warning("Bridge: copy to %s failed for row %s: %s", country, row.id, exc)
            continue
        # The copy happened. If recording it fails (e.g. a CHECK constraint
        # on bridge_country_copy_log.country), the row would be re-copied
        # every run -- so shout loudly with everything needed to reconcile.
        try:
            _upsert_bridge_country_copy_log(conn, main_sheet_id, row.id, country, target_sheet_id, target_row_id, "copied")
        except Exception as log_exc:
            conn.rollback()
            logger.critical("Bridge: row %s WAS copied to %s sheet %s (new row %s) but logging it failed: %s "
                            "-- it WILL be re-copied next run until this is fixed",
                            row.id, country, target_sheet_id, target_row_id, log_exc)
            continue
        stats["bridge_country_copies"] = stats.get("bridge_country_copies", 0) + 1
        logger.info("Bridge: copied row %s to %s sheet %s", row.id, country, target_sheet_id)

    delivery_col_id = name_to_id.get("Siphemandla Hleza delivery approval")
    if delivery_col_id is None:
        return True
    delivery_cell = cells_by_col.get(delivery_col_id)
    delivery_value = (delivery_cell.display_value or delivery_cell.value) if delivery_cell else None
    if not delivery_value or str(delivery_value).strip().casefold() != "approved":
        # No longer approved (e.g. flipped to Declined) -- stop re-checking
        # a row that was previously tracked as unresolved.
        if unresolved_rows and row.id in unresolved_rows:
            clear_unresolved(conn, main_sheet_id, row.id)
        return True

    if _bridge_adhoc_tagging_triggered(cells_by_col, columns_by_id):
        if _bridge_move_checked(conn, ss_client, main_sheet_id, row, columns_by_id,
                                BRIDGE_ADHOC_TAGGING, route_value):
            stats["rows_moved"] = stats.get("rows_moved", 0) + 1
            clear_unresolved(conn, main_sheet_id, row.id)
            logger.info("Bridge: final delivery approved, row %s moved to ADHOC TAGGING (override)", row.id)
        return True

    ref_col_id = name_to_id.get("INTERNAL REF:")
    ref_cell = cells_by_col.get(ref_col_id) if ref_col_id else None
    internal_ref = str((ref_cell.display_value or ref_cell.value) if ref_cell else row.id)

    filled_countries = []
    for country, (date_col_name, target_sheet_id) in BRIDGE_FINAL_DESTINATION_DATE_COLUMNS.items():
        date_col_id = name_to_id.get(date_col_name)
        if date_col_id is None:
            continue
        cell = cells_by_col.get(date_col_id)
        value = (cell.display_value or cell.value) if cell else None
        if value and str(value).strip():
            filled_countries.append(country)

    if len(filled_countries) == 0 and BRIDGE_ZERO_DATE_ACTION == "remove":
        final_activity = _bridge_final_region_activity(cells_by_col, columns_by_id)
        if final_activity:
            reason = ("no final arrival date, but final-region data present "
                      f"({', '.join(final_activity[:3])}) -- may still be heading to final approval")
        elif stats.get("bridge_removed_after_copy", 0) >= BRIDGE_MAX_REMOVALS_PER_RUN:
            reason = "removal cap reached this run -- will retry next run"
        else:
            confirmed = _bridge_confirmed_copies(conn, ss_client, main_sheet_id, row.id)
            used_archived = False
            if not confirmed and BRIDGE_ACCEPT_ARCHIVED_COPIES:
                confirmed = _bridge_archived_copies(conn, ss_client, main_sheet_id, row.id)
                used_archived = bool(confirmed)
            if confirmed:
                if _bridge_remove_row_after_copy(conn, ss_client, main_sheet_id, row, columns_by_id,
                                                 confirmed, route_value):
                    stats["bridge_removed_after_copy"] = stats.get("bridge_removed_after_copy", 0) + 1
                    clear_unresolved(conn, main_sheet_id, row.id)
                    if used_archived:
                        logger.info("Bridge: row %s removed on ARCHIVED-COPY rule (logged copy 404, backup verified)", row.id)
                return True
            reason = "no final date and no confirmed or archived country-sheet copy"
    elif len(filled_countries) == 0:
        reason = "no TAN/NAM/SA/MOZ arrival date filled"
    else:
        reason = f"more than one final arrival date filled ({', '.join(filled_countries)})"

    if len(filled_countries) != 1:
        stats["bridge_final_destination_ambiguous"] = stats.get("bridge_final_destination_ambiguous", 0) + 1
        logger.warning("Bridge: row %s (%s) has final delivery approved but %s -- left in place, flagged",
                       row.id, internal_ref, reason)
        # Track it as unresolved so it is re-checked every run and clears
        # itself once the data (or this rule) is fixed -- without this a
        # row is only re-evaluated if someone edits it.
        upsert_unresolved(conn, main_sheet_id, row.id, "bridge", route_value or "", reason)
        if ambiguous is not None:
            ambiguous.append((internal_ref, filled_countries, reason))
        return True

    destination = filled_countries[0]
    _, target_sheet_id = BRIDGE_FINAL_DESTINATION_DATE_COLUMNS[destination]
    if _bridge_move_checked(conn, ss_client, main_sheet_id, row, columns_by_id, target_sheet_id, route_value):
        stats["rows_moved"] = stats.get("rows_moved", 0) + 1
        clear_unresolved(conn, main_sheet_id, row.id)
        logger.info("Bridge: final delivery approved, row %s moved to %s (%s)", row.id, destination, target_sheet_id)
    return True
