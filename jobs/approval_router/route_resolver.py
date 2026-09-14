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

The plain "Glencore" folder (main-tracking id 5600395335624580) is a
genuinely different, actively-used operation with its own sheet structure
(no single main-sheet + ROUTE + approval pattern) — confirmed with Jay
2026-09-13 that it's out of scope. It's simply not in MAIN_SHEETS below.
"""

import os
import time
import logging
from typing import Any

from dotenv import load_dotenv
import requests
import smartsheet
import psycopg2.extras

load_dotenv()

from jobs.approval_router.backup import get_connection, row_to_dict
from jobs.approval_router.mover import get_smartsheet_client, move_row, MoverError

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

# Confirmed 2026-09-13: Bridge's ROUTE picklist previously had two options
# ("Sicomine Mine-DBN" and "Sicomine Mine-DAR") pointing at one combined
# sheet. Jay asked for these split into two separate sheets instead of
# staying merged — done live (new sheets BRIDGE-SICOMINE MINE-DBN /
# BRIDGE-SICOMINE MINE-DAR, both empty, created from the same template as
# the other Bridge sheets). Both ROUTE values now resolve correctly through
# the normal prefix+sheet-name matching below with no override needed. Kept
# as an empty dict (rather than removed) since it's a real mechanism other
# clients may need in the future if a similar merge turns up.
KNOWN_ROUTE_OVERRIDES: dict[str, int] = {}

# RELOAD's special case: when ROUTE == "Reload-DRC-LOCAL", the LOCAL ROUTE
# column (not ROUTE) determines the real target — one of 4 sheets. Confirmed
# live against the real LOCAL ROUTE picklist 2026-09-13 (29 options, matches
# Jay's original table exactly). Keyed by normalized LOCAL ROUTE value.
RELOAD_TRIGGER_ROUTE = "reload-drc-local"

RELOAD_KOLWEZI = 907292680605572
RELOAD_LIKASI = 3831881941340036
RELOAD_FUNGURUME = 4887378744266628
RELOAD_KASUMBALESA = 869874388651908

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
    normalized: RELOAD_KASUMBALESA for normalized in (
        "reload-sem - kas", "reload-kicc - kas",
    )
})


def _norm(value: str) -> str:
    """Casefold + whitespace-collapse for tolerant matching — handles the
    casing and stray-space inconsistencies confirmed live across real ROUTE
    data (trailing spaces, "Bridge-CCS-serenje" vs "BRIDGE-CCS-SERENJE ",
    etc.)."""
    return " ".join(value.split()).casefold()


def _find_approval_column(columns_by_id: dict[int, str]) -> int | None:
    """Finds the "<Manager Name> approval" column by suffix match — the
    manager's name varies per client/sheet, only the " approval" suffix is
    stable. Confirmed with Jay 2026-09-13: the separate "Load approval
    status" column must NOT be used, even though it looks similar."""
    for col_id, title in columns_by_id.items():
        if title.strip().casefold().endswith("approval") and title.strip().casefold() != "load approval status":
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
) -> tuple[int | None, str]:
    """
    Resolves a ROUTE value (plus, for RELOAD, the LOCAL ROUTE value) to a
    target invoicing sheet_id.

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


def process_sheet(conn, ss_client, folder_cache: "_FolderSheetCache", client_slug: str, main_sheet_id: int) -> dict[str, int]:
    """
    Checks one client's main sheet for approved, routed rows and moves them.
    Returns a small stats dict for logging. Never raises for a single bad
    row — logs and continues, same philosophy as mover.py.
    """
    stats = {"rows_checked": 0, "rows_moved": 0, "rows_skipped_no_route": 0, "rows_skipped_not_approved": 0, "rows_failed_resolve": 0}

    sheet = ss_client.Sheets.get_sheet(main_sheet_id)
    columns_by_id = {c.id: c.title for c in sheet.columns}

    route_col_id = _find_column_id(columns_by_id, "ROUTE") or _find_column_id(columns_by_id, "Routes")
    local_route_col_id = _find_column_id(columns_by_id, "LOCAL ROUTE")
    approval_col_id = _find_approval_column(columns_by_id)

    if route_col_id is None:
        logger.error("No ROUTE/Routes column found on %s main sheet (sheet_id=%s) — skipping", client_slug, main_sheet_id)
        return stats
    if approval_col_id is None:
        logger.error("No approval column found on %s main sheet (sheet_id=%s) — skipping", client_slug, main_sheet_id)
        return stats

    for row in sheet.rows:
        stats["rows_checked"] += 1
        cells_by_col = {cell.column_id: cell for cell in row.cells}

        route_cell = cells_by_col.get(route_col_id)
        route_value = (route_cell.display_value or route_cell.value) if route_cell else None
        if not route_value or not str(route_value).strip():
            stats["rows_skipped_no_route"] += 1
            continue
        route_value = str(route_value)

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

        target_sheet_id, reason = resolve_target_sheet_id(route_value, local_route_value, folder_cache)
        if target_sheet_id is None:
            stats["rows_failed_resolve"] += 1
            logger.warning(
                "Could not resolve target for row %s on %s (ROUTE=%r, LOCAL ROUTE=%r): %s",
                row.id, client_slug, route_value, local_route_value, reason,
            )
            _alert_unresolvable_route(conn, client_slug, route_value, local_route_value, reason)
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
        stats["rows_moved"] += 1
        logger.info("Row %s on %s moved (ROUTE=%r) -> move_log_id=%s", row.id, client_slug, route_value, move_log_id)

    return stats


LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/route_resolver.lock"
JOB_NAME = "route_resolver_part1"


def _start_job_run(conn, job_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_job_run_log (job_name, status) VALUES (%s, 'running') RETURNING id",
            (job_name,),
        )
        job_run_id = cur.fetchone()[0]
    conn.commit()
    return job_run_id


def _finish_job_run(conn, job_run_id: int, status: str, rows_processed: int, error_message: str | None = None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE automation_job_run_log SET status = %s, rows_processed = %s, "
            "error_message = %s, finished_at = now() WHERE id = %s",
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

    try:
        conn = get_connection()
        ss_client = get_smartsheet_client()
        folder_cache = _FolderSheetCache(ss_client)
        job_run_id = _start_job_run(conn, JOB_NAME)

        for client_slug, config in MAIN_SHEETS.items():
            try:
                stats = process_sheet(conn, ss_client, folder_cache, client_slug, config["main_sheet_id"])
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