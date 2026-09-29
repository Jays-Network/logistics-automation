#!/usr/bin/env python3
"""
Move rows that landed on the OLD (ARCHIVED-named) contractor sheets after the
first rotation onto the ACTIVE sheets (incident 2026-09-29).

Background: contractor sheets rotate ONCE a month, on the 26th at 02:00.
The legitimate rotation was 2026-09-26. Because main_to_contractor.py had a
hardcoded sheet ID, rows created after that rotation kept landing on the
old (now *_ARCHIVED_*) sheet. Audit 2026-09-29 (by date, so approximate):
Botswana 1, Namibia 6, everything else 0. The count grows every day until
the fixed main_to_contractor.py is deployed, so this tool recounts live.

Rows created 09-17..09-25 are NOT stranded: they belong to the month that the
26th rotation archived, so they correctly stay on the old sheet. (The 09-17
rotations were accidental -- the removed Archive-checkbox trigger -- and are
ignored for cutoff purposes.)

A row is "stranded" if it was CREATED on the old sheet after that sheet's
26 Sep 2026 rotation (per-contractor timestamps below, taken from when each
rotation created its blank copy). Rows are MOVED (native atomic
Sheets.move_rows), in chunks, and each contractor is verified: target row
count must grow by exactly the number moved.

Run AFTER: migration 018 applied, fixed main_to_contractor.py deployed
(otherwise new rows keep landing on the old sheets), contractors_router cron
still disabled or already fixed.

Duplicate rule (Jay, 2026-09-29): two mechanisms copy the same SUBMIT rows to a
contractor sheet (the native "<REGION> P-A" automations on the main sheets AND
main_to_contractor), so the same booking can appear twice. The rule is by
INTERNAL REF: among the stranded rows, ONE row per INTERNAL REF is moved
(the most recently modified); a booking whose INTERNAL REF is already on the
active sheet is not moved again; rows with a unique INTERNAL REF (for example
the ARM# rows typed directly on the contractor sheet) are moved. The extra
copies are NOT deleted: they stay on the archive sheet.

Schema guard: rows are only moved between sheets whose columns match. A
mismatch blocks --apply unless --ignore-schema is given (don't, unless you
have looked at the diff). --check-schema (read-only) prints the comparison
between each old sheet and the CURRENT active sheet from the database.

RUN THIS AFTER the catch-up rotation (`python3 -m jobs.contractors_router
--force`): before that the active sheet IS the old sheet, so there is nothing
to move.

Usage (repo root):
    python3 -m jobs.tools.reconcile_stranded_rows --check-schema   # read-only comparison
    python3 -m jobs.tools.reconcile_stranded_rows                  # dry run, counts only
    python3 -m jobs.tools.reconcile_stranded_rows --apply          # do it
    python3 -m jobs.tools.reconcile_stranded_rows --apply --only sps_zambia
"""
import argparse
from collections import defaultdict
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()   # backup.get_connection() does NOT load .env itself (by design)

import smartsheet

from jobs.approval_router.backup import get_connection
from jobs.approval_router.mover import get_smartsheet_client

logger = logging.getLogger("reconcile_stranded_rows")

# slug -> the OLD data-bearing sheet id (audit 2026-09-29)
OLD_DATA_SHEETS = {
    "a_track_mozambique": 7071129797611396,
    "adars_botswana":     4096231923994500,
    "adars_sa":           8244727318007684,
    "spd_drc":            1413715040620420,
    "sps_tanzania":       992501116653444,
    "sps_zambia":         2879447154773892,
    "wr_namibia":         3887174390861700,
    "zimbabwe":           4599803954548612,
}

# When the legitimate 26 Sep 2026 rotation ran for each contractor (creation
# time of that night's blank copy, from the 2026-09-29 audit). Rows created on
# the old sheet AFTER this are stranded.
ROTATION_CUTOFF = {
    "a_track_mozambique": "2026-09-26T02:00:33+00:00",
    "adars_botswana":     "2026-09-26T02:01:31+00:00",
    "adars_sa":           "2026-09-26T02:02:49+00:00",
    "spd_drc":            "2026-09-26T02:03:54+00:00",
    "sps_tanzania":       "2026-09-26T02:05:07+00:00",
    "sps_zambia":         "2026-09-26T02:07:08+00:00",
    "wr_namibia":         "2026-09-26T02:08:51+00:00",
    "zimbabwe":           "2026-09-26T02:09:45+00:00",
}

