"""
route_resolver.py — approval_router, Part 1: Main sheet -> Invoicing

Watches each client's main tracking sheet for rows whose ROUTE column is set
and whose "<Manager Name> approval" column reads "Approved", then hands them
to mover.py's move_row() to actually move them into the right invoicing sheet.

This module owns ONLY the "which row, which target, is it approved" decision.
It does not touch Smartsheet's row data directly (mover.py does the backup +
move + verify) and it has no concept of Part 2 (Invoicing -> Archive), which
is a structurally different problem (checkbox trigger, cell-limit rotation)
and lives in its own module once built.

Matching algorithm (confirmed live against all 12 clients, 2026-09-13 — see
route-resolver-logic.md for the full audit):
    1. Split the ROUTE value on its first "-" to get a candidate client prefix.
    2. Match that prefix case-insensitively against the INVOICING workspace's
       folder names (not the main-tracking side — they often differ, e.g.
       "Bridge" vs "Steinweg Bridge" on the main-tracking side, but the
       invoicing folder really is "BRIDGE").
    3. Within that folder, match the ENTIRE raw ROUTE value (not just the
       suffix), case-insensitively and whitespace-trimmed, against real sheet
       names. This is deliberately not a "parse the suffix" approach — real
       ROUTE data has typos/spacing/casing inconsistencies that make suffix
       parsing fragile; matching the whole string against real sheet names
       sidesteps that.
    4. RELOAD is structurally different (see RELOAD_LOCAL_ROUTE_MAP below).
    5. One case that WAS a many-to-one exception is no longer one: Bridge's
       "Sicomine Mine-DBN" and "Sicomine Mine-DAR" ROUTE values used to both
       point at one combined sheet. Jay asked (2026-09-13) for these split
       into two separate sheets instead — done, so this now resolves via
       the normal algorithm with no override needed. KNOWN_ROUTE_OVERRIDES
       below is kept as a real (currently empty) mechanism in case a
       similar merge turns up for another client later.
    6. No match at step 2 or 3 -> skip the row and log it for review. Never
       guess/fuzzy-match past this point.
    7. GSM and Gold Vale (2026-09-16) are now fully diverted BEFORE this
       algorithm ever runs — see GSM_GOLDVALE_CONFIG below. Their old
       per-route invoicing sheets (GSM-DRC-DBN, GOLDVALE-COMMUS-DBN, etc.)
       were deleted the same day their new country sheets were created, so
       this algorithm has nothing left to match against for them. Bridge
       (2026-09-23) joined this same "fully diverted before ROUTE matching"
       group — see _process_bridge_country_leg_row below.

The plain "Glencore" folder (main-tracking id 5600395335624580) is a
genuinely different, actively-used operation with its own sheet structure
(no single main-sheet + ROUTE + approval pattern) — confirmed with Jay
2026-09-13 that it's out of scope. It's simply not in MAIN_SHEETS below.
"""

import os
import re
import time
import logging
from typing import Any

from dotenv import load_dotenv
import requests
import smartsheet
import psycopg2.extras

load_dotenv()

from jobs.approval_router.backup import get_connection, row_to_dict
from jobs.approval_router.mover import get_smartsheet_client, move_row, MoverError, get_move_log, SHEET_FULL_ERROR_CODE
from jobs.approval_router.copier import copy_row, CopierError

logger = logging.getLogger("approval_router.route_resolver")

INVOICING_WORKSPACE_ID = 1702905712535428

