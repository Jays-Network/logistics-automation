"""RELOAD -- ROUTE 'Reload-DRC-LOCAL' is resolved via the LOCAL ROUTE column."""

from ..util import norm

# RELOAD's special case: when ROUTE == "Reload-DRC-LOCAL", the LOCAL ROUTE
# column (not ROUTE) determines the real target — one of 4 sheets. Confirmed
# live against the real LOCAL ROUTE picklist 2026-09-13 (29 options, matches
# Jay's original table exactly). Keyed by normalized LOCAL ROUTE value.
RELOAD_TRIGGER_ROUTE = "reload-drc-local"

RELOAD_KOLWEZI = 907292680605572
RELOAD_LIKASI = 3831881941340036
RELOAD_FUNGURUME = 4887378744266628
# Sheet renamed "Kasumbalesa" -> "Lubumbashi" by Jay, 2026-09-16 (manual
# Smartsheet UI change -- no API for renames). Same sheet_id, routing is
# ID-based so this needed no other code change. The "KAS" suffix in the
# LOCAL ROUTE picklist values below is unrelated -- that's the
# Kasumbalesa border-crossing abbreviation on the main-tracking side,
# confirmed staying as-is.
RELOAD_LUBUMBASHI = 869874388651908

RELOAD_LOCAL_ROUTE_MAP: dict[str, int] = {
    normalized: RELOAD_KOLWEZI for normalized in (
        "reload-deziwa - kas", "reload-metalkol - kas", "reload-comilu - kas",
        "reload-hmc - kas", "reload-tcc - kas", "reload-brother - kas",
        "reload-kamoa - kas", "reload-kamoa - sak", "reload-lcs - kas",
        "reload-mmt - kas", "reload-zfm - kas", "reload-mkm - kas",
        "reload-kas - kamoa", "reload-kms - kas", "reload-kcc - kas",
        # Added 2026-10-15 (per Jay). Note: no space before hyphen in the
        # live picklist value "RELOAD-SICO- KAS".
        "reload-sico- kas",
        # Added 2026-10-16 (per Jay): MUMI is Kolwezi.
        "reload-mumi - kas", "reload-mumi - sak",
    )
}
RELOAD_LOCAL_ROUTE_MAP.update({
    normalized: RELOAD_LIKASI for normalized in (
        "reload-ruba - mokambo", "reload-ruba mine - kas", "reload-smco - kas",
        "reload-kambove - kas", "reload-kpm - kas",
        # Added 2026-10-15 (per Jay).
        "reload-jcm - kas", "reload-excellent-kas", "reload-mjm-kas",
    )
})
RELOAD_LOCAL_ROUTE_MAP.update({
    normalized: RELOAD_FUNGURUME for normalized in (
        "reload-tfm - mokambo", "reload-tfm - kas", "reload-tfm - sak",
        "reload-lamikal - kas", "reload-lamikal - sak", "reload-kfm - mokambo",
        "reload-kisenda - kas",
        # Added 2026-10-16 (per Jay): KFM is Fungurume.
        "reload-kfm - kas",
    )
})
RELOAD_LOCAL_ROUTE_MAP.update({
    normalized: RELOAD_LUBUMBASHI for normalized in (
        "reload-sem - kas", "reload-kicc - kas",
        # Added 2026-10-15 (per Jay).
        "reload-stl - kas",
        # Added 2026-10-15 (per Jay): "Kasumbalesa" = the Lubumbashi sheet.
        # Note the live picklist spelling is "KINSENDA-JAS" (not KISENDA/KAS).
        "reload-kinsenda-jas",
    )
})


def resolve(local_route_value):
    """Returns (sheet_id_or_None, reason) -- same contract as resolve_target_sheet_id()."""
    if not local_route_value or not local_route_value.strip():
        return None, "RELOAD-DRC-LOCAL but LOCAL ROUTE column is blank"
    normalized_local = norm(local_route_value)
    sheet_id = RELOAD_LOCAL_ROUTE_MAP.get(normalized_local)
    if sheet_id is None:
        return None, f"RELOAD LOCAL ROUTE value not in known map: {local_route_value!r}"
    return sheet_id, "RELOAD local route map"
