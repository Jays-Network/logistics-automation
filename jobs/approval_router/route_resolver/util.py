"""Small pure helpers shared by the resolver and the client modules."""

import re

def norm(value: str) -> str:
    """Casefold + whitespace-collapse for tolerant matching — handles the
    casing and stray-space inconsistencies confirmed live across real ROUTE
    data (trailing spaces, "Bridge-CCS-serenje" vs "BRIDGE-CCS-SERENJE ",
    etc.)."""
    return " ".join(value.split()).casefold()


# Matches "<WORD> REGION: Approval" (case-insensitive, e.g. "DRC REGION:
# Approval", "ZAM REGION: Approval", "BOTS REGION: Approval") -- the
# per-leg approval columns now present on Bridge, GSM and Gold Vale's main
# sheets. Excluded from find_approval_column() below; see that function's
# docstring for why.
_REGION_APPROVAL_TITLE_RE = re.compile(r"^[a-z]+ region:\s*approval$")


def find_approval_column(columns_by_id: dict[int, str]) -> int | None:
    """Finds the "<Manager Name> approval" column by suffix match — the
    manager's name varies per client/sheet, only the " approval" suffix is
    stable. Confirmed with Jay 2026-09-13: the separate "Load approval
    status" column must NOT be used, even though it looks similar.

    FIXED 2026-09-16: also excludes the per-region "<COUNTRY> REGION:
    Approval" columns now on Bridge, GSM and Gold Vale's main sheets.
    Without this exclusion, whichever region-approval column happens to
    sit at the lowest column index gets returned as THIS row's overall
    gating approval column instead of the real final/manager approval
    column. Confirmed this was silently wrong for Bridge specifically:
    DRC REGION: Approval sits at column index 31 there, well before
    Siphemandla Hleza delivery approval at index 116 -- so before this
    fix, process_sheet()'s generic approval gate was checking DRC REGION:
    Approval for every Bridge row, not the real final-delivery column.
    GSM and Gold Vale happened to be unaffected by this specific symptom
    only because their 3 new approval columns were appended at the END of
    each sheet (after the pre-existing final approval column) -- but that
    was luck of column order, not something to rely on, hence this fix
    plus moving the per-client multi-leg/country-leg check to run BEFORE
    this generic gate in process_sheet() (see that function). Bridge is
    fully diverted before this function is even reached as of 2026-09-23,
    but the exclusion regex stays -- GSM/Gold Vale still rely on it."""
    for col_id, title in columns_by_id.items():
        normalized = title.strip().casefold()
        if normalized == "load approval status":
            continue
        if _REGION_APPROVAL_TITLE_RE.match(normalized):
            continue
        if normalized.endswith("approval"):
            return col_id
    return None


def find_column_id(columns_by_id: dict[int, str], title: str) -> int | None:
    target = title.strip().casefold()
    for col_id, col_title in columns_by_id.items():
        if col_title.strip().casefold() == target:
            return col_id
    return None


class FolderSheetCache:
    """Caches folder_id -> {normalized_sheet_name: sheet_id} for the
    lifetime of one run, so each client folder's sheet list is only fetched
    from Smartsheet once even though many rows may resolve against it."""

    def __init__(self, ss_client):
        self._ss_client = ss_client
        self._cache: dict[int, dict[str, int]] = {}

    def sheets_in_folder(self, folder_id: int) -> dict[str, int]:
        if folder_id not in self._cache:
            folder = self._ss_client.Folders.get_folder(folder_id)
            self._cache[folder_id] = {
                norm(sheet.name): sheet.id for sheet in (folder.sheets or [])
            }
        return self._cache[folder_id]


def client_logger(client_slug: str):
    """One logger per client: 'approval_router.route_resolver.<slug>'.

    Every client file logs through its own logger, so a line in the log names
    the client (and therefore the file clients/<slug>.py) that produced it. It is
    a child of 'approval_router.route_resolver', so existing handlers/format apply."""
    import logging
    return logging.getLogger(f"approval_router.route_resolver.{client_slug}")