# client_slug -> (main tracking sheet_id, invoicing folder_id)
# Confirmed live 2026-09-13 against the real Smartsheet structure — see
# route-resolver-logic.md for the full audit these came from.
MAIN_SHEETS: dict[str, dict[str, int]] = {
    "alistair":               {"main_sheet_id": 8253984251793284, "invoicing_folder_id": 5153479745398660},
    "reload":                 {"main_sheet_id": 1307544392781700, "invoicing_folder_id": 2409098722469764},
    "glencore_international": {"main_sheet_id": 3849491978342276, "invoicing_folder_id": 7194173326550916},
    "fh_bertling":            {"main_sheet_id": 636041135345540,  "invoicing_folder_id": 2127623745759108},
    "bridge":                 {"main_sheet_id": 4700548271918980, "invoicing_folder_id": 1397548024915844},
    "goldvale":               {"main_sheet_id": 3232243102207876, "invoicing_folder_id": 4493772768733060},
    "gsm":                    {"main_sheet_id": 5438660737453956, "invoicing_folder_id": 5399770350020484},
    "ixm":                    {"main_sheet_id": 6551095343009668, "invoicing_folder_id": 5012742257043332},
    "mittal":                 {"main_sheet_id": 4646970969771908, "invoicing_folder_id": 8812654442637188},
    "sls_africa":             {"main_sheet_id": 3849847846162308, "invoicing_folder_id": 6807145233573764},
    "sls_trading":            {"main_sheet_id": 4242048029773700, "invoicing_folder_id": 6701592117307268},
    "zalawi":                 {"main_sheet_id": 3698247053823876, "invoicing_folder_id": 6490485884774276},
}

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
KNOWN_ROUTE_OVERRIDES: dict[str, int] = {}
# Bridge no longer contributes any entries here (2026-09-23 rebuild) --
# every Bridge row is diverted to _process_bridge_country_leg_row()
# before resolve_target_sheet_id() is ever called, same as GSM/Gold Vale.
# See BRIDGE_COUNTRY_APPROVAL_COLUMNS / BRIDGE_FINAL_DESTINATION_DATE_COLUMNS
# below. Glencore International and SLS Africa's overrides (further down)
# still use this same dict.

GLENCORE_FIMPIMPA = 7949082489474948
GLENCORE_KANSANSHI = 6824445303017348
GLENCORE_KCM = 628035855536004
GLENCORE_MIMBULA = 7277469863464836
GLENCORE_MOPANI = 7105748481036164
GLENCORE_RGT = 8964850844913540
GLENCORE_ZANRONG = 4572997676650372
GLENCORE_ZCCZ = 5130982647877508
GLENCORE_CCM = 4440228627697540

# Glencore International consolidation, 2026-09-16 (per Jay): the 17
# per-destination route sheets are grouped down to 8 sheets by mine/
# loading-point prefix (the ROUTE picklist itself is untouched -- staff
# still pick the same 17 granular values, they just now converge on
# fewer physical sheets). Audited live: only Fimpimpa-DAR had real data
# (2 rows, recovered from Deleted Items and moved onto the new FIMPIMPA
# sheet after an accidental early deletion of the old sheet, 2026-09-16).
# Note: FIMPIMPA, RGT, and ZCCZ each only ever had one ROUTE value to
# begin with, so those three overrides are effectively a rename (dropping
# the destination suffix) rather than a true many-to-one merge -- kept in
# this same mechanism for consistency, per Jay's "group by first suffix,
# extra" instruction covering all of them uniformly.
# 2026-09-29 (per Jay): two more routes added -- "Mopani-Harare" joins the
# existing MOPANI group, and "CCM-Beira" starts a 9th group (CCM) with its
# own new sheet, GLENCORE INTERNATIONAL-CCM (GLENCORE_CCM above).
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_FIMPIMPA for v in (
        "Glencore international-Fimpimpa-DAR",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_KANSANSHI for v in (
        "Glencore international-Kansanshi-Beira", "Glencore international-Kansanshi-GRB",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_KCM for v in (
        "Glencore international-KCM-DBN", "Glencore international-KCM-GRB",
        "Glencore international-KCM-JHB", "Glencore international-KCM-POLYTRA KIWE",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_MIMBULA for v in (
        "Glencore international-Mimbula-Beira", "Glencore international-Mimbula-WVB",
        "Glencore international-Mimbula-DAR",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_MOPANI for v in (
        "Glencore international-Mopani-GRB", "Glencore international-Mopani-DBN",
        "Glencore international-Mopani-JHB", "Glencore international-Mopani-Harare",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_RGT for v in (
        "Glencore international-RGT-WVB",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_ZANRONG for v in (
        "Glencore international-Zanrong-DAR", "Glencore international-Zanrong-WVB",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_ZCCZ for v in (
        "Glencore international-ZCCZ-DAR",
    )
})
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_CCM for v in (
        "Glencore international-CCM-Beira",
    )
})

SLS_AFRICA_NDOLA_PORT = 4990485677690756

# SLS Africa NDOLA consolidation, 2026-09-16 (per Jay): NDOLA-DAR and
# NDOLA-GRB both now converge onto one NDOLA-PORT sheet. Both old sheets
# confirmed empty before deletion -- no data migration needed.
# NDOLA-NAKONDE removed entirely (not replaced): no override needed since
# it's no longer a selectable ROUTE option once Jay finishes removing it
# from the picklist (the sheet-deletion half of this cleanup ran live;
# the ROUTE-option removal is still pending on his end).
KNOWN_ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): SLS_AFRICA_NDOLA_PORT for v in (
        "SLS Africa-NDOLA-DAR", "SLS Africa-NDOLA-GRB",
    )
})

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
# of 2 -- see _process_bridge_country_leg_row() below.
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
# _process_bridge_country_leg_row below).
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


