#!/usr/bin/env python3
"""
Clean up after the 2026-09-17..09-29 nightly re-rotation loop.

Phase 1 (default): delete the throwaway blank copies -- sheets in the
Contractors workspace root named "<base>_ARCHIVED..." or "<base> (new)" that
are NOT an active sheet, NOT one of the 8 old data-bearing sheets, and have
ZERO rows (re-checked live immediately before each delete). Anything with
rows is skipped and reported. Deleted sheets go to Smartsheet's Deleted
Items, so this is recoverable.

Optional (--delete-stale-clean): also delete the 8 clean-named sheets made by
the accidental 09-17 (SPD DRC: 09-26) rotations. The 2026-09-29 schema check
showed they are STALE (missing 3-39 columns the live sheets now have), so they
must not become the active sheets. Each is deleted only if it has 0 rows and
is not the active sheet in the DB. Run this BEFORE the catch-up rotation so the
clean names are free for the fresh sheets.

Phase 2 (--rename-old, cosmetic, normally NOT needed: the catch-up rotation
already gives the old sheets a clean single-suffix name): give each old data sheet a clean single-suffix
archive name (e.g. "SPS Zambia_ARCHIVED_2026-09-26") instead of the chained
"..._ARCHIVED_20_ARCHIVED_2026-09-29". Only done when no stranded rows
remain (run reconcile_stranded_rows first).

The 'Old Sheets' folder is never touched (workspace root sheets only).

Usage (repo root):
    python3 -m jobs.tools.cleanup_rotation_copies --delete-stale-clean            # dry run
    python3 -m jobs.tools.cleanup_rotation_copies --delete-stale-clean --apply    # do it
"""
import argparse
import logging
import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()   # backup.get_connection() does NOT load .env itself (by design)

import smartsheet

from jobs.approval_router.backup import get_connection
from jobs.approval_router.mover import get_smartsheet_client
from jobs.approval_router.archive_router import _rename_sheet_checked, _safe_sheet_name

logger = logging.getLogger("cleanup_rotation_copies")

WORKSPACE_ID = 784561375340420
LOOP_STARTED = datetime(2026, 9, 17, tzinfo=timezone.utc)   # nothing older is a candidate

# The 8 stale clean-named sheets (empty; created by the accidental rotations).
STALE_CLEAN_SHEETS = {
    "a_track_mozambique": 41304668196740,
    "adars_botswana":     2855539039227780,
    "adars_sa":           8203563596730244,
    "spd_drc":            322423095512964,
    "sps_tanzania":       1302068158746500,
    "sps_zambia":         8344816481161092,
    "wr_namibia":         8907972593012612,
    "zimbabwe":           8451359050518404,
}

# slug -> (old data-bearing sheet id, archive suffix date). All carry the date of the
# legitimate monthly rotation (26 Sep 2026); the 09-17 rotations were accidental.
OLD_DATA_SHEETS = {
    "a_track_mozambique": (7071129797611396, "2026-09-26"),
    "adars_botswana":     (4096231923994500, "2026-09-26"),
    "adars_sa":           (8244727318007684, "2026-09-26"),
    "spd_drc":            (1413715040620420, "2026-09-26"),
    "sps_tanzania":       (992501116653444,  "2026-09-26"),
    "sps_zambia":         (2879447154773892, "2026-09-26"),
    "wr_namibia":         (3887174390861700, "2026-09-26"),
    "zimbabwe":           (4599803954548612, "2026-09-26"),
}


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


