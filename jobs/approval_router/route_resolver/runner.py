"""main(): loop every client in MAIN_SHEETS under the run lock."""

import logging
import time

from jobs.approval_router.backup import get_connection
from jobs.approval_router.mover import get_smartsheet_client

from . import lock
from .config import MAIN_SHEETS
from .settings import JOB_NAME
from .db import start_job_run, finish_job_run
from .processor import process_sheet
from .util import FolderSheetCache

logger = logging.getLogger("approval_router.route_resolver")


def main():
    """
    Entry point for cron. Loops every client in MAIN_SHEETS, moving any row
    that's ROUTE-set and Approved. A single client failing entirely (e.g. a
    Smartsheet outage mid-fetch) is logged and skipped — it does NOT abort
    the rest of the run, same "one bad thing doesn't stop the batch"
    philosophy as mover.py's per-row error handling.

    Same lock-file convention as every other job in this repo: file-based,
    heartbeat-based zombie timeout (see lock.py), so an overlapping cron tick
    doesn't stack on top of a still-running pass.
    """
    if not lock.acquire():
        return

    conn = None
    job_run_id = None
    total_rows_moved = 0
    start_time = time.time()
    known_bad_targets_this_run = set()

    try:
        conn = get_connection()
        ss_client = get_smartsheet_client()
        folder_cache = FolderSheetCache(ss_client)
        job_run_id = start_job_run(conn, JOB_NAME)

        for client_slug, config in MAIN_SHEETS.items():
            try:
                stats = process_sheet(conn, ss_client, folder_cache, client_slug, config["main_sheet_id"], known_bad_targets_this_run)
                total_rows_moved += stats["rows_moved"]
                logger.info("Client %s done: %s", client_slug, stats)
            except Exception as exc:
                logger.error("Client %s failed entirely (skipping to next client): %s", client_slug, exc, exc_info=True)

        duration = round(time.time() - start_time, 1)
        finish_job_run(conn, job_run_id, status="success", rows_processed=total_rows_moved)
        logger.info("route_resolver run complete: %s rows moved across %s clients in %ss",
                     total_rows_moved, len(MAIN_SHEETS), duration)

    except Exception as exc:
        logger.error("route_resolver run failed: %s", exc, exc_info=True)
        if conn and job_run_id:
            finish_job_run(conn, job_run_id, status="failed", rows_processed=total_rows_moved, error_message=str(exc))
    finally:
        if conn:
            conn.close()
        lock.release()