def _alert_bridge_final_destination_ambiguous(conn, row_id: int, filled_countries: list):
    """
    Alerts once per row per 24h if Bridge's final delivery approval fires
    but the TAN/NAM/SA/MOZ arrival-date check can't determine a single
    destination (zero or more than one filled) -- same de-dupe
    philosophy as _alert_unresolvable_route() below: never re-alert every
    10-minute cron tick for the same unresolved row.
    """
    dedupe_key = f"bridge-final-dest-ambiguous|{row_id}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM automation_telegram_alert_log "
            "WHERE job_name = %s AND message LIKE %s AND sent_at > now() - interval '24 hours' LIMIT 1",
            (JOB_NAME, f"%{dedupe_key}%"),
        )
        if cur.fetchone():
            return

    message = (
        f"\u26a0\ufe0f *route_resolver*: Bridge final delivery approved but final "
        f"destination is ambiguous on row `{row_id}`\n"
        f"TAN/NAM/SA/MOZ arrival dates filled: {filled_countries or 'none'}\n"
        f"Exactly one must be filled to resolve a destination.\n"
        f"`{dedupe_key}`"
    )
    delivered, delivery_error = _send_telegram(message)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_telegram_alert_log "
            "(severity, job_name, chat_id, message, delivered, delivery_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("warning", JOB_NAME, os.getenv("ROUTE_RESOLVER_LOG_ID") or os.getenv("RISK_LOG_ID") or "",
             message, delivered, delivery_error),
        )
    conn.commit()


def _process_bridge_country_leg_row(conn, ss_client, main_sheet_id: int, row, columns_by_id: dict,
                                     stats: dict) -> bool:
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
            _upsert_bridge_country_copy_log(conn, main_sheet_id, row.id, country, target_sheet_id, target_row_id, "copied")
            stats["bridge_country_copies"] = stats.get("bridge_country_copies", 0) + 1
            logger.info("Bridge: copied row %s to %s sheet %s", row.id, country, target_sheet_id)
        except CopierError as exc:
            _upsert_bridge_country_copy_log(conn, main_sheet_id, row.id, country, target_sheet_id, None, "error", str(exc))
            logger.warning("Bridge: copy to %s failed for row %s: %s", country, row.id, exc)

    delivery_col_id = name_to_id.get("Siphemandla Hleza delivery approval")
    if delivery_col_id is None:
        return True
    delivery_cell = cells_by_col.get(delivery_col_id)
    delivery_value = (delivery_cell.display_value or delivery_cell.value) if delivery_cell else None
    if not delivery_value or str(delivery_value).strip().casefold() != "approved":
        return True

    if _bridge_adhoc_tagging_triggered(cells_by_col, columns_by_id):
        move_row(
            conn, ss_client,
            source_sheet_id=main_sheet_id,
            source_row=row,
            columns_by_id=columns_by_id,
            target_sheet_id=BRIDGE_ADHOC_TAGGING,
            client_slug="bridge",
        )
        stats["rows_moved"] = stats.get("rows_moved", 0) + 1
        logger.info("Bridge: final delivery approved, row %s moved to ADHOC TAGGING (override)", row.id)
        return True

    filled_countries = []
    for country, (date_col_name, target_sheet_id) in BRIDGE_FINAL_DESTINATION_DATE_COLUMNS.items():
        date_col_id = name_to_id.get(date_col_name)
        if date_col_id is None:
            continue
        cell = cells_by_col.get(date_col_id)
        value = (cell.display_value or cell.value) if cell else None
        if value and str(value).strip():
            filled_countries.append(country)

    if len(filled_countries) != 1:
        stats["bridge_final_destination_ambiguous"] = stats.get("bridge_final_destination_ambiguous", 0) + 1
        logger.warning(
            "Bridge: row %s has final delivery approved but %s of TAN/NAM/SA/MOZ arrival dates filled (%s) -- skipping, flagged for review",
            row.id, len(filled_countries), filled_countries,
        )
        _alert_bridge_final_destination_ambiguous(conn, row.id, filled_countries)
        return True

    destination = filled_countries[0]
    _, target_sheet_id = BRIDGE_FINAL_DESTINATION_DATE_COLUMNS[destination]
    move_row(
        conn, ss_client,
        source_sheet_id=main_sheet_id,
        source_row=row,
        columns_by_id=columns_by_id,
        target_sheet_id=target_sheet_id,
        client_slug="bridge",
    )
    stats["rows_moved"] = stats.get("rows_moved", 0) + 1
    logger.info("Bridge: final delivery approved, row %s moved to %s (%s)", row.id, destination, target_sheet_id)
    return True


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


