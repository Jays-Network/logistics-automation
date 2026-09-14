"""
main_to_contractor.py — Main sheet -> Contractor sheet (COPY, not move)

Confirmed with Jay 2026-09-14: a row's per-region SUBMIT checkbox (e.g.
"ZAM: SUBMIT", "DRC: SUBMIT") on any of the 12 client main sheets is the
trigger to COPY that row to the matching contractor sheet in the
Contractors workspace. The specific contractor company name in the
target sheet's title is irrelevant/arbitrary internal naming -- only the
REGION matters, and every contractor sheet currently maps 1:1 to a
region. A row can have more than one region checked; that means copying
to every matching contractor sheet, not just one.

RETRY SEMANTICS (revised 2026-09-14 after a real production incident):
a failed copy (e.g. target sheet full) is retried on every SCHEDULED run
until it succeeds or a human clears it -- it is NOT permanently skipped.
main_to_contractor_copy_log is an upsert keyed on
(source_sheet_id, source_row_id, region_code): its status just reflects
the most recent attempt, flipping to 'copied' once it finally succeeds.

The real problem this revealed wasn't cross-run retrying (once every 10
minutes is fine) -- it was retrying the SAME already-known-bad target
dozens of times WITHIN one run, back to back. That's fixed with an
in-memory "known bad target this run" set: the first SHEET_FULL_ERROR_CODE
failure against a target blacklists it for the rest of THIS run only:
every other row destined for that same target is skipped immediately
(no API call at all) instead of being individually retried and failing.
The next scheduled run starts with a clean slate and tries again.

DELTA (2026-09-14): scanning every row on every sheet every 10 minutes
doesn't scale. Two-tier check before doing real work:
  1. Sheet-level: Sheets.get_sheet_version() (cheap, no row data) -- skip
     the full fetch entirely if unchanged AND there are no rows in
     main_to_contractor_copy_log with status='error' for this sheet
     (those need re-checking regardless of whether the sheet itself
     changed, since the fix is often external -- e.g. we create the
     missing contractor sheet -- and never touches the source row).
  2. Row-level: once fetched, a row is only evaluated if its own
     modified_at is newer than our last check, OR it has a pending
     'error' entry that needs retrying.
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
from jobs.approval_router.copier import copy_row, CopierError, SHEET_FULL_ERROR_CODE
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
# sheet yet -- deliberately absent so it's caught by the "unmapped
# region" path below instead of silently matched.

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


def _get_copied_set(conn, main_sheet_id: int) -> set[tuple[int, str]]:
    """Rows already successfully copied -- permanent skip, never retried."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_row_id, region_code FROM main_to_contractor_copy_log "
            "WHERE source_sheet_id = %s AND status = 'copied'",
            (main_sheet_id,),
        )
        return set(cur.fetchall())


