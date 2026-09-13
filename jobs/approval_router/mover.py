"""
mover.py — approval_router

Moves a row from a source sheet to a target sheet using Smartsheet's
native atomic move endpoint (POST /sheets/{sheetId}/rows/move, exposed by
the SDK as Sheets.move_rows()). Always preceded by a Postgres snapshot via
backup.py.backup_row() — if the move itself fails, the row data is already
safe and nothing is lost; if the move succeeds, a post-move verification
step confirms the copied data matches what was backed up before the
row_move_log entry is marked 'verified'.

Why move_rows() instead of a manual add_rows + delete_rows sequence:
move_rows() is a single atomic server-side operation — Smartsheet either
completes the copy-and-remove-from-source as one unit or it doesn't happen
at all. A manual two-step copy-then-delete has a real gap: if the process
dies between the two calls, the row ends up duplicated in both sheets with
nothing recording that it happened. Confirmed against current Smartsheet
API docs (checked 2026-09-10) that /rows/move remains the documented,
current way to do this.

Connection: caller supplies an already-open conn (see backup.py's
get_connection(), which goes through license_manager.py's Gatekeeper
handshake — approval_router is secondary tier). This module doesn't open
its own DB connection.

State machine (approval_router_row_move_log.status):
    backed_up -> copied -> verified   (success path)
    backed_up -> error                (move_rows() itself failed)
    copied    -> error                (post-move verification found a mismatch)

Note: the 'deleted' status in the DB CHECK constraint predates this
atomic-move design (it assumed a manual delete step existed as a distinct
action). Since move_rows() deletes from the source as part of the same
atomic call that copies it, there's no separate deletion step to record —
'verified' is the terminal success state here. 'deleted' is left in the
schema rather than migrated out, since it doesn't block anything.
"""

import os
import time
import logging
from typing import Any

import smartsheet
import psycopg2.extras

from jobs.approval_router.backup import backup_row, row_to_dict, BackupError

logger = logging.getLogger("approval_router.mover")

# Columns expected to legitimately differ between source and target sheets
# (system/auto-generated) — a mismatch here shouldn't fail verification.
DEFAULT_IGNORE_COLUMNS = frozenset({
    "Created", "Created By", "Modified", "Modified By",
})

# Confirmed live 2026-09-10 via a copy_rows() probe against a genuinely
# full sheet (8289012302172036, 4310 rows / ~116 columns ≈ 500k cells):
# Smartsheet returns this exact errorCode when a sheet is at its 500,000-
# cell capacity. route_resolver.py's Part 2 (Invoicing -> Archive) logic
# matches MoverError.error_code against this constant to trigger the
# blank-copy-and-rotate flow, rather than parsing error_message text.
SHEET_FULL_ERROR_CODE = 5636


class MoverError(Exception):
    """Raised for a Smartsheet-side failure during the move/verify steps.
    By the time this can be raised, backup_row() has already succeeded —
    see move_row()'s docstring: this is caught internally and recorded as
    status='error' in row_move_log rather than propagated, so one bad row
    doesn't stop a batch.

    error_code carries Smartsheet's own numeric errorCode when this
    originated from a decoded API error response (e.g. 5636 = cell-limit
    exceeded, confirmed live 2026-09-10) — None when it originated from a
    raw exception (network/timeout) instead. route_resolver.py's Part 2
    archive-rotation logic matches on this field to detect a full sheet,
    rather than parsing error_message text."""
    def __init__(self, message, error_code=None):
        super().__init__(message)
        self.error_code = error_code


def get_smartsheet_client():
    """Same env var SMARTSHEET_ACCESS_TOKEN every other job in this repo
    reads (Sync_ADARS.py, Sync_WorldRisk.py, etc.)."""
    client = smartsheet.Smartsheet(os.environ["SMARTSHEET_ACCESS_TOKEN"])
    client.errors_as_exceptions = False
    return client


def _robust_api_call(func, *args, **kwargs):
    """Same retry/backoff shape as robust_api_call() in the legacy scripts
    (Sync_MasterRotation.py etc.) — kept local rather than imported since
    no shared Smartsheet wrapper module exists yet (deliberate — see
    backup.py discussion 2026-09-10: extract to shared/sheet_client.py
    once a second module needs the same calls, not before).

    Whether to retry a decoded Smartsheet API error is read from the
    response's own `shouldRetry` field rather than a hardcoded list of
    "retryable" error codes — confirmed live 2026-09-10 that Smartsheet
    includes this field on every error response (e.g. errorCode 1136
    "cannot move within the same sheet" and 5636 "cell limit exceeded"
    both came back with shouldRetry: false). This is more robust than
    guessing which codes are worth retrying, since it stays correct for
    error codes we haven't personally seen yet. Only errorCode 4003 (rate
    limiting) gets the special 60s cooldown; any other retryable error
    gets the standard 10*attempts backoff."""
    attempts = 0
    while attempts < 5:
        try:
            response = func(*args, **kwargs)
            if isinstance(response, smartsheet.models.Error):
                code = getattr(response.result, "code", None)
                msg = getattr(response.result, "message", "Unknown")
                should_retry = getattr(response.result, "should_retry", None)
                if should_retry is None:
                    to_dict_fn = getattr(response.result, "to_dict", None)
                    should_retry = to_dict_fn().get("shouldRetry", False) if callable(to_dict_fn) else False

                if not should_retry:
                    raise MoverError(f"Smartsheet API error (code={code}): {msg}", error_code=code)

                attempts += 1
                if code == 4003:
                    logger.warning("Rate limit hit, cooling down 60s...")
                    time.sleep(60)
                else:
                    wait = 10 * attempts
                    logger.warning("Retryable Smartsheet API error (code=%s): %s — retrying in %ss (%s/5)",
                                   code, msg, wait, attempts)
                    time.sleep(wait)
                continue
            return response
        except MoverError:
            raise
        except Exception as exc:
            attempts += 1
            if attempts < 5:
                wait = 10 * attempts
                logger.warning("Network/API exception: %s — retrying in %ss (%s/5)", exc, wait, attempts)
                time.sleep(wait)
                continue
            raise MoverError(f"Smartsheet call failed after 5 attempts: {exc}") from exc
    raise MoverError("Smartsheet call exhausted retries")