def _process_gsm_goldvale_country_leg_row(conn, ss_client, client_slug: str, main_sheet_id: int, row,
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


# RELOAD's special case: when ROUTE == "Reload-DRC-LOCAL", the LOCAL ROUTE
# column (not ROUTE) determines the real target — one of 4 sheets. Confirmed
# live against the real LOCAL ROUTE picklist 2026-09-13 (29 options, matches
# Jay's original table exactly). Keyed by normalized LOCAL ROUTE value.
RELOAD_TRIGGER_ROUTE = "reload-drc-local"

RELOAD_KOLWEZI = 907292680605572
RELOAD_LIKASI = 3831881941340036
RELOAD_FUNGURUME = 4887378744266628
# Sheet renamed "Kasumbalesa" -> "Lubumbashi" by Jay, 2026-09-16 (manual
# Smartsheet UI change -- no API for renames). Same sheet_id, routing is
# ID-based so this needed no other code change. The "KAS" suffix in the
# LOCAL ROUTE picklist values below is unrelated -- that's the
# Kasumbalesa border-crossing abbreviation on the main-tracking side,
# confirmed staying as-is.
RELOAD_LUBUMBASHI = 869874388651908

RELOAD_LOCAL_ROUTE_MAP: dict[str, int] = {
    normalized: RELOAD_KOLWEZI for normalized in (
        "reload-deziwa - kas", "reload-metalkol - kas", "reload-comilu - kas",
        "reload-hmc - kas", "reload-tcc - kas", "reload-brother - kas",
        "reload-kamoa - kas", "reload-kamoa - sak", "reload-lcs - kas",
        "reload-mmt - kas", "reload-zfm - kas", "reload-mkm - kas",
        "reload-kas - kamoa", "reload-kms - kas", "reload-kcc - kas",
    )
}
RELOAD_LOCAL_ROUTE_MAP.update({
    normalized: RELOAD_LIKASI for normalized in (
        "reload-ruba - mokambo", "reload-ruba mine - kas", "reload-smco - kas",
        "reload-kambove - kas", "reload-kpm - kas",
    )
})
RELOAD_LOCAL_ROUTE_MAP.update({
    normalized: RELOAD_FUNGURUME for normalized in (
        "reload-tfm - mokambo", "reload-tfm - kas", "reload-tfm - sak",
        "reload-lamikal - kas", "reload-lamikal - sak", "reload-kfm - mokambo",
        "reload-kisenda - kas",
    )
})
RELOAD_LOCAL_ROUTE_MAP.update({
    normalized: RELOAD_LUBUMBASHI for normalized in (
        "reload-sem - kas", "reload-kicc - kas",
    )
})

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


def _norm(value: str) -> str:
    """Casefold + whitespace-collapse for tolerant matching — handles the
    casing and stray-space inconsistencies confirmed live across real ROUTE
    data (trailing spaces, "Bridge-CCS-serenje" vs "BRIDGE-CCS-SERENJE ",
    etc.)."""
    return " ".join(value.split()).casefold()


# Matches "<WORD> REGION: Approval" (case-insensitive, e.g. "DRC REGION:
# Approval", "ZAM REGION: Approval", "BOTS REGION: Approval") -- the
# per-leg approval columns now present on Bridge, GSM and Gold Vale's main
# sheets. Excluded from _find_approval_column() below; see that function's
# docstring for why.
_REGION_APPROVAL_TITLE_RE = re.compile(r"^[a-z]+ region:\s*approval$")


def _find_approval_column(columns_by_id: dict[int, str]) -> int | None:
    """Finds the "<Manager Name> approval" column by suffix match — the
    manager's name varies per client/sheet, only the " approval" suffix is
    stable. Confirmed with Jay 2026-09-13: the separate "Load approval
    status" column must NOT be used, even though it looks similar.

    FIXED 2026-09-16: also excludes the per-region "<COUNTRY> REGION:
    Approval" columns now on Bridge, GSM and Gold Vale's main sheets.
    Without this exclusion, whichever region-approval column happens to
    sit at the lowest column index gets returned as THIS row's overall
    gating approval column instead of the real final/manager approval
    column. Confirmed this was silently wrong for Bridge specifically:
    DRC REGION: Approval sits at column index 31 there, well before
    Siphemandla Hleza delivery approval at index 116 -- so before this
    fix, process_sheet()'s generic approval gate was checking DRC REGION:
    Approval for every Bridge row, not the real final-delivery column.
    GSM and Gold Vale happened to be unaffected by this specific symptom
    only because their 3 new approval columns were appended at the END of
    each sheet (after the pre-existing final approval column) -- but that
    was luck of column order, not something to rely on, hence this fix
    plus moving the per-client multi-leg/country-leg check to run BEFORE
    this generic gate in process_sheet() (see that function). Bridge is
    fully diverted before this function is even reached as of 2026-09-23,
    but the exclusion regex stays -- GSM/Gold Vale still rely on it."""
    for col_id, title in columns_by_id.items():
        normalized = title.strip().casefold()
        if normalized == "load approval status":
            continue
        if _REGION_APPROVAL_TITLE_RE.match(normalized):
            continue
        if normalized.endswith("approval"):
            return col_id
    return None


def _find_column_id(columns_by_id: dict[int, str], title: str) -> int | None:
    target = title.strip().casefold()
    for col_id, col_title in columns_by_id.items():
        if col_title.strip().casefold() == target:
            return col_id
    return None


class _FolderSheetCache:
    """Caches folder_id -> {normalized_sheet_name: sheet_id} for the
    lifetime of one run, so each client folder's sheet list is only fetched
    from Smartsheet once even though many rows may resolve against it."""

    def __init__(self, ss_client):
        self._ss_client = ss_client
        self._cache: dict[int, dict[str, int]] = {}

    def sheets_in_folder(self, folder_id: int) -> dict[str, int]:
        if folder_id not in self._cache:
            folder = self._ss_client.Folders.get_folder(folder_id)
            self._cache[folder_id] = {
                _norm(sheet.name): sheet.id for sheet in (folder.sheets or [])
            }
        return self._cache[folder_id]


def resolve_target_sheet_id(
    route_value: str,
    local_route_value: str | None,
    folder_cache: "_FolderSheetCache",
    commodity_value: str | None = None,
) -> tuple[int | None, str]:
    """
    Resolves a ROUTE value (plus, for RELOAD, the LOCAL ROUTE value; for
    IXM, the COMMODITY value) to a target invoicing sheet_id.

    Returns (sheet_id_or_None, reason) — reason is a short human-readable
    string explaining the outcome, useful for logging when sheet_id is None.
    """
    normalized_route = _norm(route_value)

    if normalized_route in KNOWN_ROUTE_OVERRIDES:
        return KNOWN_ROUTE_OVERRIDES[normalized_route], "known override"

    if normalized_route == RELOAD_TRIGGER_ROUTE:
        if not local_route_value or not local_route_value.strip():
            return None, "RELOAD-DRC-LOCAL but LOCAL ROUTE column is blank"
        normalized_local = _norm(local_route_value)
        sheet_id = RELOAD_LOCAL_ROUTE_MAP.get(normalized_local)
        if sheet_id is None:
            return None, f"RELOAD LOCAL ROUTE value not in known map: {local_route_value!r}"
        return sheet_id, "RELOAD local route map"

    if normalized_route == IXM_TRIGGER_ROUTE:
        if not commodity_value or not commodity_value.strip():
            return None, "IXM-Lonshi-Dar but COMMODITY column is blank"
        normalized_commodity = _norm(commodity_value)
        sheet_id = IXM_COMMODITY_MAP.get(normalized_commodity)
        if sheet_id is None:
            return None, f"IXM COMMODITY value not in known map: {commodity_value!r}"
        return sheet_id, "IXM commodity map"

    prefix = route_value.split("-", 1)[0].strip()
    if not prefix:
        return None, "ROUTE value has no client prefix (blank before first '-')"

    # Resolve prefix -> invoicing folder_id by comparing against each known
    # client's slug (with underscores as spaces). Every MAIN_SHEETS entry's
    # invoicing folder name matches its slug once cased/spaced the same way
    # — confirmed live for all 12 clients, including Mittal (colloquial
    # slug "mittal" already equals its real ROUTE prefix "Mittal", so no
    # extra special-casing is needed here beyond this normalization).
    normalized_prefix = _norm(prefix)
    matched_folder_id = None
    for slug, config in MAIN_SHEETS.items():
        if normalized_prefix == slug.replace("_", " "):
            matched_folder_id = config["invoicing_folder_id"]
            break

    if matched_folder_id is None:
        return None, f"no known client folder matches ROUTE prefix {prefix!r}"

    sheets_in_folder = folder_cache.sheets_in_folder(matched_folder_id)
    sheet_id = sheets_in_folder.get(normalized_route)
    if sheet_id is None:
        return None, f"no sheet in folder {matched_folder_id} matches ROUTE value {route_value!r}"
    return sheet_id, "matched folder + sheet name"


def _send_telegram(message: str) -> tuple[bool, str | None]:
    """
    Raw send, same shape as every other job in this repo (Sync_ADARS.py,
    Sync_WorldRisk.py, etc.) — LOG_BOT_TOKEN + a chat id. Returns
    (delivered, delivery_error) rather than raising, so a Telegram outage
    never breaks the actual routing run — it's logged into
    automation_telegram_alert_log either way.

    Chat id: falls back to RISK_LOG_ID (already used by WorldRisk/
    MasterRotation for operational alerts) if a dedicated
    ROUTE_RESOLVER_LOG_ID isn't set — Jay can add that env var later for a
    separate channel without any code change.
    """
    token = os.getenv("LOG_BOT_TOKEN")
    chat_id = os.getenv("ROUTE_RESOLVER_LOG_ID") or os.getenv("RISK_LOG_ID")
    if not token or not chat_id:
        return False, "LOG_BOT_TOKEN or chat_id env var missing"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        if response.status_code == 200:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except Exception as exc:
        return False, str(exc)


def _alert_unresolvable_route(conn, client_slug: str, route_value: str,
                               local_route_value: str | None, reason: str):
    """
    Alerts once per distinct (client, route, local_route) combination per
    24h — NOT on every cron tick. Without this de-dupe, a single unresolved
    route (e.g. a new picklist option added without its matching invoicing
    sheet) would re-alert every 10 minutes until someone fixes it, which
    trains people to ignore the channel. Every attempt — sent or
    de-duplicated-away — still gets written to automation_telegram_alert_log
    so there's a real audit trail, unlike the legacy scripts' fire-and-
    forget sends.
    """
    dedupe_key = f"{client_slug}|{route_value}|{local_route_value or ''}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM automation_telegram_alert_log "
            "WHERE job_name = %s AND message LIKE %s AND sent_at > now() - interval '24 hours' LIMIT 1",
            (JOB_NAME, f"%{dedupe_key}%"),
        )
        if cur.fetchone():
            return  # already alerted about this exact route recently

    message = (
        f"⚠️ *route_resolver*: unresolvable ROUTE on *{client_slug}*\n"
        f"ROUTE: `{route_value}`\n"
        + (f"LOCAL ROUTE: `{local_route_value}`\n" if local_route_value else "")
        + f"Reason: {reason}\n"
        f"Likely fix: create the matching invoicing sheet, same as the recent Bridge orphans.\n"
        f"`{dedupe_key}`"
    )
    delivered, delivery_error = _send_telegram(message)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_telegram_alert_log "
            "(severity, job_name, chat_id, message, delivered, delivery_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("warning", JOB_NAME, os.getenv("ROUTE_RESOLVER_LOG_ID") or os.getenv("RISK_LOG_ID") or "",
             message, delivered, delivery_error),
        )
    conn.commit()


