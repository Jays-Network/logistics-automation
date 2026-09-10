"""
backup.py — approval_router

Snapshots a Smartsheet row into approval_router_row_backup BEFORE anything
else touches it. This is the insurance policy for the whole move pipeline:
if the process dies at any point after this succeeds, the row's data is
safe and reconstructable regardless of what state the source/target sheets
are in.

Connection: goes through license_manager.py's Gatekeeper handshake, same
as every other job in jobs/ (Sync_ADARS.py, Sync_WorldRisk.py, etc.) —
NOT a direct psycopg2 connection with static credentials. approval_router
is secondary tier under the critical/secondary split (2026-09-10): if the
license is revoked, this job silently stalls rather than crashing, which
is the whole point of routing it through the handshake instead of
DB_DIRECT_* env vars. Only Grafana and the frontend get the static,
always-on critical-tier connection.
"""

import os
import json
import logging
from typing import Any

import psycopg2
import psycopg2.extras

from shared.license_manager import JaysNetworkLicense

logger = logging.getLogger("approval_router.backup")


class BackupError(Exception):
    """Raised when a row backup fails to write. Callers MUST treat this as
    fatal for that row — never proceed to copy/move without a confirmed
    backup_id."""
    pass


def get_connection():
    """
    Authenticates with the Gatekeeper via JaysNetworkLicense, then opens a
    psycopg2 connection using the credentials it returns. Reads
    LICENSE_KEY/JAYS_NETWORK_LICENSE_KEY and GATEKEEPER_SECRET from the
    environment — populated from .env via python-dotenv at the process
    entrypoint (run_watcher.py), not loaded here, so this module stays
    testable without a real .env present.

    Raises BackupError if the license key is missing or the handshake
    fails — this is deliberately fatal rather than falling back to a
    direct connection, since a silent fallback would defeat the whole
    point of the secondary-tier design.
    """
    license_key = os.getenv("LICENSE_KEY") or os.getenv("JAYS_NETWORK_LICENSE_KEY")
    if not license_key:
        raise BackupError("LICENSE_KEY / JAYS_NETWORK_LICENSE_KEY missing in .env")

    auth_system = JaysNetworkLicense(license_key)
    credentials = auth_system.authenticate()
    if not credentials:
        raise BackupError("Gatekeeper license validation failed — cannot open DB connection")

    return psycopg2.connect(
        host=credentials["url"],
        user=credentials["key"],
        password=credentials["pass"],
        database="jaysnet_data",
        port=5432,
        connect_timeout=10,
    )


def row_to_dict(row, columns_by_id: dict[int, str]) -> dict[str, Any]:
    """
    Flattens a smartsheet-python-sdk Row object into {column_name: value},
    keyed by actual column NAME (not ID) — this is what mover.py should
    call. columns_by_id comes from sheet.columns (id -> title), fetched
    once per sheet and passed in rather than re-fetched per row.

    Uses display_value where available (what a human sees in the sheet UI)
    falling back to the raw value — same precedence check_border_overdue.py
    and police_bot_db.py already use elsewhere in this codebase, kept
    consistent here rather than inventing a different convention.
    """
    out: dict[str, Any] = {}
    for cell in row.cells:
        col_name = columns_by_id.get(cell.column_id, str(cell.column_id))
        value = cell.display_value if cell.display_value is not None else cell.value
        # dates/datetimes from the SDK aren't JSON-serializable directly
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        out[col_name] = value
    return out


def backup_row(
    conn,
    source_sheet_id: int,
    source_row_id: int,
    client_slug: str,
    row_data: dict[str, Any],
) -> int:
    """
    Inserts one backup row. Returns the new backup id.

    Does NOT commit — caller controls the transaction boundary, so this can
    be composed with the row_move_log insert that follows it in mover.py
    inside a single transaction if that's ever wanted. For the current
    design (backup commits independently, is never rolled back even if the
    later move fails) call conn.commit() right after this returns.

    Raises BackupError on any failure — never returns a falsy/None id.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approval_router_row_backup
                    (source_sheet_id, source_row_id, client_slug, row_data)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (source_sheet_id, source_row_id, client_slug, json.dumps(row_data)),
            )
            backup_id = cur.fetchone()[0]
            logger.info(
                "Backed up row %s from sheet %s (client=%s) -> backup_id=%s",
                source_row_id, source_sheet_id, client_slug, backup_id,
            )
            return backup_id
    except Exception as exc:
        logger.error(
            "BACKUP FAILED for row %s on sheet %s (client=%s): %s",
            source_row_id, source_sheet_id, client_slug, exc,
        )
        raise BackupError(
            f"Could not back up row {source_row_id} on sheet {source_sheet_id}: {exc}"
        ) from exc


def get_backup(conn, backup_id: int) -> dict[str, Any] | None:
    """
    Fetches a backup row back out — used by mover.py's post-copy
    verification step (diff the copied target row against this) and by
    scripts/reconcile_backups.py for auditing anything stuck mid-move.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, source_sheet_id, source_row_id, client_slug, "
            "row_data, backed_up_at FROM approval_router_row_backup WHERE id = %s",
            (backup_id,),
        )
        return cur.fetchone()