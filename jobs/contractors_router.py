"""
contractors_router.py — Contractors workspace sheet rotation (Phase 4.5)

Not related to approval_router (Part 1/Part 2) business logic -- this
handles a different workspace (Contractors, id 784561375340420) for a
different reason: these are long-lived, continuously-updated operational
tracking sheets (not a main->invoicing->archive pipeline), so periodically
the WHOLE sheet gets rotated out for housekeeping, rather than individual
rows moving between sheets like Part 1/Part 2.

Two independent triggers, both doing the exact same rotation operation:
    1. Scheduled: forced rotation on the 26th of every month, regardless
       of whether the sheet is anywhere near full.
    2. Manual: the "Archive" checkbox (same column name/convention as
       Part 2, confirmed live 2026-09-14 across two different sheet
       lineages) checked on ANY row in the sheet is a whole-sheet
       housekeeping signal here -- NOT a per-row move like Part 2's
       Archive checkbox. Checking it anywhere means "rotate this sheet
       now."

Reuses _safe_sheet_name() and _rename_sheet_checked() from
archive_router.py -- these are generic Smartsheet utilities (name-length
safety, confirmed-rename-with-retry), not approval_router business logic,
so importing them here doesn't violate the "Part 1/Part 2 stay fully
independent" principle -- that was specifically about routing/business
logic, not generic plumbing that's genuinely safer to share than
re-implement.

KNOWN LIMITATION, not yet addressed (time-constrained build 2026-09-14):
no idempotency guard against rotating the same sheet twice if this job
runs more than once on the 26th itself -- relies on the crontab entry
being once-daily. Worth hardening later (e.g. a state table recording
"already rotated today") if the cron cadence ever changes.
"""

import os
import time
import logging
from datetime import date

from dotenv import load_dotenv
import smartsheet
import psycopg2.extras

load_dotenv()

from jobs.approval_router.backup import get_connection
from jobs.approval_router.mover import get_smartsheet_client
from jobs.approval_router.archive_router import _safe_sheet_name, _rename_sheet_checked

logger = logging.getLogger("contractors_router")

CONTRACTORS_WORKSPACE_ID = 784561375340420
ARCHIVE_CHECKBOX_COLUMN = "Archive"
ROTATION_DAY_OF_MONTH = 26

# The 8 active per-contractor sheets living directly in the workspace root
# (confirmed live 2026-09-14) -- NOT the Contractor Reports / Old Sheets /
# test sheets folders in the same workspace, which are out of scope here.
CONTRACTOR_SHEETS: dict[str, int] = {
    "a_track_mozambique": 7071129797611396,
    "adars_botswana":     4096231923994500,
    "adars_sa":           8244727318007684,
    "spd_drc":            1413715040620420,
    "sps_tanzania":       992501116653444,
    "sps_zambia":         2879447154773892,
    "wr_namibia":         3887174390861700,
    "zimbabwe":           4599803954548612,
}


class ContractorsRouterError(Exception):
    """Raised for a genuinely unrecoverable rotation failure. Caught per-
    sheet in main() and logged rather than propagated, so one sheet
    failing doesn't stop the rest."""
    pass


def _sheet_needs_manual_rotation(ss_client, sheet_id: int) -> bool:
    """True if ANY row on this sheet has the Archive checkbox checked --
    a whole-sheet rotation signal here, not a per-row move."""
    sheet = ss_client.Sheets.get_sheet(sheet_id)
    columns_by_id = {c.id: c.title for c in sheet.columns}
    archive_col_id = next(
        (cid for cid, title in columns_by_id.items()
         if title.strip().casefold() == ARCHIVE_CHECKBOX_COLUMN.casefold()),
        None,
    )
    if archive_col_id is None:
        return False
    for row in sheet.rows:
        cells_by_col = {cell.column_id: cell for cell in row.cells}
        cell = cells_by_col.get(archive_col_id)
        if cell and cell.value is True:
            return True
    return False