def _get_unresolved_rows(conn, main_sheet_id: int) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_row_id FROM approval_router_unresolved_route_log WHERE source_sheet_id = %s",
            (main_sheet_id,),
        )
        return {r[0] for r in cur.fetchall()}


def _upsert_unresolved(conn, main_sheet_id: int, source_row_id: int, client_slug: str,
                        route_value: str, reason: str):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO approval_router_unresolved_route_log "
            "(source_sheet_id, source_row_id, client_slug, route_value, reason, last_checked_at) "
            "VALUES (%s, %s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (source_sheet_id, source_row_id) DO UPDATE SET "
            "route_value = EXCLUDED.route_value, reason = EXCLUDED.reason, "
            "last_checked_at = EXCLUDED.last_checked_at",
            (main_sheet_id, source_row_id, client_slug, route_value, reason),
        )
    conn.commit()


def _clear_unresolved(conn, main_sheet_id: int, source_row_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM approval_router_unresolved_route_log WHERE source_sheet_id = %s AND source_row_id = %s",
            (main_sheet_id, source_row_id),
        )
    conn.commit()


def _get_sheet_delta_state(conn, sheet_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_version, last_checkpoint_at FROM approval_router_sync_sheet_state WHERE sheet_id = %s",
            (sheet_id,),
        )
        return cur.fetchone()


