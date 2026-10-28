"""ROUTE value -> target invoicing sheet_id.

Matching algorithm (see route-resolver-logic.md for the live audit):
  1. known override table (Glencore International, SLS Africa, ...)
  2. client special cases: RELOAD (LOCAL ROUTE), IXM (COMMODITY)
  3. first '-' prefix -> invoicing folder -> whole raw ROUTE matched against
     real sheet names (case/whitespace-insensitive)
  4. no match -> (None, reason): skipped + logged, never guessed.

Bridge, GSM and Gold Vale never reach this module (diverted in processor.py).
"""

from .clients import glencore_international, ixm, reload_, sls_africa
from .config import MAIN_SHEETS
from .util import norm, FolderSheetCache

# Per-client ROUTE -> sheet_id overrides, merged. Kept as a real mechanism in
# case another many-to-one merge turns up (Bridge no longer contributes any
# entries since the 2026-09-23 country-sheet rebuild).
KNOWN_ROUTE_OVERRIDES: dict[str, int] = {}
for _client in (glencore_international, sls_africa):
    KNOWN_ROUTE_OVERRIDES.update(_client.ROUTE_OVERRIDES)


def resolve_target_sheet_id(
    route_value: str,
    local_route_value: str | None,
    folder_cache: "FolderSheetCache",
    commodity_value: str | None = None,
) -> tuple[int | None, str]:
    """
    Resolves a ROUTE value (plus, for RELOAD, the LOCAL ROUTE value; for
    IXM, the COMMODITY value) to a target invoicing sheet_id.

    Returns (sheet_id_or_None, reason) — reason is a short human-readable
    string explaining the outcome, useful for logging when sheet_id is None.
    """
    normalized_route = norm(route_value)

    if normalized_route in KNOWN_ROUTE_OVERRIDES:
        return KNOWN_ROUTE_OVERRIDES[normalized_route], "known override"

    if normalized_route == reload_.RELOAD_TRIGGER_ROUTE:
        return reload_.resolve(local_route_value)

    if normalized_route == ixm.IXM_TRIGGER_ROUTE:
        return ixm.resolve(commodity_value)

    prefix = route_value.split("-", 1)[0].strip()
    if not prefix:
        return None, "ROUTE value has no client prefix (blank before first '-')"

    # Resolve prefix -> invoicing folder_id by comparing against each known
    # client's slug (with underscores as spaces). Every MAIN_SHEETS entry's
    # invoicing folder name matches its slug once cased/spaced the same way
    # — confirmed live for all 12 clients, including Mittal (colloquial
    # slug "mittal" already equals its real ROUTE prefix "Mittal", so no
    # extra special-casing is needed here beyond this normalization).
    normalized_prefix = norm(prefix)
    matched_folder_id = None
    for slug, config in MAIN_SHEETS.items():
        if normalized_prefix == slug.replace("_", " "):
            matched_folder_id = config["invoicing_folder_id"]
            break

    if matched_folder_id is None:
        return None, f"no known client folder matches ROUTE prefix {prefix!r}"

    sheets_in_folder = folder_cache.sheets_in_folder(matched_folder_id)
    sheet_id = sheets_in_folder.get(normalized_route)
    if sheet_id is None:
        return None, f"no sheet in folder {matched_folder_id} matches ROUTE value {route_value!r}"
    return sheet_id, "matched folder + sheet name"