def _insert_move_log(conn, backup_id, source_sheet_id, source_row_id,
                      target_sheet_id, client_slug, route_value, mine_value):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO approval_router_row_move_log
                (backup_id, source_sheet_id, source_row_id, target_sheet_id,
                 client_slug, route_value, mine_value, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'backed_up')
            RETURNING id
            """,
            (backup_id, source_sheet_id, source_row_id, target_sheet_id,
             client_slug, route_value, mine_value),
        )
        return cur.fetchone()[0]


def _update_move_log(conn, move_log_id, **fields):
    """fields may include: status, target_row_id, error_message. Always
    bumps updated_at. Column names come only from this module's own
    hardcoded call sites below, never from external input, so building
    the SET clause from fields.keys() is safe here."""
    set_clauses = [f"{k} = %s" for k in fields]
    set_clauses.append("updated_at = now()")
    values = list(fields.values()) + [move_log_id]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE approval_router_row_move_log SET {', '.join(set_clauses)} WHERE id = %s",
            values,
        )


def get_move_log(conn, move_log_id: int) -> dict[str, Any] | None:
    """Fetches a move_log row back out — used by the smoke test below and
    by scripts/reconcile_backups.py (planned) for auditing stuck/errored
    moves."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM approval_router_row_move_log WHERE id = %s",
            (move_log_id,),
        )
        return cur.fetchone()


def _values_equivalent(original_value: Any, moved_value: Any) -> bool:
    """
    True if two cell values should be treated as the same for verification
    purposes. Handles one confirmed real-world quirk: a blank CHECKBOX cell
    reads back as None via row_to_dict() on the source sheet, but the same
    logical "unchecked" state reads back as an explicit False once it lands
    on the target sheet after a move. Confirmed live 2026-09-13 across
    multiple real moves (Glencore International, Goldvale) — this is not a
    real data-loss mismatch, just how Smartsheet represents an empty
    checkbox on read vs. write. Every other type of value still needs an
    exact match.
    """
    if original_value == moved_value:
        return True
    if (original_value is None and moved_value is False) or (original_value is False and moved_value is None):
        return True
    return False


def _diff_row_data(original: dict[str, Any], moved: dict[str, Any],
                    target_column_titles: set[str],
                    ignore_columns=DEFAULT_IGNORE_COLUMNS) -> tuple[list[tuple[str, Any, Any]], int]:
    """Compares the pre-move backup snapshot against what actually landed
    in the target sheet. Returns (mismatches, skipped_count) where
    mismatches is a list of (column_name, original_value, moved_value)
    tuples — an empty list means verified clean.

    Only checks columns that exist on BOTH sheets. A source column that
    the target sheet simply doesn't have (common — e.g. an operational
    tracking column with no reason to exist on an invoicing sheet) is
    structural, not data loss, and is skipped rather than counted as a
    mismatch. Confirmed via the 2026-09-10 smoke test: comparing against
    every source column produced false-positive "mismatches" for every
    column absent from the target sheet's structure, all showing
    'got None' — that was this function reading a missing key as a lost
    value, not the move actually losing anything."""
    mismatches = []
    skipped = 0
    for col_name, original_value in original.items():
        if col_name in ignore_columns:
            continue
        if col_name not in target_column_titles:
            skipped += 1
            continue
        moved_value = moved.get(col_name)
        if not _values_equivalent(original_value, moved_value):
            mismatches.append((col_name, original_value, moved_value))
    return mismatches, skipped


