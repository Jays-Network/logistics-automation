"""ROUTE value -> target invoicing sheet_id, dispatched to the client's own file.

Each client's rules live in clients/<slug>.py (resolve()). This module only
picks the right client file, calls it, and makes sure a crash inside one client's
rules is logged against THAT client's file instead of vanishing.

Rules every client file follows (never guess -- no match = (None, reason), the row
is skipped, logged, alerted once per 24h and tracked in
approval_router_unresolved_route_log):
  * client special rules first (override tables, RELOAD LOCAL ROUTE, IXM COMMODITY)
  * then the whole normalized ROUTE must equal a sheet name in the client's
    invoicing folder (shared/folder_match.py)
  * Bridge, GSM and Gold Vale never get here: they use country-leg flows (process_row).
"""

from .clients import CLIENTS
from .util import FolderSheetCache


def resolve_target_sheet_id(
    client_slug: str,
    route_value: str,
    local_route_value: str | None,
    folder_cache: "FolderSheetCache",
    commodity_value: str | None = None,
) -> tuple[int | None, str]:
    """Returns (sheet_id_or_None, reason). reason is prefixed with the client
    slug so a log line points straight at clients/<slug>.py."""
    module = CLIENTS.get(client_slug)
    if module is None:
        return None, f"[{client_slug}] no client file registered in clients/__init__.py"
    try:
        sheet_id, reason = module.resolve(route_value, local_route_value, commodity_value, folder_cache)
    except Exception as exc:  # a bug in ONE client's rules must be traceable to that client
        module.log.error("resolve() crashed in %s for ROUTE=%r: %s", module.__name__, route_value, exc, exc_info=True)
        return None, f"[{client_slug}] resolver crashed in {module.__name__}: {exc}"
    return sheet_id, f"[{client_slug}] {reason}"
