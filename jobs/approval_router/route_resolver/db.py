"""Postgres bookkeeping: unresolved-route log, per-sheet delta state, job run log."""

def get_unresolved_rows(conn, main_sheet_id: int) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_row_id FROM approval_router_unresolved_route_log WHERE source_sheet_id = %s",
            (main_sheet_id,),
        )
        return {r[0] for r in cur.fetchall()}


def upsert_unresolved(conn, main_sheet_id: int, source_row_id: int, client_slug: str,
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


def clear_unresolved(conn, main_sheet_id: int, source_row_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM approval_router_unresolved_route_log WHERE source_sheet_id = %s AND source_row_id = %s",
            (main_sheet_id, source_row_id),
        )
    conn.commit()


def get_sheet_delta_state(conn, sheet_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_version, last_checkpoint_at FROM approval_router_sync_sheet_state WHERE sheet_id = %s",
            (sheet_id,),
        )
        return cur.fetchone()


def update_sheet_delta_state(conn, sheet_id: int, client_slug: str, sheet_name: str, version: int):
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


def start_job_run(conn, job_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_job_run_log (job_name, status, started_at) "
            "VALUES (%s, 'running', clock_timestamp()) RETURNING id",
            (job_name,),
        )
        job_run_id = cur.fetchone()[0]
    conn.commit()
    return job_run_id


def finish_job_run(conn, job_run_id: int, status: str, rows_processed: int, error_message: str | None = None):
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
