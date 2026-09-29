"""
contractors_router.py — Contractors workspace sheet rotation (Phase 4.5)

Not related to approval_router (Part 1/Part 2) business logic -- this
handles a different workspace (Contractors, id 784561375340420) for a
different reason: these are long-lived, continuously-updated operational
tracking sheets (not a main->invoicing->archive pipeline), so periodically
the WHOLE sheet gets rotated out for housekeeping, rather than individual
rows moving between sheets like Part 1/Part 2.

ONE trigger only: the scheduled rotation on the 26th of every month
(02:00 cron). There is deliberately NO "Archive checkbox" trigger on the
contractor sheets -- rows on these sheets have already been archived by the
time they carry that checkbox, so it says nothing about the sheet. The only
checkbox this pipeline reads is SUBMIT on the client MAIN sheets
(main_to_contractor.py), which copies rows to the contractor's ACTIVE sheet.
A manual run outside the 26th needs an explicit --force.

FIX 2026-09-29 (incident: nightly re-rotation loop)
---------------------------------------------------
The previous version had a second "Archive checked anywhere" trigger, and
it hardcoded the contractor sheet IDs, ignoring the new sheet's ID after a
rotation. So after the first (accidental) rotation on 2026-09-17:
  * this job kept inspecting the OLD sheet (now named *_ARCHIVED_*), whose
    checked Archive rows re-triggered a rotation every night (12 blank
    copies per sheet, names chaining into "..._ARCHIVED_20_ARCHIVED_...").
  * main_to_contractor.py kept copying new rows into the old sheet.
Now:
  * The Archive trigger is gone; this job never reads sheet rows at all.
  * The ACTIVE sheet id per contractor lives in the DB
    (contractor_sheet_state, migration 018). main_to_contractor.py reads the
    same table, so both jobs always agree on which sheet is live.
  * The DB is updated IMMEDIATELY after the blank copy exists, before the
    cosmetic renames. Routing never depends on a rename succeeding. A failed
    rename leaves the rotation_log row 'pending_rename'; any later run
    (cron can stay daily -- non-26th runs only do this repair) finishes it.
  * Names come from the stored clean base_name, never the sheet's current
    name, so they can't chain or get truncated.
  * Idempotency: a sheet rotates at most once per calendar day.

Reuses _safe_sheet_name() and _rename_sheet_checked() from
archive_router.py -- generic Smartsheet utilities (name-length safety,
confirmed-rename-with-retry), not approval_router business logic.
"""

import os
import sys
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
ROTATION_DAY_OF_MONTH = 26

LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/contractors_router.lock"
JOB_NAME = "contractors_router_4_5"


class ContractorsRouterError(Exception):
    """Raised for a genuinely unrecoverable rotation failure. Caught per-
    sheet in main() and logged rather than propagated, so one sheet
    failing doesn't stop the rest."""
    pass


# --------------------------------------------------------------------------
# State (contractor_sheet_state / contractor_sheet_rotation_log)
# --------------------------------------------------------------------------

def _load_state(conn) -> list[dict]:
    """All contractors with their currently ACTIVE sheet. Empty means the
    migration wasn't applied/seeded -- the caller must fail loudly rather
    than guess IDs."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT slug, region_code, base_name, active_sheet_id, last_rotated_on "
            "FROM contractor_sheet_state ORDER BY slug"
        )
        return [dict(r) for r in cur.fetchall()]


def _finish_pending_renames(conn, ss_client) -> int:
    """Completes cosmetic renames left over from a rotation whose rename
    step failed. Routing was already switched, so this is housekeeping."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, slug, old_sheet_id, new_sheet_id, old_renamed_to, new_name "
            "FROM contractor_sheet_rotation_log WHERE status = 'pending_rename' ORDER BY id"
        )
        pending = [dict(r) for r in cur.fetchall()]

    completed = 0
    for item in pending:
        try:
            _rename_sheet_checked(ss_client, item["old_sheet_id"], item["old_renamed_to"], item["slug"])
            _rename_sheet_checked(ss_client, item["new_sheet_id"], item["new_name"], item["slug"])
            _mark_rotation_complete(conn, item["id"])
            completed += 1
            logger.info("Completed pending rename for %s (log id %s)", item["slug"], item["id"])
        except Exception as exc:
            logger.error("Pending rename still failing for %s (log id %s): %s", item["slug"], item["id"], exc)
            _set_rotation_error(conn, item["id"], str(exc))
    return completed


def _mark_rotation_complete(conn, log_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE contractor_sheet_rotation_log SET status = 'complete', error_message = NULL, "
            "completed_at = clock_timestamp() WHERE id = %s",
            (log_id,),
        )
    conn.commit()


def _set_rotation_error(conn, log_id: int, message: str):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE contractor_sheet_rotation_log SET error_message = %s WHERE id = %s",
            (message[:500], log_id),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------