def _update_sheet_delta_state(conn, sheet_id: int, client_slug: str, sheet_name: str, version: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO approval_router_sync_sheet_state "
            "(sheet_id, client_slug, sheet_name, last_version, last_checkpoint_at, updated_at) "
            "VALUES (%s, %s, %s, %s, clock_timestamp(), clock_timestamp()) "
            "ON CONFLICT (sheet_id) DO UPDATE SET client_slug = EXCLUDED.client_slug, "
            "sheet_name = EXCLUDED.sheet_name, last_version = EXCLUDED.last_version, "
            "last_checkpoint_at = EXCLUDED.last_checkpoint_at, updated_at = EXCLUDED.updated_at",
            (sheet_id, client_slug, sheet_name, version),
        )
    conn.commit()


def process_sheet(conn, ss_client, folder_cache: "_FolderSheetCache", client_slug: str, main_sheet_id: int,
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
    whichever REGION: Approval column _find_approval_column() happens to
    pick, silently skipping rows that still need earlier-leg processing
    before that column is ever set. See _find_approval_column()'s
    docstring for the related fix this pairs with.
    """
    stats = {"rows_checked": 0, "rows_moved": 0, "rows_skipped_no_route": 0,
              "rows_skipped_not_approved": 0, "rows_failed_resolve": 0, "skipped_known_bad_target": 0}

    unresolved_rows = _get_unresolved_rows(conn, main_sheet_id)
    current_version = ss_client.Sheets.get_sheet_version(main_sheet_id).version
    state = _get_sheet_delta_state(conn, main_sheet_id)
    last_version, last_checkpoint_at = state if state else (None, None)

    if last_version is not None and current_version == last_version and not unresolved_rows:
        logger.info("%s: unchanged (version %s), no pending unresolved rows — skipping", client_slug, current_version)
        return stats

    sheet = ss_client.Sheets.get_sheet(main_sheet_id)
    columns_by_id = {c.id: c.title for c in sheet.columns}

    route_col_id = _find_column_id(columns_by_id, "ROUTE") or _find_column_id(columns_by_id, "Routes")
    local_route_col_id = _find_column_id(columns_by_id, "LOCAL ROUTE")
    commodity_col_id = _find_column_id(columns_by_id, "COMMODITY")
    approval_col_id = _find_approval_column(columns_by_id)

    if route_col_id is None:
        logger.error("No ROUTE/Routes column found on %s main sheet (sheet_id=%s) — skipping", client_slug, main_sheet_id)
        return stats
    if approval_col_id is None:
        logger.error("No approval column found on %s main sheet (sheet_id=%s) — skipping", client_slug, main_sheet_id)
        return stats

    for row in sheet.rows:
        row_changed = last_checkpoint_at is None or (row.modified_at and row.modified_at > last_checkpoint_at)
        if not row_changed and row.id not in unresolved_rows:
            continue

        stats["rows_checked"] += 1
        cells_by_col = {cell.column_id: cell for cell in row.cells}

        route_cell = cells_by_col.get(route_col_id)
        route_value = (route_cell.display_value or route_cell.value) if route_cell else None
        if not route_value or not str(route_value).strip():
            stats["rows_skipped_no_route"] += 1
            continue
        route_value = str(route_value)

        # Bridge country-leg and GSM/Gold Vale country-leg handling both
        # run BEFORE the generic approval gate -- see this function's
        # docstring and _find_approval_column()'s docstring for why.
        if client_slug == "bridge" and _process_bridge_country_leg_row(
            conn, ss_client, main_sheet_id, row, columns_by_id, stats
        ):
            continue
        if _process_gsm_goldvale_country_leg_row(
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
            _alert_unresolvable_route(conn, client_slug, route_value, local_route_value, reason)
            _upsert_unresolved(conn, main_sheet_id, row.id, client_slug, route_value, reason)
            continue

        _clear_unresolved(conn, main_sheet_id, row.id)

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

    _update_sheet_delta_state(conn, main_sheet_id, client_slug, sheet.name, current_version)
    return stats


LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/route_resolver.lock"
JOB_NAME = "route_resolver_part1"


def _start_job_run(conn, job_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_job_run_log (job_name, status, started_at) "
            "VALUES (%s, 'running', clock_timestamp()) RETURNING id",
            (job_name,),
        )
        job_run_id = cur.fetchone()[0]
    conn.commit()
    return job_run_id


def _finish_job_run(conn, job_run_id: int, status: str, rows_processed: int, error_message: str | None = None):
    """
    Uses clock_timestamp() rather than now() for finished_at -- confirmed
    live 2026-09-14 (found in archive_router.py, applied here defensively
    for the same identical code pattern) that now() returns the current
    TRANSACTION's start time in Postgres, not real wall-clock time. A run
    with zero commit() calls between start and finish stays in one
    long-lived transaction, making now() at the end look like it happened
    right at the start. clock_timestamp() is immune to this.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE automation_job_run_log SET status = %s, rows_processed = %s, "
            "error_message = %s, finished_at = clock_timestamp() WHERE id = %s",
            (status, rows_processed, error_message, job_run_id),
        )
    conn.commit()


