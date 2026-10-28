from collections import Counter
from dotenv import load_dotenv
load_dotenv("/opt/jaysnet/logistics-automation/.env")
from jobs.approval_router import route_resolver as rr

SHEET = 4700548271918980
conn = rr.get_connection()
ss = rr.get_smartsheet_client()
cur = conn.cursor()
cur.execute("SELECT source_row_id, reason FROM approval_router_unresolved_route_log "
            "WHERE source_sheet_id = %s AND client_slug = 'bridge'", (SHEET,))
rows = cur.fetchall()
print("flagged Bridge rows in unresolved log:", len(rows))
print("by reason:", Counter(r[1] for r in rows).most_common())

buckets = Counter()
examples = {}
for rid, reason in rows:
    cur.execute("SELECT country, target_sheet_id, target_row_id, status FROM bridge_country_copy_log "
                "WHERE source_sheet_id = %s AND source_row_id = %s", (SHEET, rid))
    logged = cur.fetchall()
    ok_logged = [l for l in logged if l[3] == 'copied' and l[2] is not None]
    if not logged:
        key = "A: no copy ever logged"
    elif not ok_logged:
        key = "B: only error/null-target copy entries"
    else:
        conf = rr._bridge_confirmed_copies(conn, ss, SHEET, rid)
        key = "C: copy logged AND still on country sheet" if conf else "D: copy logged but NOT on country sheet any more"
    buckets[key] += 1
    examples.setdefault(key, rid)
print()
for k, v in sorted(buckets.items()):
    print(f"{v:4d}  {k}   e.g. row {examples[k]}")