CHUNK = 100
BACKUP_DIR = "backups/contractor_reconcile"


def _to_dt(value):
    if value is None:
        return None
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _row_count(ss_client, sheet_id: int) -> int:
    sheet = ss_client.Sheets.get_sheet(sheet_id, page_size=1)
    if isinstance(sheet, smartsheet.models.Error):
        raise RuntimeError(f"get_sheet failed for {sheet_id}: {sheet.result.message}")
    return sheet.total_row_count


def _schema(ss_client, sheet_id: int) -> list[tuple]:
    """(title, type, options) per column, in column order."""
    cols = ss_client.Sheets.get_columns(sheet_id, include_all=True)
    if isinstance(cols, smartsheet.models.Error):
        raise RuntimeError(f"get_columns failed for {sheet_id}: {cols.result.message}")
    return [(c.title, str(c.type), tuple(c.options or [])) for c in cols.data]


def schema_diff(ss_client, old_id: int, new_id: int) -> list[str]:
    """Empty list = identical layout. Otherwise human-readable differences."""
    old, new = _schema(ss_client, old_id), _schema(ss_client, new_id)
    if old == new:
        return []
    diffs = []
    old_by, new_by = {c[0]: c for c in old}, {c[0]: c for c in new}
    if len(old) != len(new):
        diffs.append(f"column count: old={len(old)} new={len(new)}")
    for title in old_by.keys() - new_by.keys():
        diffs.append(f"missing on new sheet: {title!r}")
    for title in new_by.keys() - old_by.keys():
        diffs.append(f"only on new sheet: {title!r}")
    for title in old_by.keys() & new_by.keys():
        if old_by[title][1] != new_by[title][1]:
            diffs.append(f"type differs for {title!r}: {old_by[title][1]} vs {new_by[title][1]}")
        elif old_by[title][2] != new_by[title][2]:
            diffs.append(f"dropdown options differ for {title!r}")
    if not diffs:
        diffs.append("same columns but different ORDER")
    return diffs


def check_schema_all(ss_client, active: dict) -> int:
    bad = 0
    for slug, old_id in OLD_DATA_SHEETS.items():
        if slug not in active or active[slug]["active_sheet_id"] == old_id:
            print(f"{slug}: active sheet is still the old sheet -- nothing to compare yet")
            continue
        diffs = schema_diff(ss_client, old_id, active[slug]["active_sheet_id"])
        if diffs:
            bad += 1
            print(f"{slug}: DIFFERS")
            for d in diffs[:15]:
                print(f"    - {d}")
        else:
            print(f"{slug}: identical")
    print("No differences found -- safe to proceed." if not bad else
          f"{bad} sheet(s) differ. STOP: the new sheet does not match the old one; tell Claude before continuing.")
    return 1 if bad else 0


