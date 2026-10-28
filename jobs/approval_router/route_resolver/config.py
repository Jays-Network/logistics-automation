"""Registry view: MAIN_SHEETS is BUILT from the per-client files.

The single source of truth for a client's main sheet id and invoicing folder id
is that client's own file in clients/. Nothing is duplicated here, so changing a
sheet id means editing exactly one file.

client_slug -> {"main_sheet_id": int, "invoicing_folder_id": int}
(shape unchanged, jobs/main_to_contractor.py still imports MAIN_SHEETS).
"""

from .settings import (  # noqa: F401  (re-exported for older imports)
    INVOICING_WORKSPACE_ID, JOB_NAME, LOCK_FILE, LOCK_STALE_SECONDS,
)
from .clients import CLIENTS

MAIN_SHEETS: dict[str, dict[str, int]] = {
    slug: {"main_sheet_id": module.MAIN_SHEET_ID, "invoicing_folder_id": module.INVOICING_FOLDER_ID}
    for slug, module in CLIENTS.items()
}
