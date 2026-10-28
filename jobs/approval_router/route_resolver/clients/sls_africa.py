"""SLS Africa -- NDOLA-DAR / NDOLA-GRB converge onto NDOLA-PORT."""

# route (normalized) -> invoicing sheet_id. Merged into the global override
# table by resolver.py.
ROUTE_OVERRIDES: dict[str, int] = {}

SLS_AFRICA_NDOLA_PORT = 4990485677690756

# SLS Africa NDOLA consolidation, 2026-09-16 (per Jay): NDOLA-DAR and
# NDOLA-GRB both now converge onto one NDOLA-PORT sheet. Both old sheets
# confirmed empty before deletion -- no data migration needed.
# NDOLA-NAKONDE removed entirely (not replaced): no override needed since
# it's no longer a selectable ROUTE option once Jay finishes removing it
# from the picklist (the sheet-deletion half of this cleanup ran live;
# the ROUTE-option removal is still pending on his end).
ROUTE_OVERRIDES.update({
    " ".join(v.split()).casefold(): SLS_AFRICA_NDOLA_PORT for v in (
        "SLS Africa-NDOLA-DAR", "SLS Africa-NDOLA-GRB",
    )
})

# Added 2026-10-28 (per Jay): two new routes. NO code needed -- each resolves by
# the normal folder + exact-sheet-name match once the ROUTE picklist option is
# added with exactly the same text as the sheet name:
#   "SLS Africa-NDOLA - WALVIS BAY"  -> sheet_id 4806276258418564
#   "SLS Africa-SAKANIA - CDM MINE"  -> sheet_id 6356484574367620
