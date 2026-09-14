"""
main_to_contractor.py — Main sheet -> Contractor sheet (COPY, not move)

Confirmed with Jay 2026-09-14 (Wednesday 4pm deadline, time-constrained
build): a row's per-region SUBMIT checkbox (e.g. "ZAM: SUBMIT",
"DRC: SUBMIT") on any of the 12 client main sheets is the trigger to COPY
that row to the matching contractor sheet in the Contractors workspace.
The specific contractor company name in the target sheet's title is
irrelevant/arbitrary internal naming (confirmed explicitly) -- only the
REGION matters, and every contractor sheet currently maps 1:1 to a
region. A row can have more than one region checked; per Jay, that means
copying to every matching contractor sheet, not just one.

Idempotency: since nothing gets unchecked or removed after a copy
(unlike Part 1/Part 2's move-based pipelines), re-scanning the same row
on every cron tick would re-copy it forever without a dedup mechanism.
Migration 011 adds main_to_contractor_copy_log with a unique index on
(source_sheet_id, source_row_id, region_code) -- checked before every
copy attempt.

MAL (Malawi) has a SUBMIT checkbox on the main sheets but NO contractor
sheet exists for it yet -- rows with MAL: SUBMIT checked are logged and
alerted, not silently dropped, same fail-safe philosophy as the rest of
this codebase.

NOT YET LIVE-TESTED (time-constrained build) -- needs the same kind of
live dry run every other piece of this system got before being trusted
on cron.
"""

import os
import time
import logging

from dotenv import load_dotenv
import requests
import psycopg2.extras

load_dotenv()

from jobs.approval_router.backup import get_connection
from jobs.approval_router.mover import get_smartsheet_client
from jobs.approval_router.copier import copy_row, CopierError
from jobs.approval_router.route_resolver import MAIN_SHEETS

logger = logging.getLogger("main_to_contractor")

# Region code (as it appears before ": SUBMIT" in the checkbox column
# title) -> Contractors workspace sheet_id. Confirmed live 2026-09-14 --
# every contractor sheet currently maps to exactly one region; the
# contractor company name itself is NOT used for matching (confirmed
# with Jay: it's arbitrary internal naming, not a routing key).
REGION_TO_CONTRACTOR_SHEET: dict[str, int] = {
    "MOZ": 7071129797611396,   # A-Track Mozambique
    "BOTS": 4096231923994500,  # ADARS Botswana
    "SA": 8244727318007684,    # ADARS SA
    "DRC": 1413715040620420,   # SPD DRC
    "TAN": 992501116653444,    # SPS Tanzania
    "ZAM": 2879447154773892,   # SPS Zambia
    "NAM": 3887174390861700,   # WR Namibia
    "ZIM": 4599803954548612,   # Zimbabwe
}
# MAL (Malawi) has a SUBMIT checkbox on main sheets but no contractor
# sheet yet -- deliberately absent from the map above so it's caught by
# the "unmapped region" path below instead of silently matched.

JOB_NAME = "main_to_contractor"


def _find_submit_columns(columns_by_id: dict[int, str]) -> dict[int, str]:
    """Returns {column_id: region_code} for every "<REGION>: SUBMIT"
    checkbox column on a sheet."""
    result = {}
    for col_id, title in columns_by_id.items():
        stripped = title.strip()
        if stripped.upper().endswith(": SUBMIT"):
            region_code = stripped[:-len(": SUBMIT")].strip().upper()
            result[col_id] = region_code
    return result