def _get_pending_error_set(conn, main_sheet_id: int) -> set[tuple[int, str]]:
    """Rows that failed last time and still need retrying -- forces
    evaluation even if the row itself hasn't changed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_row_id, region_code FROM main_to_contractor_copy_log "
            "WHERE source_sheet_id = %s AND status = 'error'",
            (main_sheet_id,),
        )
        return set(cur.fetchall())


def _upsert_copy_log(conn, source_sheet_id, source_row_id, region_code, target_sheet_id,
                      target_row_id, client_slug, status, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO main_to_contractor_copy_log "
            "(source_sheet_id, source_row_id, region_code, target_sheet_id, target_row_id, "
            "client_slug, status, error_message, copied_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (source_sheet_id, source_row_id, region_code) DO UPDATE SET "
            "target_sheet_id = EXCLUDED.target_sheet_id, target_row_id = EXCLUDED.target_row_id, "
            "status = EXCLUDED.status, error_message = EXCLUDED.error_message, "
            "copied_at = EXCLUDED.copied_at",
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


def _get_sheet_state(conn, main_sheet_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_version, last_checked_at FROM main_to_contractor_sheet_state WHERE main_sheet_id = %s",
            (main_sheet_id,),
        )
        return cur.fetchone()


def _update_sheet_state(conn, main_sheet_id: int, version: int):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO main_to_contractor_sheet_state (main_sheet_id, last_version, last_checked_at) "
            "VALUES (%s, %s, clock_timestamp()) "
            "ON CONFLICT (main_sheet_id) DO UPDATE SET last_version = EXCLUDED.last_version, "
            "last_checked_at = EXCLUDED.last_checked_at",
            (main_sheet_id, version),
        )
    conn.commit()


def process_sheet(conn, ss_client, client_slug: str, main_sheet_id: int,
                   known_bad_targets_this_run: set) -> dict[str, int]:
    stats = {"rows_checked": 0, "copies_made": 0, "already_copied": 0,
              "unmapped_region": 0, "errors": 0, "skipped_known_bad_target": 0}

    pending_errors = _get_pending_error_set(conn, main_sheet_id)
    already_copied = _get_copied_set(conn, main_sheet_id)

    current_version = ss_client.Sheets.get_sheet_version(main_sheet_id).version
    state = _get_sheet_state(conn, main_sheet_id)
    last_version, last_checked_at = state if state else (None, None)

    if last_version is not None and current_version == last_version and not pending_errors:
        logger.info("%s: unchanged (version %s), no pending errors — skipping", client_slug, current_version)
        return stats

    sheet = ss_client.Sheets.get_sheet(main_sheet_id)
    columns_by_id = {c.id: c.title for c in sheet.columns}
    submit_columns = _find_submit_columns(columns_by_id)

    if not submit_columns:
        _update_sheet_state(conn, main_sheet_id, current_version)
        return stats

    for row in sheet.rows:
        row_has_pending = any((row.id, region) in pending_errors for region in submit_columns.values())
        row_changed = last_checked_at is None or (row.modified_at and row.modified_at > last_checked_at)
        if not row_changed and not row_has_pending:
            continue

        stats["rows_checked"] += 1
        cells_by_col = {cell.column_id: cell for cell in row.cells}

        for col_id, region_code in submit_columns.items():
            cell = cells_by_col.get(col_id)
            if not cell or cell.value is not True:
                continue

            if (row.id, region_code) in already_copied:
                stats["already_copied"] += 1
                continue

            target_sheet_id = REGION_TO_CONTRACTOR_SHEET.get(region_code)
            if target_sheet_id is None:
                stats["unmapped_region"] += 1
                logger.warning("Unmapped region %r for row %s on %s", region_code, row.id, client_slug)
                _alert_unmapped_region(conn, client_slug, row.id, region_code)
                _upsert_copy_log(conn, main_sheet_id, row.id, region_code, None, None, client_slug,
                                  "error", f"No contractor sheet mapped for region {region_code!r}")
                continue

            if target_sheet_id in known_bad_targets_this_run:
                stats["skipped_known_bad_target"] += 1
                continue

            try:
                target_row_id = copy_row(ss_client, main_sheet_id, row.id, target_sheet_id)
                _upsert_copy_log(conn, main_sheet_id, row.id, region_code, target_sheet_id,
                                  target_row_id, client_slug, "copied")
                stats["copies_made"] += 1
                logger.info("Copied row %s (%s) on %s -> contractor sheet %s (target_row=%s)",
                            row.id, region_code, client_slug, target_sheet_id, target_row_id)
            except CopierError as exc:
                stats["errors"] += 1
                logger.error("Copy failed for row %s (%s) on %s: %s", row.id, region_code, client_slug, exc)
                _upsert_copy_log(conn, main_sheet_id, row.id, region_code, target_sheet_id, None,
                                  client_slug, "error", str(exc))
                if exc.error_code == SHEET_FULL_ERROR_CODE:
                    known_bad_targets_this_run.add(target_sheet_id)
                    logger.warning("Target sheet %s marked bad for the rest of this run", target_sheet_id)

    _update_sheet_state(conn, main_sheet_id, current_version)
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
    known_bad_targets_this_run = set()

    try:
        conn = get_connection()
        ss_client = get_smartsheet_client()
        job_run_id = _start_job_run(conn, JOB_NAME)

        for client_slug, config in MAIN_SHEETS.items():
            try:
                stats = process_sheet(conn, ss_client, client_slug, config["main_sheet_id"], known_bad_targets_this_run)
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