"""The generic rule: whole ROUTE value must equal a sheet name in the client's
invoicing folder (case / extra-space insensitive). No match -> (None, reason).
Never guesses."""

from ..util import norm


def resolve_in_folder(client_slug: str, route_prefixes: tuple, folder_id: int, route_value: str, folder_cache):
    """Returns (sheet_id_or_None, reason).

    route_prefixes: the normalized text before the first '-' that marks a route as
    belonging to this client. A ROUTE from a different client's picklist is
    refused rather than silently routed into another client's folder."""
    prefix = norm(route_value.split("-", 1)[0])
    if not prefix:
        return None, "ROUTE value has no client prefix (blank before first '-')"
    if prefix not in route_prefixes:
        return None, (f"ROUTE prefix {route_value.split('-', 1)[0].strip()!r} does not belong to "
                      f"{client_slug} (expected one of {sorted(route_prefixes)})")
    sheets_in_folder = folder_cache.sheets_in_folder(folder_id)
    sheet_id = sheets_in_folder.get(norm(route_value))
    if sheet_id is None:
        return None, f"no sheet in folder {folder_id} matches ROUTE value {route_value!r}"
    return sheet_id, "matched folder + sheet name"