def main():
    """
    Entry point for cron. Loops every client in MAIN_SHEETS, moving any row
    that's ROUTE-set and Approved. A single client failing entirely (e.g. a
    Smartsheet outage mid-fetch) is logged and skipped — it does NOT abort
    the rest of the run, same "one bad thing doesn't stop the batch"
    philosophy as mover.py's per-row error handling.

    Same lock-file convention as every other job in this repo
    (Sync_MasterRotation.py etc.): file-based, 60-minute zombie-lock
    timeout, so an overlapping cron tick doesn't stack on top of a still-
    running pass.
    """
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > 3600:
            logger.warning("Zombie lock detected (older than 60 min) — clearing it")
            os.remove(LOCK_FILE)
        else:
            logger.info("route_resolver already running — aborting to avoid overlap")
            return

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(time.time()))

    conn = None
    job_run_id = None
    total_rows_moved = 0
    start_time = time.time()
    known_bad_targets_this_run = set()

    try:
        conn = get_connection()
        ss_client = get_smartsheet_client()
        folder_cache = _FolderSheetCache(ss_client)
        job_run_id = _start_job_run(conn, JOB_NAME)

        for client_slug, config in MAIN_SHEETS.items():
            try:
                stats = process_sheet(conn, ss_client, folder_cache, client_slug, config["main_sheet_id"], known_bad_targets_this_run)
                total_rows_moved += stats["rows_moved"]
                logger.info("Client %s done: %s", client_slug, stats)
            except Exception as exc:
                logger.error("Client %s failed entirely (skipping to next client): %s", client_slug, exc, exc_info=True)

        duration = round(time.time() - start_time, 1)
        _finish_job_run(conn, job_run_id, status="success", rows_processed=total_rows_moved)
        logger.info("route_resolver run complete: %s rows moved across %s clients in %ss",
                     total_rows_moved, len(MAIN_SHEETS), duration)

    except Exception as exc:
        logger.error("route_resolver run failed: %s", exc, exc_info=True)
        if conn and job_run_id:
            _finish_job_run(conn, job_run_id, status="failed", rows_processed=total_rows_moved, error_message=str(exc))
    finally:
        if conn:
            conn.close()
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()