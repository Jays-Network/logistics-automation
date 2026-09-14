"""
archive_router.py — approval_router, Part 2: Invoicing -> Archive

Deliberately its OWN file, separate from route_resolver.py (Part 1), even
though both live under jobs/approval_router/ and both call mover.py's
move_row(). Part 1 and Part 2 have genuinely different triggers, different
source sheets, and — critically — Part 2 has a failure mode Part 1 never
sees (a full destination sheet) with its own multi-step recovery flow.
Keeping them in separate files means a bug or change in one can be
diagnosed and fixed without having to read through the other's logic —
that separation was an explicit request (2026-09-14), not an accident.

Trigger: a checkbox column literally named "Archive" on every invoicing
sheet (confirmed live 2026-09-14 across two independent sheet lineages —
Bridge-derived and Alistair-derived templates both have it under the same
name, so this is a real convention, not something inherited from one
template family).

Source sheets: every sheet in a client's invoicing folder EXCEPT that
client's own Archive sheet, looked up from approval_router_archive_state
(migration 010 seeds this from the 12 confirmed Archive sheets found live
2026-09-14) rather than guessed from folder listing/naming — sheet names
alone aren't a reliable "is this the archive" signal.

Target: the client's currently active archive sheet
(approval_router_archive_state.is_active = true). If a move fails with
mover.SHEET_FULL_ERROR_CODE (5636, confirmed live 2026-09-10 against a
real full sheet), that triggers the rotation flow below instead of just
logging an error.
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

from jobs.approval_router.backup import get_connection
from jobs.approval_router.mover import get_smartsheet_client, move_row, MoverError, SHEET_FULL_ERROR_CODE

logger = logging.getLogger("approval_router.archive_router")

ARCHIVE_CHECKBOX_COLUMN = "Archive"

# client_slug -> invoicing folder_id. Deliberately duplicated from
# route_resolver.py's MAIN_SHEETS rather than imported — keeps this module
# fully independent, so a bug in Part 1 can never break Part 2 or vice
# versa. If a client's folder_id ever changes, both files need updating.
INVOICING_FOLDERS: dict[str, int] = {
    "alistair":               5153479745398660,
    "reload":                 2409098722469764,
    "glencore_international": 7194173326550916,
    "fh_bertling":            2127623745759108,
    "bridge":                 1397548024915844,
    "goldvale":               4493772768733060,
    "gsm":                    5399770350020484,
    "ixm":                    5012742257043332,
    "mittal":                 8812654442637188,
    "sls_africa":             6807145233573764,
    "sls_trading":            6701592117307268,
    "zalawi":                 6490485884774276,
}


MAX_SHEET_NAME_LENGTH = 50


def _safe_sheet_name(base: str, suffix: str) -> str:
    """
    Smartsheet enforces a hard 50-character sheet name limit (confirmed
    live 2026-09-14 — errorCode 1041, hit on the very first rotation test:
    a 45-character original name plus a 6-character " (new)" suffix came
    to 51 and was rejected). Truncates `base` as needed so `base + suffix`
    never exceeds the limit, rather than letting a long original name
    silently break the rotation.
    """
    max_base_length = MAX_SHEET_NAME_LENGTH - len(suffix)
    if max_base_length < 1:
        raise ArchiveRouterError(f"Suffix {suffix!r} alone exceeds the 50-character sheet name limit")
    return base[:max_base_length] + suffix


class ArchiveRouterError(Exception):
    """Raised for a genuinely unrecoverable Part 2 failure — e.g. the
    rotation flow itself failing partway. Like mover.MoverError, this is
    caught internally per-client and logged rather than propagated, so one
    client's archive failing doesn't stop the whole run."""
    pass


def _get_active_archive(conn, client_slug: str) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, sheet_id, sheet_name FROM approval_router_archive_state "
            "WHERE client_slug = %s AND is_active = true",
            (client_slug,),
        )
        return cur.fetchone()


def _deactivate_and_insert_archive(conn, client_slug: str, old_sheet_id: int,
                                    new_sheet_id: int, new_sheet_name: str):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE approval_router_archive_state SET is_active = false, deactivated_at = now() "
            "WHERE client_slug = %s AND sheet_id = %s AND is_active = true",
            (client_slug, old_sheet_id),
        )
        cur.execute(
            "INSERT INTO approval_router_archive_state (client_slug, sheet_id, sheet_name, is_active) "
            "VALUES (%s, %s, %s, true)",
            (client_slug, new_sheet_id, new_sheet_name),
        )
    conn.commit()


def _send_telegram(message: str) -> tuple[bool, str | None]:
    """Same shape as route_resolver.py's _send_telegram() — deliberately
    duplicated rather than imported, for the same file-independence reason
    as INVOICING_FOLDERS above."""
    token = os.getenv("LOG_BOT_TOKEN")
    chat_id = os.getenv("ARCHIVE_ROUTER_LOG_ID") or os.getenv("RISK_LOG_ID")
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


def _log_alert(conn, message: str, severity: str = "info"):
    delivered, delivery_error = _send_telegram(message)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_telegram_alert_log "
            "(severity, job_name, chat_id, message, delivered, delivery_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (severity, JOB_NAME, os.getenv("ARCHIVE_ROUTER_LOG_ID") or os.getenv("RISK_LOG_ID") or "",
             message, delivered, delivery_error),
        )
    conn.commit()


def _rename_sheet_checked(ss_client, sheet_id: int, new_name: str, client_slug: str, max_attempts: int = 4):
    """
    Renames a sheet and actually confirms it worked, unlike a bare
    Sheets.update_sheet() call — errors_as_exceptions is False (mover.py's
    get_smartsheet_client() convention), so a failed rename returns an
    Error object rather than raising, and silently ignoring that return
    value is exactly the bug found live 2026-09-14: step 3 of a real
    rotation test failed silently while step 2 (identical code) succeeded,
    leaving the new archive sheet stuck with its temporary "(new)" name.

    Retries with a short backoff, since the specific failure seen was very
    likely Smartsheet's backend not having fully released a just-vacated
    name yet — a timing issue, not a permanent one. Raises ArchiveRouterError
    only after genuinely exhausting the retries.
    """
    last_message = None
    for attempt in range(1, max_attempts + 1):
        response = ss_client.Sheets.update_sheet(sheet_id, smartsheet.models.Sheet({"name": new_name}))
        if not isinstance(response, smartsheet.models.Error):
            # Confirm on Smartsheet's own record, not just a non-error
            # response — belt and braces given this exact silent-failure
            # history.
            confirmed = ss_client.Sheets.get_sheet(sheet_id)
            if confirmed.name == new_name:
                return
            last_message = f"update_sheet returned success but sheet name is still {confirmed.name!r}"
        else:
            last_message = response.result.message
        if attempt < max_attempts:
            wait = 2 * attempt
            logger.warning("Rename to %r failed for %s (attempt %s/%s): %s — retrying in %ss",
                            new_name, client_slug, attempt, max_attempts, last_message, wait)
            time.sleep(wait)
    raise ArchiveRouterError(f"Could not rename sheet {sheet_id} to {new_name!r} for {client_slug} "
                              f"after {max_attempts} attempts: {last_message}")


def _rotate_archive_sheet(conn, ss_client, client_slug: str, full_sheet_id: int, folder_id: int) -> int:
    """
    Handles a full archive sheet: creates a blank copy, gives it the clean
    original name, renames the full one out of the way with a timestamp
    suffix, and updates approval_router_archive_state. Returns the new
    sheet_id.

    NOT yet live-verified end-to-end (2026-09-14) — this only runs when a
    real archive sheet actually hits the 500k-cell limit, which hasn't
    happened yet in production. The individual API calls (copy_sheet,
    update_sheet) follow documented Smartsheet SDK patterns but haven't
    been smoke-tested against a real full sheet the way SHEET_FULL_ERROR_CODE
    itself was. Flagged here deliberately rather than assumed correct —
    worth a dedicated dry run before the first real rotation happens, the
    same way backup.py/mover.py were proven live before Part 1 went out.

    Sequencing matters: the clean original name is held by the FULL sheet
    until step 2, so the copy has to get a temporary name first (step 1),
    then the full sheet is renamed out of the way (step 2), then the copy
    can take the clean name (step 3) — doing this in any other order risks
    a naming collision.
    """
    full_sheet = ss_client.Sheets.get_sheet(full_sheet_id)
    original_name = full_sheet.name

    # Step 1: copy the full sheet (structure only, no data) under a
    # temporary name.
    temp_name = _safe_sheet_name(original_name, " (new)")
    directive = smartsheet.models.ContainerDestination({
        "destination_type": "folder",
        "destination_id": folder_id,
        "new_name": temp_name,
    })
    copy_response = ss_client.Sheets.copy_sheet(full_sheet_id, directive, include=[])
    if isinstance(copy_response, smartsheet.models.Error):
        raise ArchiveRouterError(f"copy_sheet failed for {client_slug}: {copy_response.result.message}")
    new_sheet_id = copy_response.result.id

    # Step 2: rename the full sheet out of the way.
    timestamp = time.strftime("%Y-%m-%d")
    full_renamed = _safe_sheet_name(original_name, f"_FULL_{timestamp}")
    _rename_sheet_checked(ss_client, full_sheet_id, full_renamed, client_slug)

    # Step 3: give the new copy the clean original name. Confirmed live
    # 2026-09-14 that this can fail on the first attempt even though step 2
    # (identical code, different sheet) just succeeded — almost certainly
    # Smartsheet's backend not having fully released the vacated name yet.
    # _rename_sheet_checked retries with backoff for exactly this reason;
    # step 4 below only runs once this genuinely succeeds.
    _rename_sheet_checked(ss_client, new_sheet_id, original_name, client_slug)

    # Step 4: update the DB state.
    _deactivate_and_insert_archive(conn, client_slug, full_sheet_id, new_sheet_id, original_name)

    logger.info("Rotated archive sheet for %s: %s (now %s) -> new active %s",
                client_slug, full_sheet_id, full_renamed, new_sheet_id)
    _log_alert(conn,
        f"📦 *archive_router*: rotated full archive sheet for *{client_slug}*\n"
        f"Old sheet renamed to `{full_renamed}` (id {full_sheet_id})\n"
        f"New active archive: `{original_name}` (id {new_sheet_id})\n"
        f"No action needed — this is an FYI."
    )
    return new_sheet_id


def process_client_archive(conn, ss_client, sheets_in_folder: list, client_slug: str, folder_id: int) -> dict[str, int]:
    """
    Scans every non-archive sheet in a client's invoicing folder for rows
    with the Archive checkbox checked, and moves each one to the client's
    active archive sheet. Handles full-sheet rotation transparently — the
    caller doesn't need to know a rotation happened mid-run.
    """
    stats = {"sheets_checked": 0, "rows_checked": 0, "rows_moved": 0, "rotations": 0}

    active_archive = _get_active_archive(conn, client_slug)
    if active_archive is None:
        logger.error("No active archive sheet found in DB for %s — skipping client entirely", client_slug)
        return stats
    archive_sheet_id = active_archive["sheet_id"]

    for sheet_summary in sheets_in_folder:
        if sheet_summary.id == archive_sheet_id:
            continue  # never scan the archive sheet itself as a source
        stats["sheets_checked"] += 1

        sheet = ss_client.Sheets.get_sheet(sheet_summary.id)
        columns_by_id = {c.id: c.title for c in sheet.columns}
        archive_col_id = next(
            (cid for cid, title in columns_by_id.items()
             if title.strip().casefold() == ARCHIVE_CHECKBOX_COLUMN.casefold()),
            None,
        )
        if archive_col_id is None:
            continue  # no Archive column on this sheet — skip silently, not every sheet needs one

        for row in sheet.rows:
            stats["rows_checked"] += 1
            cells_by_col = {cell.column_id: cell for cell in row.cells}
            archive_cell = cells_by_col.get(archive_col_id)
            if not archive_cell or archive_cell.value is not True:
                continue

            move_log_id = move_row(
                conn, ss_client,
                source_sheet_id=sheet_summary.id,
                source_row=row,
                columns_by_id=columns_by_id,
                target_sheet_id=archive_sheet_id,
                client_slug=client_slug,
                route_value=None,
                mine_value=None,
            )

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT status, error_code FROM approval_router_row_move_log WHERE id = %s",
                    (move_log_id,),
                )
                move_log = cur.fetchone()

            if move_log and move_log["status"] == "error" and move_log["error_code"] == SHEET_FULL_ERROR_CODE:
                logger.warning("Archive sheet full for %s (sheet_id=%s) — rotating", client_slug, archive_sheet_id)
                try:
                    archive_sheet_id = _rotate_archive_sheet(conn, ss_client, client_slug, archive_sheet_id, folder_id)
                    stats["rotations"] += 1
                except ArchiveRouterError as exc:
                    logger.error("Rotation failed for %s: %s — row %s left unarchived, will retry next run",
                                 client_slug, exc, row.id)
                    continue

                # Retry the move against the newly-rotated sheet. This
                # writes a second backup_row() entry for the same row —
                # harmless duplication (backups are cheap insurance, not a
                # scarce resource), not a bug.
                move_row(
                    conn, ss_client,
                    source_sheet_id=sheet_summary.id,
                    source_row=row,
                    columns_by_id=columns_by_id,
                    target_sheet_id=archive_sheet_id,
                    client_slug=client_slug,
                    route_value=None,
                    mine_value=None,
                )

            stats["rows_moved"] += 1

    return stats


LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/archive_router.lock"
JOB_NAME = "archive_router_part2"


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
    Entry point for cron. Loops every client in INVOICING_FOLDERS, checking
    every non-archive sheet in that client's folder for Archive-checked
    rows and moving them. A single client failing entirely is logged and
    skipped, same "one bad thing doesn't stop the batch" philosophy as
    route_resolver.py — and the same lock-file/zombie-timeout convention.
    """
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > 3600:
            logger.warning("Zombie lock detected (older than 60 min) — clearing it")
            os.remove(LOCK_FILE)
        else:
            logger.info("archive_router already running — aborting to avoid overlap")
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
        job_run_id = _start_job_run(conn, JOB_NAME)

        for client_slug, folder_id in INVOICING_FOLDERS.items():
            try:
                folder = ss_client.Folders.get_folder(folder_id)
                sheets_in_folder = folder.sheets or []
                stats = process_client_archive(conn, ss_client, sheets_in_folder, client_slug, folder_id)
                total_rows_moved += stats["rows_moved"]
                logger.info("Client %s done: %s", client_slug, stats)
            except Exception as exc:
                logger.error("Client %s failed entirely (skipping to next client): %s", client_slug, exc, exc_info=True)

        duration = round(time.time() - start_time, 1)
        _finish_job_run(conn, job_run_id, status="success", rows_processed=total_rows_moved)
        logger.info("archive_router run complete: %s rows moved across %s clients in %ss",
                     total_rows_moved, len(INVOICING_FOLDERS), duration)

    except Exception as exc:
        logger.error("archive_router run failed: %s", exc, exc_info=True)
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