def _already_attempted(conn, source_sheet_id: int, source_row_id: int, region_code: str) -> bool:
    """
    True if this row+region has EVER been attempted before, regardless of
    outcome -- confirmed live 2026-09-14 this needs to include 'error' as
    well as 'copied': SPD DRC was already full in production, and without
    this, every single cron run would re-attempt every already-failed
    DRC row forever (hammering the API with the same 5636 error on
    repeat). A failed attempt now stays failed until a human clears it
    (e.g. deletes the error row after fixing the target sheet), same
    "logged, not silently retried" philosophy as the rest of this
    codebase's error handling.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM main_to_contractor_copy_log "
            "WHERE source_sheet_id = %s AND source_row_id = %s AND region_code = %s",
            (source_sheet_id, source_row_id, region_code),
        )
        return cur.fetchone() is not None


def _log_copy(conn, source_sheet_id, source_row_id, region_code, target_sheet_id,
              target_row_id, client_slug, status, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO main_to_contractor_copy_log "
            "(source_sheet_id, source_row_id, region_code, target_sheet_id, target_row_id, "
            "client_slug, status, error_message) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (source_sheet_id, source_row_id, region_code, target_sheet_id, target_row_id,
             client_slug, status, error_message),
        )
    conn.commit()


def _send_telegram(message: str) -> tuple[bool, str | None]:
    token = os.getenv("LOG_BOT_TOKEN")
    chat_id = os.getenv("MAIN_TO_CONTRACTOR_LOG_ID") or os.getenv("RISK_LOG_ID")
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


def _alert_unmapped_region(conn, client_slug: str, source_row_id: int, region_code: str):
    message = (
        f"⚠️ *main_to_contractor*: no contractor sheet mapped for region `{region_code}`\n"
        f"Client: *{client_slug}*, row: `{source_row_id}`\n"
        f"Region checkbox is checked but has nowhere to copy to."
    )
    delivered, delivery_error = _send_telegram(message)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_telegram_alert_log "
            "(severity, job_name, chat_id, message, delivered, delivery_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("warning", JOB_NAME, os.getenv("MAIN_TO_CONTRACTOR_LOG_ID") or os.getenv("RISK_LOG_ID") or "",
             message, delivered, delivery_error),
        )
    conn.commit()


def process_sheet(conn, ss_client, client_slug: str, main_sheet_id: int) -> dict[str, int]:
    stats = {"rows_checked": 0, "copies_made": 0, "already_copied": 0, "unmapped_region": 0, "errors": 0}

    sheet = ss_client.Sheets.get_sheet(main_sheet_id)
    columns_by_id = {c.id: c.title for c in sheet.columns}
    submit_columns = _find_submit_columns(columns_by_id)

    if not submit_columns:
        return stats

    for row in sheet.rows:
        stats["rows_checked"] += 1
        cells_by_col = {cell.column_id: cell for cell in row.cells}

        for col_id, region_code in submit_columns.items():
            cell = cells_by_col.get(col_id)
            if not cell or cell.value is not True:
                continue

            if _already_attempted(conn, main_sheet_id, row.id, region_code):
                stats["already_copied"] += 1
                continue

            target_sheet_id = REGION_TO_CONTRACTOR_SHEET.get(region_code)
            if target_sheet_id is None:
                stats["unmapped_region"] += 1
                logger.warning("Unmapped region %r for row %s on %s", region_code, row.id, client_slug)
                _alert_unmapped_region(conn, client_slug, row.id, region_code)
                _log_copy(conn, main_sheet_id, row.id, region_code, None, None, client_slug,
                          "error", f"No contractor sheet mapped for region {region_code!r}")
                continue

            try:
                target_row_id = copy_row(ss_client, main_sheet_id, row.id, target_sheet_id)
                _log_copy(conn, main_sheet_id, row.id, region_code, target_sheet_id,
                          target_row_id, client_slug, "copied")
                stats["copies_made"] += 1
                logger.info("Copied row %s (%s) on %s -> contractor sheet %s (target_row=%s)",
                            row.id, region_code, client_slug, target_sheet_id, target_row_id)
            except CopierError as exc:
                stats["errors"] += 1
                logger.error("Copy failed for row %s (%s) on %s: %s", row.id, region_code, client_slug, exc)
                _log_copy(conn, main_sheet_id, row.id, region_code, target_sheet_id, None,
                          client_slug, "error", str(exc))

    return stats


LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/main_to_contractor.lock"


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
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE automation_job_run_log SET status = %s, rows_processed = %s, "
            "error_message = %s, finished_at = clock_timestamp() WHERE id = %s",
            (status, rows_processed, error_message, job_run_id),
        )
    conn.commit()


def main():
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > 3600:
            logger.warning("Zombie lock detected (older than 60 min) — clearing it")
            os.remove(LOCK_FILE)
        else:
            logger.info("main_to_contractor already running — aborting to avoid overlap")
            return

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(time.time()))

    conn = None
    job_run_id = None
    total_copies = 0
    start_time = time.time()

    try:
        conn = get_connection()
        ss_client = get_smartsheet_client()
        job_run_id = _start_job_run(conn, JOB_NAME)

        for client_slug, config in MAIN_SHEETS.items():
            try:
                stats = process_sheet(conn, ss_client, client_slug, config["main_sheet_id"])
                total_copies += stats["copies_made"]
                logger.info("Client %s done: %s", client_slug, stats)
            except Exception as exc:
                logger.error("Client %s failed entirely (skipping to next client): %s", client_slug, exc, exc_info=True)

        duration = round(time.time() - start_time, 1)
        _finish_job_run(conn, job_run_id, status="success", rows_processed=total_copies)
        logger.info("main_to_contractor run complete: %s copies made across %s clients in %ss",
                     total_copies, len(MAIN_SHEETS), duration)

    except Exception as exc:
        logger.error("main_to_contractor run failed: %s", exc, exc_info=True)
        if conn and job_run_id:
            _finish_job_run(conn, job_run_id, status="failed", rows_processed=total_copies, error_message=str(exc))
    finally:
        if conn:
            conn.close()
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()