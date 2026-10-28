from dotenv import load_dotenv
load_dotenv("/opt/jaysnet/logistics-automation/.env")
from jobs.approval_router.backup import get_connection

conn = get_connection()
cur = conn.cursor()
cur.execute("SELECT 1 FROM approval_router_archive_state WHERE client_slug='alliance' AND is_active=true")
if cur.fetchone():
    print("alliance archive row already present - nothing to do")
else:
    cur.execute(
        "INSERT INTO approval_router_archive_state (client_slug, sheet_id, sheet_name, is_active) "
        "VALUES ('alliance', 7348488238223236, 'Arcive Alliance invoicing', true)"
    )
    conn.commit()
    print("alliance archive row inserted")
cur.execute("SELECT client_slug, sheet_id, sheet_name, is_active FROM approval_router_archive_state WHERE client_slug='alliance'")
print(cur.fetchall())
conn.close()