def _rotate_contractor_sheet(conn, ss_client, workspace_id: int, entry: dict, reason: str,
                             archive_date: date | None = None) -> int | None:
    """
    Rotates one contractor sheet: blank copy -> switch routing in the DB ->
    rename old to *_ARCHIVED_<date> -> rename copy to the clean base name.

    Returns the new active sheet id, or None if skipped (already rotated
    today). The old sheet is kept as the archive; it is not full here, so
    it gets an "_ARCHIVED_<date>" suffix rather than "_FULL_".
    """
    slug = entry["slug"]
    base_name = entry["base_name"]
    old_sheet_id = entry["active_sheet_id"]
    today = date.today()

    if entry["last_rotated_on"] == today:
        logger.info("%s: already rotated today (%s) — skipping", slug, today)
        return None

    temp_name = _safe_sheet_name(base_name, " (new)")
    directive = smartsheet.models.ContainerDestination({
        "destination_type": "workspace",
        "destination_id": workspace_id,
        "new_name": temp_name,
    })
    copy_response = ss_client.Sheets.copy_sheet(old_sheet_id, directive, include=[])
    if isinstance(copy_response, smartsheet.models.Error):
        raise ContractorsRouterError(f"copy_sheet failed for {slug}: {copy_response.result.message}")
    new_sheet_id = copy_response.result.id

    label_date = archive_date or today
    old_renamed = _safe_sheet_name(base_name, f"_ARCHIVED_{label_date.isoformat()}")

    # Switch routing FIRST. Guarded on the old id so a concurrent change is
    # never silently overwritten.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE contractor_sheet_state SET active_sheet_id = %s, last_rotated_on = %s, "
            "updated_at = clock_timestamp() WHERE slug = %s AND active_sheet_id = %s",
            (new_sheet_id, today, slug, old_sheet_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise ContractorsRouterError(
                f"{slug}: active sheet changed under us — blank copy {new_sheet_id} is orphaned, "
                f"delete it manually"
            )
        cur.execute(
            "INSERT INTO contractor_sheet_rotation_log "
            "(slug, old_sheet_id, new_sheet_id, old_renamed_to, new_name, trigger_reason) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (slug, old_sheet_id, new_sheet_id, old_renamed, base_name, reason),
        )
        log_id = cur.fetchone()[0]
    conn.commit()

    # Cosmetic part. Old first, so the clean name is vacated before reuse.
    try:
        _rename_sheet_checked(ss_client, old_sheet_id, old_renamed, slug)
        _rename_sheet_checked(ss_client, new_sheet_id, base_name, slug)
        _mark_rotation_complete(conn, log_id)
    except Exception as exc:
        logger.error("%s: rotated (active is now %s) but rename step failed, will retry next run: %s",
                     slug, new_sheet_id, exc)
        _set_rotation_error(conn, log_id, str(exc))

    logger.info("Rotated contractor sheet %s: %s (-> %s) => new active %s (%s)",
                slug, old_sheet_id, old_renamed, new_sheet_id, base_name)
    return new_sheet_id


# --------------------------------------------------------------------------
# Job bookkeeping
# --------------------------------------------------------------------------

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


def main(force: bool = False, archive_date: date | None = None):
    """
    Entry point for cron. Rotates every contractor's ACTIVE sheet (from
    contractor_sheet_state) on the 26th of the month -- and ONLY then, unless
    force=True (--force). On any other day it just finishes pending renames.
    archive_date (--archive-date YYYY-MM-DD, only with --force) sets the date
    in the archive sheet's name; the once-per-day guard still uses today.
    A single sheet failing is logged and skipped.
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

        _finish_pending_renames(conn, ss_client)

        state = _load_state(conn)
        if not state:
            raise ContractorsRouterError(
                "contractor_sheet_state is empty — apply migration 018 before running. "
                "Refusing to guess sheet IDs."
            )

        is_scheduled_day = date.today().day == ROTATION_DAY_OF_MONTH
        if is_scheduled_day or force:
            reason = "scheduled (26th)" if is_scheduled_day else "forced (--force)"
            for entry in state:
                slug = entry["slug"]
                try:
                    logger.info("Rotating %s — trigger: %s", slug, reason)
                    if _rotate_contractor_sheet(conn, ss_client, CONTRACTORS_WORKSPACE_ID, entry, reason,
                                                archive_date=archive_date):
                        rotations_done += 1
                except ContractorsRouterError as exc:
                    logger.error("Rotation failed for %s: %s", slug, exc)
                except Exception as exc:
                    logger.error("Unexpected error processing %s: %s", slug, exc, exc_info=True)
        else:
            logger.info("Not the %sth (today is the %s) — no rotation. Pending renames checked.",
                        ROTATION_DAY_OF_MONTH, date.today().day)

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
    import argparse
    parser = argparse.ArgumentParser(description="Contractor sheet rotation (runs on the 26th; --force for a manual run)")
    parser.add_argument("--force", action="store_true", help="rotate now even if today is not the 26th")
    parser.add_argument("--archive-date", help="YYYY-MM-DD used in the archive sheet name (requires --force)")
    args = parser.parse_args()
    if args.archive_date and not args.force:
        parser.error("--archive-date only makes sense together with --force")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main(force=args.force, archive_date=date.fromisoformat(args.archive_date) if args.archive_date else None)