def _extract_target_row_id(response):
    """
    Pulls the destination row id out of a move_rows() response. We only
    ever move one row per call in this module, so the response should
    contain exactly one row_mapping — this reads its 'to' id defensively,
    since 'from' is a reserved word in Python and different SDK versions
    have exposed RowMapping's fields under slightly different attribute
    names (to_ / to_row_id / to_id). Falls back to to_dict()/as_dict() if
    none of those attributes are present, rather than assuming one
    specific SDK version's naming without ever having run this against
    the box's actual installed version yet.
    """
    mappings = getattr(getattr(response, "result", response), "row_mappings", None)
    if not mappings:
        return None
    mapping = mappings[0]
    for attr in ("to_", "to_row_id", "to_id", "to"):
        value = getattr(mapping, attr, None)
        if value is not None:
            return value
    for dict_method in ("to_dict", "as_dict"):
        to_dict_fn = getattr(mapping, dict_method, None)
        if callable(to_dict_fn):
            as_dict = to_dict_fn()
            return as_dict.get("to") or as_dict.get("toId")
    return None


def move_row(
    conn,
    ss_client,
    source_sheet_id: int,
    source_row,  # smartsheet.models.Row, already fetched by the caller
    columns_by_id: dict[int, str],  # source sheet's column id -> title
    target_sheet_id: int,
    client_slug: str,
    route_value: str | None = None,
    mine_value: str | None = None,
) -> int:
    """
    Full backup -> move -> verify orchestration for one row. Returns the
    row_move_log id.

    Never raises for a failure AFTER the backup succeeds — those are
    recorded as status='error' in row_move_log with the failure reason in
    error_message, so the caller (route_resolver.py's watcher loop, once
    it exists) can keep processing other rows and a human/Telegram alert
    can pick up the error row later. Only propagates BackupError if the
    backup itself fails — that's the one failure mode where nothing is
    safe to proceed past, since without a confirmed backup a failed move
    could mean real data loss.
    """
    source_row_id = source_row.id
    row_data = row_to_dict(source_row, columns_by_id)

    # Step 1 — backup. If this raises, propagate — never attempt a move
    # without a confirmed backup_id.
    backup_id = backup_row(conn, source_sheet_id, source_row_id, client_slug, row_data)
    conn.commit()

    move_log_id = _insert_move_log(
        conn, backup_id, source_sheet_id, source_row_id, target_sheet_id,
        client_slug, route_value, mine_value,
    )
    conn.commit()

    # Step 2 — move (atomic copy + delete-from-source via Smartsheet's
    # native endpoint).
    try:
        directive = smartsheet.models.CopyOrMoveRowDirective({
            "row_ids": [source_row_id],
            "to": {"sheet_id": target_sheet_id},
        })
        response = _robust_api_call(ss_client.Sheets.move_rows, source_sheet_id, directive)
    except MoverError as exc:
        _update_move_log(conn, move_log_id, status="error", error_message=str(exc), error_code=exc.error_code)
        conn.commit()
        logger.error("MOVE FAILED for row %s (backup_id=%s, move_log_id=%s): %s",
                     source_row_id, backup_id, move_log_id, exc)
        return move_log_id

    target_row_id = _extract_target_row_id(response)
    if target_row_id is None:
        _update_move_log(conn, move_log_id, status="error",
                          error_message="move_rows() succeeded but no row_mapping was found in the response")
        conn.commit()
        logger.error("MOVE AMBIGUOUS for row %s (backup_id=%s, move_log_id=%s): no row_mapping in response",
                      source_row_id, backup_id, move_log_id)
        return move_log_id

    _update_move_log(conn, move_log_id, status="copied", target_row_id=target_row_id)
    conn.commit()

    # Step 3 — verify: fetch the row at its new location and diff against
    # the backup snapshot.
    try:
        target_columns = _robust_api_call(ss_client.Sheets.get_columns, target_sheet_id)
        target_columns_by_id = {c.id: c.title for c in target_columns.data}
        moved_row = _robust_api_call(ss_client.Sheets.get_row, target_sheet_id, target_row_id)
        moved_row_data = row_to_dict(moved_row, target_columns_by_id)
    except MoverError as exc:
        _update_move_log(conn, move_log_id, status="error",
                          error_message=f"Verification fetch failed: {exc}", error_code=exc.error_code)
        conn.commit()
        logger.error("VERIFY FETCH FAILED for row %s -> target_row %s (move_log_id=%s): %s",
                      source_row_id, target_row_id, move_log_id, exc)
        return move_log_id

    mismatches, skipped = _diff_row_data(row_data, moved_row_data, set(target_columns_by_id.values()))
    if skipped:
        logger.info("Verification skipped %s column(s) not present on target sheet %s (row %s)",
                     skipped, target_sheet_id, source_row_id)
    if mismatches:
        error_message = "Verification mismatch: " + "; ".join(
            f"{col}: expected {orig!r}, got {moved!r}" for col, orig, moved in mismatches
        )
        _update_move_log(conn, move_log_id, status="error", error_message=error_message)
        conn.commit()
        logger.error("VERIFY MISMATCH for row %s -> target_row %s (move_log_id=%s): %s",
                      source_row_id, target_row_id, move_log_id, error_message)
        return move_log_id

    _update_move_log(conn, move_log_id, status="verified")
    conn.commit()
    logger.info("Move verified: row %s (sheet %s) -> row %s (sheet %s), move_log_id=%s",
                source_row_id, source_sheet_id, target_row_id, target_sheet_id, move_log_id)
    return move_log_id