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
import signal
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
    # Added 2026-10-12: the separate GLENCORE folder (not GLENCORE INTERNATIONAL)
    # was never scanned. Needs an approval_router_archive_state row first
    # (GLENCORE Archive 4364822220066692) -- see sql note delivered with this patch.
    "glencore":               3007233047979908,
    # Added 2026-10-16. Alliance moves tracking -> invoicing via a Smartsheet
    # approval workflow (not route_resolver), so it is archive-only here.
    # Needs an approval_router_archive_state row (sql/migrations/019).
    "alliance":               6083455994881924,
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


# ---------------------------------------------------------------------------
# 2026-10-12 hardening
#  * sub-folders are scanned (e.g. RELOAD/Reload-DRC-LOCAL)
#  * a sheet is only marked "checked" when EVERY ticked row on it moved;
#    failed rows are retried next run (max MAX_ROW_ATTEMPTS per 24h)
#  * Smartsheet request timeouts + whole-run deadline + lock heartbeat
#    (same protections as route_resolver.py)
# ---------------------------------------------------------------------------
MAX_FOLDER_DEPTH = 4
MAX_MOVES_PER_RUN = 300        # backlog drains over several hourly runs
DRY_RUN = os.getenv("ARCHIVE_DRY_RUN") == "1"   # ARCHIVE_DRY_RUN=1 -> log what WOULD move, change nothing
MAX_ROW_ATTEMPTS = 3           # failed attempts per row per 24h before we stop retrying
SMARTSHEET_CONNECT_TIMEOUT = 10
SMARTSHEET_READ_TIMEOUT = 90
RUN_MAX_SECONDS = 3000         # 50 min ceiling; cron fires hourly
LOCK_STALE_SECONDS = 1800      # measured from the last heartbeat
_LOCK_TOKEN = None


class _RunDeadline(BaseException):
    """BaseException on purpose: per-row / per-client `except Exception`
    blocks must not swallow the deadline."""


def _on_run_deadline(signum, frame):
    raise _RunDeadline(f"run exceeded {RUN_MAX_SECONDS}s")


def _apply_request_timeout(ss_client):
    """The Smartsheet SDK sends requests with no timeout; wrap send()."""
    session = None
    for value in list(vars(ss_client).values()):   # __dict__ only: the SDK's __getattr__ fabricates attributes
        if callable(getattr(value, "send", None)) and callable(getattr(value, "mount", None)):
            session = value                          # a requests.Session
            break
    if session is None:
        logger.warning("Could not find the Smartsheet HTTP session -- request timeouts NOT applied")
        return ss_client
    original_send = session.send

    def send_with_timeout(request, **kwargs):
        kwargs.setdefault("timeout", (SMARTSHEET_CONNECT_TIMEOUT, SMARTSHEET_READ_TIMEOUT))
        return original_send(request, **kwargs)

    session.send = send_with_timeout
    return ss_client


def _touch_lock():
    try:
        os.utime(LOCK_FILE, None)
    except OSError:
        pass


def _collect_sheets(ss_client, folder_id: int, depth: int = 0, seen: set | None = None) -> list:
    """All sheets in a folder INCLUDING nested folders. Returns sheet summaries
    (objects with .id/.name), de-duplicated by id."""
    seen = seen if seen is not None else set()
    folder = ss_client.Folders.get_folder(folder_id)
    if isinstance(folder, smartsheet.models.Error):
        raise ArchiveRouterError(f"get_folder({folder_id}) failed: {folder.result.message}")
    out = []
    for sh in (folder.sheets or []):
        if sh.id not in seen:
            seen.add(sh.id)
            out.append(sh)
    if depth < MAX_FOLDER_DEPTH:
        for sub in (folder.folders or []):
            if sub.id in seen:
                continue
            seen.add(sub.id)
            out.extend(_collect_sheets(ss_client, sub.id, depth + 1, seen))
    return out


def _known_archive_ids(conn, client_slug: str) -> set:
    """Every archive sheet ever registered for the client (active or rotated)
    -- none of them may be scanned as a source."""
    with conn.cursor() as cur:
        cur.execute("SELECT sheet_id FROM approval_router_archive_state WHERE client_slug = %s", (client_slug,))
        return {r[0] for r in cur.fetchall()}


def _row_failed_attempts(conn, source_sheet_id: int, source_row_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM approval_router_row_move_log "
            "WHERE source_sheet_id = %s AND source_row_id = %s AND route_value IS NULL "
            "AND status = 'error' AND target_row_id IS NULL "
            "AND created_at > now() - interval '24 hours'",
            (source_sheet_id, source_row_id),
        )
        return cur.fetchone()[0]


def _move_succeeded(conn, move_log_id: int) -> bool:
    """A row has left the source once the move produced a target_row_id --
    even if the later verification step logged a mismatch."""
    with conn.cursor() as cur:
        cur.execute("SELECT target_row_id FROM approval_router_row_move_log WHERE id = %s", (move_log_id,))
        r = cur.fetchone()
    return bool(r and r[0] is not None)


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
            "UPDATE approval_router_archive_state SET is_active = false, deactivated_at = clock_timestamp() "
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