def _load_state(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT slug, base_name, active_sheet_id FROM contractor_sheet_state")
        return {r[0]: {"base_name": r[1], "active_sheet_id": int(r[2])} for r in cur.fetchall()}


def phase1_delete_blanks(ss_client, state: dict, apply: bool, delete_stale_clean: bool = False) -> int:
    protected = {s["active_sheet_id"] for s in state.values()}
    protected |= {sid for sid, _ in OLD_DATA_SHEETS.values()}
    bases = [s["base_name"] for s in state.values()]
    pattern = re.compile(
        r"^(?:%s)(?:_ARCHIVED.*| \(new\))$" % "|".join(re.escape(b) for b in bases)
    )

    workspace = ss_client.Workspaces.get_workspace(WORKSPACE_ID, load_all=True)
    if isinstance(workspace, smartsheet.models.Error):
        raise RuntimeError(f"get_workspace failed: {workspace.result.message}")
    deleted = skipped = 0
    for sheet in workspace.sheets:          # workspace-root sheets only, not folders
        if sheet.id in protected or not pattern.match(sheet.name or ""):
            continue
        created = _to_dt(getattr(sheet, "created_at", None))
        if created is None or created < LOOP_STARTED:
            print(f"SKIP (predates incident): {sheet.id} {sheet.name}")
            skipped += 1
            continue
        info = ss_client.Sheets.get_sheet(sheet.id, page_size=1)
        if isinstance(info, smartsheet.models.Error):
            print(f"SKIP (could not read): {sheet.id} {sheet.name}: {info.result.message}")
            skipped += 1
            continue
        rows = info.total_row_count
        if rows != 0:
            print(f"SKIP (HAS {rows} ROWS): {sheet.id} {sheet.name}")
            skipped += 1
            continue
        if apply:
            # errors_as_exceptions is False in this codebase: a failed delete
            # RETURNS an Error object, it does not raise.
            resp = ss_client.Sheets.delete_sheet(sheet.id)
            if isinstance(resp, smartsheet.models.Error):
                print(f"FAILED to delete {sheet.id} {sheet.name}: {resp.result.message}")
                skipped += 1
                continue
        print(f"{'DELETED' if apply else 'would delete'}: {sheet.id} {sheet.name}")
        deleted += 1
    if delete_stale_clean:
        active_ids = {s["active_sheet_id"] for s in state.values()}
        for slug, sid in STALE_CLEAN_SHEETS.items():
            if sid in active_ids or sid in protected:
                print(f"SKIP (is an active/protected sheet): {sid} {slug}")
                skipped += 1
                continue
            info = ss_client.Sheets.get_sheet(sid, page_size=1)
            if isinstance(info, smartsheet.models.Error):
                print(f"SKIP (could not read, already gone?): {sid} {slug}: {info.result.message}")
                skipped += 1
                continue
            if info.total_row_count != 0:
                print(f"SKIP (HAS {info.total_row_count} ROWS): {sid} {info.name}")
                skipped += 1
                continue
            if apply:
                resp = ss_client.Sheets.delete_sheet(sid)
                if isinstance(resp, smartsheet.models.Error):
                    print(f"FAILED to delete {sid} {info.name}: {resp.result.message}")
                    skipped += 1
                    continue
            print(f"{'DELETED' if apply else 'would delete'} (stale clean sheet): {sid} {info.name}")
            deleted += 1
    print(f"Phase 1: {deleted} {'deleted' if apply else 'deletable'}, {skipped} skipped")
    return deleted


def phase2_rename_old(ss_client, state: dict, apply: bool):
    for slug, (old_id, suffix_date) in OLD_DATA_SHEETS.items():
        if slug not in state:
            continue
        if state[slug]["active_sheet_id"] == old_id:
            print(f"{slug}: old sheet is still active, not renaming")
            continue
        target = _safe_sheet_name(state[slug]["base_name"], f"_ARCHIVED_{suffix_date}")
        current = ss_client.Sheets.get_sheet(old_id, page_size=1).name
        if current == target:
            print(f"{slug}: already {target!r}")
            continue
        print(f"{slug}: {current!r} -> {target!r}" + ("" if apply else "  (dry run)"))
        if apply:
            _rename_sheet_checked(ss_client, old_id, target, slug)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rename-old", action="store_true")
    parser.add_argument("--delete-stale-clean", action="store_true",
                        help="also delete the 8 stale clean-named sheets (only if empty and not active)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    conn = get_connection()
    try:
        state = _load_state(conn)
    finally:
        conn.close()
    if not state:
        print("contractor_sheet_state is empty -- apply migration 018 first.")
        return 1

    ss_client = get_smartsheet_client()
    print("APPLY" if args.apply else "DRY RUN")
    phase1_delete_blanks(ss_client, state, args.apply, args.delete_stale_clean)
    if args.rename_old:
        phase2_rename_old(ss_client, state, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())