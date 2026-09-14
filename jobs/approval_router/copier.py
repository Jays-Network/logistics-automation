"""
copier.py — approval_router

Copies a row to another sheet without removing it from the source.
Deliberately its OWN file, separate from mover.py -- copying and moving
are different operations with different risk profiles (a copy never puts
source data at risk, so it doesn't need mover.py's backup-first safety
net), and mixing them in one file breaks this codebase's organization
principle: one file, one clear responsibility (confirmed explicitly by
Jay, 2026-09-14).

Self-contained rather than importing mover.py's private (_-prefixed)
helpers -- get_smartsheet_client() is imported since it's pure connection
plumbing, not move/copy business logic, but the retry and response-
parsing logic below is its own independent copy so this file never
depends on mover.py's internals changing.
"""

import time
import logging

import smartsheet

logger = logging.getLogger("approval_router.copier")


class CopierError(Exception):
    """Raised for a Smartsheet-side failure during a copy. Never involves
    data loss risk the way a failed move can -- the source row is always
    untouched regardless of outcome."""
    def __init__(self, message, error_code=None):
        super().__init__(message)
        self.error_code = error_code


def _robust_api_call(func, *args, **kwargs):
    """Same retry logic as mover.py's _robust_api_call (kept as an
    independent copy here, not shared, per this file's own docstring):
    retries only when Smartsheet's own response says shouldRetry is true
    (rate limiting gets the special 60s cooldown), fails fast on anything
    deterministic (validation errors, a full target sheet, etc.)."""
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
                    raise CopierError(f"Smartsheet API error (code={code}): {msg}", error_code=code)

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
        except CopierError:
            raise
        except Exception as exc:
            attempts += 1
            if attempts < 5:
                wait = 10 * attempts
                logger.warning("Network/API exception: %s — retrying in %ss (%s/5)", exc, wait, attempts)
                time.sleep(wait)
                continue
            raise CopierError(f"Smartsheet call failed after 5 attempts: {exc}") from exc
    raise CopierError("Smartsheet call exhausted retries")


def _extract_target_row_id(response):
    """Same defensive response parsing as mover.py's version (independent
    copy, not shared) -- we only ever copy one row per call, so the
    response should contain exactly one row_mapping."""
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


def copy_row(ss_client, source_sheet_id: int, source_row_id: int, target_sheet_id: int) -> int:
    """
    Copies a single row to another sheet via Smartsheet's native
    copy_rows() -- the row stays on the source. No backup step: a copy
    never puts the source data at risk, so mover.py's backup-before-move
    rationale doesn't apply here.

    Returns the new row's id on the target sheet. Raises CopierError on
    failure, including a full target sheet (Smartsheet's errorCode 5636,
    same one mover.py's SHEET_FULL_ERROR_CODE tracks -- callers here can
    check exc.error_code == 5636 the same way).
    """
    directive = smartsheet.models.CopyOrMoveRowDirective({
        "row_ids": [source_row_id],
        "to": {"sheet_id": target_sheet_id},
    })
    response = _robust_api_call(ss_client.Sheets.copy_rows, source_sheet_id, directive)
    target_row_id = _extract_target_row_id(response)
    if target_row_id is None:
        raise CopierError("copy_rows() succeeded but no row_mapping was found in the response")
    return target_row_id