"""Glencore International -- ROUTE values grouped onto per-mine invoicing sheets."""

from ..shared import folder_match
from ..util import client_logger, norm

CLIENT_SLUG = "glencore_international"
MAIN_SHEET_ID = 3849491978342276
INVOICING_FOLDER_ID = 7194173326550916
ROUTE_PREFIXES = ("glencore international",)   # normalized text before the first '-'

log = client_logger(CLIENT_SLUG)


GLENCORE_FIMPIMPA = 7949082489474948
GLENCORE_KANSANSHI = 6824445303017348
GLENCORE_KCM = 628035855536004
GLENCORE_MIMBULA = 7277469863464836
GLENCORE_MOPANI = 7105748481036164
GLENCORE_RGT = 8964850844913540
GLENCORE_ZANRONG = 4572997676650372
GLENCORE_ZCCZ = 5130982647877508
GLENCORE_CCM = 4440228627697540
# Added 2026-10-28 (per Jay): two new single-route sheets. Named without the
# destination suffix, like the other group sheets, so each needs an explicit
# override (the sheet name does not equal the ROUTE value).
GLENCORE_KASUMBALESA = 5231959057059716   # "Glencore international-Kasumbalesa"
GLENCORE_POLYTRA = 7552409628004228       # "Glencore international-Polytra"

# route (normalized) -> invoicing sheet_id. Merged into the global override
# table by resolver.py.
ROUTE_OVERRIDES: dict[str, int] = {}

# Glencore International consolidation, 2026-09-16 (per Jay): the 17
# per-destination route sheets are grouped down to 8 sheets by mine/
# loading-point prefix (the ROUTE picklist itself is untouched -- staff
# still pick the same 17 granular values, they just now converge on
# fewer physical sheets). Audited live: only Fimpimpa-DAR had real data
# (2 rows, recovered from Deleted Items and moved onto the new FIMPIMPA
# sheet after an accidental early deletion of the old sheet, 2026-09-16).
# Note: FIMPIMPA, RGT, and ZCCZ each only ever had one ROUTE value to
# begin with, so those three overrides are effectively a rename (dropping
# the destination suffix) rather than a true many-to-one merge -- kept in
# this same mechanism for consistency, per Jay's "group by first suffix,
# extra" instruction covering all of them uniformly.
# 2026-09-29 (per Jay): two more routes added -- "Mopani-Harare" joins the
# existing MOPANI group, and "CCM-Beira" starts a 9th group (CCM) with its
# own new sheet, GLENCORE INTERNATIONAL-CCM (GLENCORE_CCM above).
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_FIMPIMPA for v in (
        "Glencore international-Fimpimpa-DAR",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_KANSANSHI for v in (
        "Glencore international-Kansanshi-Beira", "Glencore international-Kansanshi-GRB",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_KCM for v in (
        "Glencore international-KCM-DBN", "Glencore international-KCM-GRB",
        "Glencore international-KCM-JHB", "Glencore international-KCM-POLYTRA KIWE",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_MIMBULA for v in (
        "Glencore international-Mimbula-Beira", "Glencore international-Mimbula-WVB",
        "Glencore international-Mimbula-DAR",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_MOPANI for v in (
        "Glencore international-Mopani-GRB", "Glencore international-Mopani-DBN",
        "Glencore international-Mopani-JHB", "Glencore international-Mopani-Harare",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_RGT for v in (
        "Glencore international-RGT-WVB",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_ZANRONG for v in (
        "Glencore international-Zanrong-DAR", "Glencore international-Zanrong-WVB",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_ZCCZ for v in (
        "Glencore international-ZCCZ-DAR",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_CCM for v in (
        "Glencore international-CCM-Beira",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_KASUMBALESA for v in (
        "Glencore international-Kasumbalesa-Dar",
    )
})
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): GLENCORE_POLYTRA for v in (
        "Glencore international-Polytra-GRB",
    )
})


def process_row(conn, ss_client, main_sheet_id, row, columns_by_id, stats, **ctx) -> bool:
    """No custom per-row flow for this client."""
    return False


# Snapshot cases: every override must land on its sheet; one plain case shows the folder-match step.
SELFTEST_CASES = [(route, None, None, sheet_id) for route, sheet_id in ROUTE_OVERRIDES.items()]


def resolve(route_value, local_route_value, commodity_value, folder_cache):
    """Returns (sheet_id_or_None, reason)."""
    normalized_route = norm(route_value)
    # 1. Known override table (many ROUTE values -> one mine sheet).
    if normalized_route in ROUTE_OVERRIDES:
        return ROUTE_OVERRIDES[normalized_route], "known override"
    # 2. Otherwise the whole ROUTE must equal a sheet name in the folder.
    sheet_id, reason = folder_match.resolve_in_folder(
        CLIENT_SLUG, ROUTE_PREFIXES, INVOICING_FOLDER_ID, route_value, folder_cache)
    if sheet_id is None:
        log.debug("step 2 (folder match) failed: %s", reason)
    return sheet_id, reason
