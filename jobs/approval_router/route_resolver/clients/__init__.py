"""One file per client. Every client has its own file, even when the logic is
shared, so a failure in the log can be traced to the exact client file.

Contract every client module must provide (checked by `selftest`):

    CLIENT_SLUG          str   matches the registry key below
    MAIN_SHEET_ID        int   the client's main tracking sheet
    INVOICING_FOLDER_ID  int   the client's invoicing folder
    log                  logger 'approval_router.route_resolver.<slug>'
    SELFTEST_CASES       list of (route, local_route, commodity, expected_sheet_id)

    resolve(route_value, local_route_value, commodity_value, folder_cache)
        -> (sheet_id | None, reason)     the client's ROUTE rules, step by step
    process_row(conn, ss_client, main_sheet_id, row, columns_by_id, stats, **ctx)
        -> bool    True = the client's own flow handled the row (Bridge, GSM,
                   Gold Vale country legs); False = carry on to resolve()
    after_sheet(conn, sheet_ctx)         optional hook after a sheet's row loop

To add a client: copy alistair.py (simplest example), fill in the ids, add it
to CLIENTS below. Nothing else needs to change.
"""

from . import (alistair, reload_, glencore_international, fh_bertling, bridge, goldvale,
               gsm, ixm, mittal, sls_africa, sls_trading, zalawi)

# Order = the order clients are processed each run (unchanged from before).
CLIENTS = {
    m.CLIENT_SLUG: m for m in (
        alistair, reload_, glencore_international, fh_bertling, bridge, goldvale,
        gsm, ixm, mittal, sls_africa, sls_trading, zalawi,
    )
}