def _get_archive_sheet_state(conn, sheet_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_version, last_checkpoint_at FROM archive_router_sheet_state WHERE sheet_id = %s",
            (sheet_id,),
        )
        return cur.fetchone()


def _update_archive_sheet_state(conn, sheet_id: int, client_slug: str, version: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO archive_router_sheet_state (sheet_id, client_slug, last_version, last_checkpoint_at) "
            "VALUES (%s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (sheet_id) DO UPDATE SET client_slug = EXCLUDED.client_slug, "
            "last_version = EXCLUDED.last_version, last_checkpoint_at = EXCLUDED.last_checkpoint_at",
            (sheet_id, client_slug, version),
        )
    conn.commit()


def process_client_archive(conn, ss_client, sheets_in_folder: list, client_slug: str, folder_id: int,
                           moves_left: int = MAX_MOVES_PER_RUN) -> dict[str, int]:
    """
    Scans every non-archive sheet under a client's invoicing folder (nested
    folders included) for rows with the Archive checkbox checked, and moves
    each one to the client's active archive sheet. Handles full-sheet rotation
    transparently.

    DELTA: a cheap get_sheet_version() check skips unchanged sheets.
    A sheet's version/checkpoint is only recorded when EVERY ticked row on it
    was moved (or deliberately given up on after MAX_ROW_ATTEMPTS). A failed
    row therefore keeps the sheet "dirty" and is retried on the next run.
    Only genuinely moved rows count in stats["rows_moved"].
    """
    stats = {"sheets_checked": 0, "rows_checked": 0, "rows_moved": 0, "rows_failed": 0,
             "rotations": 0, "sheets_incomplete": 0}

    active_archive = _get_active_archive(conn, client_slug)
    if active_archive is None:
        logger.error("No active archive sheet found in DB for %s — skipping client entirely", client_slug)
        return stats
    archive_sheet_id = active_archive["sheet_id"]
    archive_ids = _known_archive_ids(conn, client_slug) | {archive_sheet_id}

    for sheet_summary in sheets_in_folder:
        if sheet_summary.id in archive_ids:
            continue  # never scan an archive sheet (active or rotated) as a source

        if moves_left - stats["rows_moved"] <= 0:
            stats["sheets_incomplete"] += 1
            continue  # per-run cap reached; remaining sheets are picked up next run

        current_version = ss_client.Sheets.get_sheet_version(sheet_summary.id).version
        state = _get_archive_sheet_state(conn, sheet_summary.id)
        last_version, last_checkpoint_at = state if state else (None, None)

        if last_version is not None and current_version == last_version:
            continue  # unchanged since last check, nothing to do here

        stats["sheets_checked"] += 1

        sheet = ss_client.Sheets.get_sheet(sheet_summary.id)
        columns_by_id = {c.id: c.title for c in sheet.columns}
        archive_col_id = next(
            (cid for cid, title in columns_by_id.items()
             if title.strip().casefold() == ARCHIVE_CHECKBOX_COLUMN.casefold()),
            None,
        )
        if archive_col_id is None:
            _update_archive_sheet_state(conn, sheet_summary.id, client_slug, current_version)
            continue  # no Archive column on this sheet — skip silently, not every sheet needs one

        sheet_incomplete = False
        for row in sheet.rows:
            _touch_lock()  # heartbeat: a live run is never mistaken for a zombie
            if last_checkpoint_at is not None and row.modified_at and row.modified_at <= last_checkpoint_at:
                continue  # this specific row hasn't changed since our last check

            stats["rows_checked"] += 1
            cells_by_col = {cell.column_id: cell for cell in row.cells}
            archive_cell = cells_by_col.get(archive_col_id)
            if not archive_cell or archive_cell.value is not True:
                continue

            if moves_left - stats["rows_moved"] <= 0:
                sheet_incomplete = True
                break  # cap reached mid-sheet; leave the sheet dirty

            if _row_failed_attempts(conn, sheet_summary.id, row.id) >= MAX_ROW_ATTEMPTS:
                logger.warning("Row %s on sheet %s failed %s times in 24h — not retrying (see row_move_log)",
                                row.id, sheet_summary.id, MAX_ROW_ATTEMPTS)
                continue  # given up for now; does not hold the sheet dirty forever

            if DRY_RUN:
                logger.info("DRY RUN: would archive row %s from %r (%s) -> archive sheet %s",
                             row.id, sheet_summary.name, sheet_summary.id, archive_sheet_id)
                stats["rows_moved"] += 1
                sheet_incomplete = True  # never checkpoint in a dry run
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
                    archive_ids.add(archive_sheet_id)
                    stats["rotations"] += 1
                except ArchiveRouterError as exc:
                    logger.error("Rotation failed for %s: %s — row %s left unarchived, will retry next run",
                                 client_slug, exc, row.id)
                    stats["rows_failed"] += 1
                    sheet_incomplete = True
                    continue

                # Retry the move against the newly-rotated sheet (writes a second
                # backup entry -- harmless duplication).
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

            if _move_succeeded(conn, move_log_id):
                stats["rows_moved"] += 1
            else:
                stats["rows_failed"] += 1
                sheet_incomplete = True
                attempts = _row_failed_attempts(conn, sheet_summary.id, row.id)
                logger.error("Archive move FAILED for row %s on sheet %s (%s) — attempt %s/%s, move_log_id=%s",
                              row.id, sheet_summary.id, sheet_summary.name, attempts, MAX_ROW_ATTEMPTS, move_log_id)
                if attempts == MAX_ROW_ATTEMPTS:
                    _log_alert(conn,
                        f"⚠️ *archive_router*: row {row.id} on *{sheet_summary.name}* ({client_slug}) "
                        f"failed to archive {MAX_ROW_ATTEMPTS} times in 24h. Giving up until it is edited. "
                        f"See approval_router_row_move_log (move_log_id {move_log_id}).",
                        severity="warning")

        if sheet_incomplete:
            stats["sheets_incomplete"] += 1  # do NOT checkpoint: leftover/failed rows are retried next run
        else:
            _update_archive_sheet_state(conn, sheet_summary.id, client_slug, current_version)

    return stats


LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/archive_router.lock"
JOB_NAME = "archive_router_part2"


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
    live 2026-09-14 that now() returns the current TRANSACTION's start
    time in Postgres, not real wall-clock time. Two real runs that moved
    0 rows (meaning zero commit() calls happened anywhere inside
    process_client_archive(), so the whole run stayed in one long-lived
    transaction) both logged a finished_at only ~1.1s after started_at,
    even though the run genuinely took ~52s end to end. clock_timestamp()
    is immune to this -- it always reflects the actual moment the
    statement executes, regardless of how long the surrounding
    transaction has been open.
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
    Entry point for cron. Loops every client in INVOICING_FOLDERS, checking
    every non-archive sheet (nested folders included) for Archive-checked
    rows and moving them. One client failing is logged and skipped.

    Hardened 2026-10-12: Smartsheet request timeouts, a whole-run deadline,
    a lock heartbeat (stale = 30 min without a heartbeat) and an owner-token
    lock release, matching route_resolver.py.
    """
    global _LOCK_TOKEN
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > LOCK_STALE_SECONDS:
            logger.warning("Zombie lock detected (no heartbeat for %ss) — clearing it", int(file_age))
            os.remove(LOCK_FILE)
        else:
            logger.info("archive_router already running — aborting to avoid overlap")
            return

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    _LOCK_TOKEN = f"{os.getpid()}-{time.time()}"
    with open(LOCK_FILE, "w") as f:
        f.write(_LOCK_TOKEN)

    signal.signal(signal.SIGALRM, _on_run_deadline)
    signal.alarm(RUN_MAX_SECONDS)

    conn = None
    job_run_id = None
    total_rows_moved = 0
    total_failed = 0
    start_time = time.time()

    try:
        conn = get_connection()
        ss_client = _apply_request_timeout(get_smartsheet_client())
        job_run_id = _start_job_run(conn, JOB_NAME)

        for client_slug, folder_id in INVOICING_FOLDERS.items():
            try:
                sheets_in_folder = _collect_sheets(ss_client, folder_id)
                stats = process_client_archive(conn, ss_client, sheets_in_folder, client_slug, folder_id,
                                               moves_left=MAX_MOVES_PER_RUN - total_rows_moved)
                total_rows_moved += stats["rows_moved"]
                total_failed += stats["rows_failed"]
                logger.info("Client %s done (%s sheets scanned incl. sub-folders): %s",
                             client_slug, len(sheets_in_folder), stats)
            except Exception as exc:
                logger.error("Client %s failed entirely (skipping to next client): %s", client_slug, exc, exc_info=True)

        duration = round(time.time() - start_time, 1)
        _finish_job_run(conn, job_run_id, status="success", rows_processed=total_rows_moved)
        logger.info("archive_router run complete: %s rows moved, %s failed, across %s clients in %ss",
                     total_rows_moved, total_failed, len(INVOICING_FOLDERS), duration)

    except _RunDeadline as exc:
        logger.critical("archive_router ABORTED by deadline: %s", exc)
        if conn and job_run_id:
            try:
                _finish_job_run(conn, job_run_id, status="failed", rows_processed=total_rows_moved,
                                error_message=f"deadline: {exc}")
            except Exception:
                pass
    except Exception as exc:
        logger.error("archive_router run failed: %s", exc, exc_info=True)
        if conn and job_run_id:
            _finish_job_run(conn, job_run_id, status="failed", rows_processed=total_rows_moved, error_message=str(exc))
    finally:
        signal.alarm(0)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        try:
            with open(LOCK_FILE) as f:
                owner_ok = (f.read().strip() == _LOCK_TOKEN)
            if owner_ok:
                os.remove(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()