def _load_active(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT slug, base_name, active_sheet_id FROM contractor_sheet_state")
        return {r[0]: {"base_name": r[1], "active_sheet_id": int(r[2])} for r in cur.fetchall()}


def _move_chunk(ss_client, source_id: int, target_id: int, row_ids: list[int]):
    directive = smartsheet.models.CopyOrMoveRowDirective({
        "row_ids": row_ids,
        "to": {"sheet_id": target_id},
    })
    try:
        from jobs.approval_router.copier import _robust_api_call
        resp = _robust_api_call(ss_client.Sheets.move_rows, source_id, directive)
    except ImportError:
        resp = ss_client.Sheets.move_rows(source_id, directive)
    if isinstance(resp, smartsheet.models.Error):
        raise RuntimeError(f"move_rows failed: {resp.result.message}")
    return resp


REF_COLUMN_TITLE = "INTERNAL REF:"


def _ref_column_id(sheet):
    for col in sheet.columns:
        if (col.title or "").strip().casefold() == REF_COLUMN_TITLE.casefold():
            return col.id
    return None


def _ref_of(row, ref_col_id):
    for cell in row.cells:
        if cell.column_id == ref_col_id:
            value = getattr(cell, "display_value", None) or cell.value
            return str(value).strip() if value not in (None, "") else None
    return None


def plan_moves(old_sheet, active_sheet, cutoff):
    """Returns (stranded, to_move, duplicates_left, already_on_active)."""
    old_ref_col = _ref_column_id(old_sheet)
    active_ref_col = _ref_column_id(active_sheet)
    if old_ref_col is None or active_ref_col is None:
        raise RuntimeError(f"column {REF_COLUMN_TITLE!r} not found on old/active sheet")
    active_refs = {r for r in (_ref_of(row, active_ref_col) for row in active_sheet.rows) if r}

    stranded = []
    skipped_no_created_at = 0
    for row in old_sheet.rows:
        created = _to_dt(getattr(row, "created_at", None))
        if created is None:
            skipped_no_created_at += 1
        elif created > cutoff:
            stranded.append(row)

    by_ref, no_ref = defaultdict(list), []
    for row in stranded:
        ref = _ref_of(row, old_ref_col)
        (by_ref[ref] if ref else no_ref).append(row)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    to_move, duplicates_left, already_on_active = [], [], []
    for ref, rows in by_ref.items():
        if ref in active_refs:
            already_on_active.extend(rows)
            continue
        rows.sort(key=lambda r: (_to_dt(getattr(r, "modified_at", None)) or epoch, r.id), reverse=True)
        to_move.append(rows[0])
        duplicates_left.extend(rows[1:])
    to_move.extend(no_ref)
    return stranded, to_move, duplicates_left, already_on_active, skipped_no_created_at


def reconcile_one(ss_client, slug: str, old_id: int, active_id: int, apply: bool,
                  ignore_schema: bool = False) -> dict:
    result = {"slug": slug, "stranded": 0, "to_move": 0, "moved": 0, "status": "ok"}

    if active_id == old_id:
        result["status"] = "skip: active sheet is still the old sheet (migration seed not applied?)"
        return result

    diffs = schema_diff(ss_client, old_id, active_id)
    if diffs and not ignore_schema:
        result["status"] = "SCHEMA DRIFT -- not moving: " + "; ".join(diffs[:5])
        return result

    cutoff = _to_dt(ROTATION_CUTOFF[slug])
    old_sheet = ss_client.Sheets.get_sheet(old_id)
    active_sheet = ss_client.Sheets.get_sheet(active_id)
    try:
        stranded, to_move, duplicates_left, already_on_active, no_ts = plan_moves(old_sheet, active_sheet, cutoff)
    except RuntimeError as exc:
        result["status"] = f"ERROR: {exc}"
        return result
    result.update({
        "stranded": len(stranded),
        "to_move": len(to_move),
        "duplicates_left_on_archive": len(duplicates_left),
        "already_on_active": len(already_on_active),
        "skipped_no_created_at": no_ts,
        "cutoff": cutoff.isoformat(),
    })

    if not to_move or not apply:
        return result

    # Backup before touching anything (all stranded rows, including those left behind)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"{slug}_{stamp}.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in stranded], f, default=str)
    result["backup"] = backup_path

    before = _row_count(ss_client, active_id)
    ids = [r.id for r in to_move]
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        _move_chunk(ss_client, old_id, active_id, chunk)
        result["moved"] += len(chunk)
        logger.info("%s: moved %s/%s rows", slug, result["moved"], len(ids))

    after = _row_count(ss_client, active_id)
    if after - before != result["moved"]:
        result["status"] = (f"CHECK: target grew by {after - before}, expected {result['moved']} "
                            f"(main_to_contractor may have added rows meanwhile) -- backup {backup_path}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually move rows (default: dry run)")
    parser.add_argument("--only", help="limit to one slug")
    parser.add_argument("--check-schema", action="store_true",
                        help="read-only: compare old sheet vs current active sheet columns")
    parser.add_argument("--ignore-schema", action="store_true", help="move even if columns differ (not advised)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    conn = get_connection()
    try:
        active = _load_active(conn)
    finally:
        conn.close()
    if not active:
        print("contractor_sheet_state is empty -- apply migration 018 first.")
        return 1

    if args.check_schema:
        return check_schema_all(get_smartsheet_client(), active)

    ss_client = get_smartsheet_client()
    print(f"{'APPLY' if args.apply else 'DRY RUN'}")
    bad = 0
    total = 0
    for slug, old_id in OLD_DATA_SHEETS.items():
        if args.only and slug != args.only:
            continue
        if slug not in active:
            print(f"{slug}: not in contractor_sheet_state, skipping")
            continue
        res = reconcile_one(ss_client, slug, old_id, active[slug]["active_sheet_id"], args.apply,
                            ignore_schema=args.ignore_schema)
        total += res["moved"] if args.apply else res.get("to_move", 0)
        if res["status"] != "ok":
            bad += 1
        print(json.dumps(res))
    print(f"Total {'moved' if args.apply else 'to move'}: {total}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())