def _rotate_contractor_sheet(ss_client, workspace_id: int, sheet_id: int, sheet_slug: str) -> int:
    """
    Same fundamental operation as archive_router.py's _rotate_archive_sheet
    (blank copy under a temp name, rename the old sheet out of the way,
    give the copy the clean name) -- but the old sheet isn't necessarily
    full here, so it gets an "_ARCHIVED_<date>" suffix instead of "_FULL_",
    to be semantically accurate about why the rotation happened.
    """
    sheet = ss_client.Sheets.get_sheet(sheet_id)
    original_name = sheet.name

    temp_name = _safe_sheet_name(original_name, " (new)")
    directive = smartsheet.models.ContainerDestination({
        "destination_type": "workspace",
        "destination_id": workspace_id,
        "new_name": temp_name,
    })
    copy_response = ss_client.Sheets.copy_sheet(sheet_id, directive, include=[])
    if isinstance(copy_response, smartsheet.models.Error):
        raise ContractorsRouterError(f"copy_sheet failed for {sheet_slug}: {copy_response.result.message}")
    new_sheet_id = copy_response.result.id

    timestamp = time.strftime("%Y-%m-%d")
    old_renamed = _safe_sheet_name(original_name, f"_ARCHIVED_{timestamp}")
    _rename_sheet_checked(ss_client, sheet_id, old_renamed, sheet_slug)
    _rename_sheet_checked(ss_client, new_sheet_id, original_name, sheet_slug)

    logger.info("Rotated contractor sheet %s: %s (now %s) -> new active %s",
                sheet_slug, sheet_id, old_renamed, new_sheet_id)
    return new_sheet_id


LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/contractors_router.lock"
JOB_NAME = "contractors_router_4_5"


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
    """
    Entry point for cron -- intended to run once daily. Checks each of the
    8 contractor sheets for either trigger (today is the 26th, OR the
    Archive checkbox is checked anywhere on that sheet) and rotates any
    that qualify. A single sheet failing is logged and skipped.
    """
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > 3600:
            logger.warning("Zombie lock detected (older than 60 min) — clearing it")
            os.remove(LOCK_FILE)
        else:
            logger.info("contractors_router already running — aborting to avoid overlap")
            return

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(str(time.time()))

    conn = None
    job_run_id = None
    rotations_done = 0

    try:
        conn = get_connection()
        ss_client = get_smartsheet_client()
        job_run_id = _start_job_run(conn, JOB_NAME)
        is_scheduled_day = date.today().day == ROTATION_DAY_OF_MONTH

        for sheet_slug, sheet_id in CONTRACTOR_SHEETS.items():
            try:
                manual_trigger = _sheet_needs_manual_rotation(ss_client, sheet_id)
                if is_scheduled_day or manual_trigger:
                    reason = "scheduled (26th)" if is_scheduled_day else "manual (Archive checked)"
                    logger.info("Rotating %s — trigger: %s", sheet_slug, reason)
                    _rotate_contractor_sheet(ss_client, CONTRACTORS_WORKSPACE_ID, sheet_id, sheet_slug)
                    rotations_done += 1
                else:
                    logger.info("%s: no rotation needed", sheet_slug)
            except ContractorsRouterError as exc:
                logger.error("Rotation failed for %s: %s", sheet_slug, exc)
            except Exception as exc:
                logger.error("Unexpected error processing %s: %s", sheet_slug, exc, exc_info=True)

        _finish_job_run(conn, job_run_id, status="success", rows_processed=rotations_done)
        logger.info("contractors_router run complete: %s sheets rotated", rotations_done)

    except Exception as exc:
        logger.error("contractors_router run failed: %s", exc, exc_info=True)
        if conn and job_run_id:
            _finish_job_run(conn, job_run_id, status="failed", rows_processed=rotations_done, error_message=str(exc))
    finally:
        if conn:
            conn.close()
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()