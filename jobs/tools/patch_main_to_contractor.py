#!/usr/bin/env python3
"""
One-shot patch for jobs/main_to_contractor.py (incident 2026-09-29).

Replaces the hardcoded REGION_TO_CONTRACTOR_SHEET dict with a lazy map that
reads the ACTIVE sheet per region from contractor_sheet_state (migration
018) once per process. It fails closed: if the table is empty it raises
instead of falling back to stale IDs (stale IDs = writing into archives).

Nothing else in the file changes -- callers still use
REGION_TO_CONTRACTOR_SHEET.get(region_code) exactly as before.

Usage (from the repo root):
    python3 jobs/tools/patch_main_to_contractor.py            # dry run
    python3 jobs/tools/patch_main_to_contractor.py --apply    # writes, keeps .bak
"""
import re
import sys
import shutil

TARGET = "jobs/main_to_contractor.py"

DICT_RE = re.compile(r"REGION_TO_CONTRACTOR_SHEET: dict\[str, int\] = \{.*?\n\}\n", re.S)

REPLACEMENT = '''class _RegionSheetMap:
    """region_code -> ACTIVE contractor sheet id.

    Read from contractor_sheet_state (migration 018) once per process, i.e.
    once per cron run. contractors_router.py updates that table when it
    rotates a sheet, so both jobs always agree on which sheet is live.
    FAILS CLOSED: an empty table raises rather than guessing IDs, because a
    wrong guess means copying rows into an archived sheet (the 2026-09-29
    incident). Unmapped regions (e.g. MAL) still return None from .get().
    """

    def __init__(self):
        self._map = None

    def _ensure_loaded(self):
        if self._map is None:
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT region_code, active_sheet_id FROM contractor_sheet_state")
                    rows = cur.fetchall()
            finally:
                conn.close()
            if not rows:
                raise RuntimeError(
                    "contractor_sheet_state is empty -- apply migration 018 first; "
                    "refusing to guess contractor sheet IDs"
                )
            self._map = {str(code).strip().upper(): int(sheet_id) for code, sheet_id in rows}
        return self._map

    def get(self, region_code, default=None):
        return self._ensure_loaded().get(region_code, default)

    def __getitem__(self, region_code):
        return self._ensure_loaded()[region_code]

    def __contains__(self, region_code):
        return region_code in self._ensure_loaded()

    def items(self):
        return self._ensure_loaded().items()

    def keys(self):
        return self._ensure_loaded().keys()

    def values(self):
        return self._ensure_loaded().values()

    def __iter__(self):
        return iter(self._ensure_loaded())

    def __len__(self):
        return len(self._ensure_loaded())


REGION_TO_CONTRACTOR_SHEET = _RegionSheetMap()
'''


def main():
    apply = "--apply" in sys.argv
    with open(TARGET, encoding="utf-8") as f:
        src = f.read()

    if "class _RegionSheetMap" in src:
        print("Already patched -- nothing to do.")
        return 0

    matches = DICT_RE.findall(src)
    if len(matches) != 1:
        print(f"ABORT: expected exactly 1 REGION_TO_CONTRACTOR_SHEET dict literal, found {len(matches)}.")
        print("The file differs from what this patch was written against -- edit by hand.")
        return 1
    if "get_connection" not in src:
        print("ABORT: get_connection is not imported in this file.")
        return 1

    new_src = DICT_RE.sub(lambda m: REPLACEMENT, src, count=1)
    compile(new_src, TARGET, "exec")  # syntax check before touching the file

    print("Would replace this block:\n")
    print(matches[0])
    if not apply:
        print("Dry run only. Re-run with --apply to write.")
        return 0

    shutil.copy2(TARGET, TARGET + ".bak")
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(new_src)
    print(f"Patched {TARGET} (backup: {TARGET}.bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main())