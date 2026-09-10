import sys
import os
import time
import json
import requests
import smartsheet
import dateutil.parser
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from license_manager import JaysNetworkLicense

# =============================================================================
# SECTION 1 — SETUP & TELEGRAM
# =============================================================================
load_dotenv()

LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN')
RISK_LOG_ID   = os.getenv('RISK_LOG_ID')

LOCK_FILE = "/opt/jaysnet/worldrisk_sync.lock"

def send_telegram(message):
    if not LOG_BOT_TOKEN or not RISK_LOG_ID:
        print("⚠️ Telegram tokens missing, skipping alert.")
        return
    api_url = f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, json={
            "chat_id": RISK_LOG_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to send Telegram: {e}")


# =============================================================================
# SECTION 2 — LICENSE HANDSHAKE
# =============================================================================
print("🔐 Authenticating with Gatekeeper...")
license_key = os.getenv('LICENSE_KEY') or os.getenv('JAYS_NETWORK_LICENSE_KEY')

if not license_key:
    err_msg = "❌ *LICENSE ERROR*: Key missing in .env"
    print(err_msg)
    send_telegram(err_msg)
    exit(1)

auth_system  = JaysNetworkLicense(license_key)
credentials  = auth_system.authenticate()

if not credentials:
    err_msg = "⛔ *ACCESS DENIED*: License Validation Failed. Script Aborted."
    print(err_msg)
    send_telegram(err_msg)
    exit(1)

print("✅ *ACCESS GRANTED*: Secure Connection Established.")


# =============================================================================
# SECTION 3 — DATABASE CONNECTION
# =============================================================================
def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=credentials["url"],
            user=credentials["key"],
            password=credentials["pass"],
            database="jaysnet_data",
            port="5432"
        )
        return conn
    except Exception as e:
        print(f"🔴 DB Connection Failed: {e}")
        send_telegram(f"🚨 *DATABASE ERROR*: Could not connect to Postgres.\n`{e}`")
        exit(1)


# =============================================================================
# SECTION 4 — SMARTSHEET INITIALIZATION
# =============================================================================
ss_client = smartsheet.Smartsheet(os.getenv('SMARTSHEET_ACCESS_TOKEN'))
ss_client.errors_as_exceptions = False


# =============================================================================
# SECTION 5 — SYNC CONFIGURATION
# =============================================================================
# FORCE_FULL_SYNC is now a genuine manual override / safety net, not a permanent
# workaround for broken delta logic. Delta logic is fixed (see should_process_sheet
# below), so this can safely stay False day-to-day. The midnight full rescan is
# kept as a deliberate once-a-day safety net regardless of delta reliability.
current_hour = datetime.now().hour

if current_hour == 0:
    FORCE_FULL_SYNC = True
    SYNC_MODE_NAME  = "🌙 Midnight Full Rescan"
else:
    FORCE_FULL_SYNC = False
    SYNC_MODE_NAME  = "☀️ Delta Sync (state-anchored)"

def load_id_list(env_var):
    raw = os.getenv(env_var, "")
    return [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]

WORLDRISK_IDS = load_id_list('WORLDRISK_WORKSPACES')


# =============================================================================
# SECTION 6 — GEO LOOKUP TABLE
# =============================================================================
GEO_LOOKUP = {
    "durban": (-29.8587, 31.0218), "richards bay": (-28.7807, 32.0383), "jhb": (-26.2041, 28.0473),
    "johannesburg": (-26.2041, 28.0473), "cape town": (-33.9249, 18.4241), "pietermaritzburg": (-29.6006, 30.3794),
    "vereeniging": (-26.6736, 27.9319), "springs": (-26.2547, 28.4428), "komatipoort": (-25.4335, 31.9547),
    "kpm": (-25.4335, 31.9547), "skilpadshek": (-25.3289, 25.6562), "tulisa": (-26.2570, 28.1130), 
    "aberdare": (-29.6380, 30.4070), "mkondeni": (-29.6380, 30.4070), "cbi electric": (-26.6600, 27.9400),
    "peacehaven": (-26.6600, 27.9400), "maksal": (-26.2600, 28.4200), "marksal": (-26.2600, 28.4200),
    "new era": (-26.2600, 28.4200), "access": (-29.8700, 31.0300), "access world": (-29.8700, 31.0300),
    "bridge": (-33.9608, 25.6022), "steinweg": (-26.2041, 28.0473), "unitrade": (-29.3389, 31.2961),
    "stanger": (-29.3389, 31.2961), "nfm": (-29.9167, 30.9500), "reclam": (-29.9500, 30.9500),
    "cato ridge": (-29.7333, 30.6167), "reload asi": (-26.2000, 28.0500), "fpt": (-29.8700, 31.0300),
    "point": (-29.8700, 31.0300),
    "walvis": (-22.9575, 14.5053), "walvis bay": (-22.9575, 14.5053), "windhoek": (-22.5609, 17.0658),
    "katima": (-17.5000, 24.2667), "uis": (-21.2200, 14.8700),
    "ndola": (-12.9716, 28.6309), "lusaka": (-15.3875, 28.3228), "kitwe": (-12.8024, 28.2132),
    "solwezi": (-12.1688, 26.3592), "kansanshi": (-12.0955, 26.3750), "kanshanshi": (-12.0955, 26.3750),
    "chirundu": (-16.0378, 28.8524), "nakonde": (-9.3175, 32.7486), "sesheke": (-17.4750, 24.2961),
    "shesheke": (-17.4750, 24.2961), "chingola": (-12.5280, 27.8427), "luanshya": (-13.1415, 28.4116),
    "luanshiya": (-13.1415, 28.4116), "kabwe": (-14.4300, 28.4500), "sable": (-14.4300, 28.4500),
    "serenje": (-13.2300, 30.2300), "zccz": (-12.5500, 27.8700), "cnmc": (-13.1400, 28.4100),
    "mmt": (-12.9700, 28.6300), "livingstone": (-17.8500, 25.8500),
    "lubumbashi": (-11.6609, 27.4794), "kolwezi": (-10.7148, 25.4725), "kasumbalesa": (-12.2576, 27.8042),
    "kasumbelesa": (-12.2576, 27.8042), "kamoa": (-10.8256, 25.2167), "kicc": (-10.8256, 25.2167),
    "tfm": (-10.6050, 26.1700), "mutanda": (-10.7850, 25.4290), "kfm": (-10.9500, 25.5500),
    "kisanfu": (-10.9500, 25.5500), "rgt": (-11.6200, 27.5500), "ruashi": (-11.6200, 27.5500),
    "rdc": (-11.6600, 27.4700), "lamikal": (-10.9800, 26.7300), "jcm": (-11.6600, 27.4800),
    "grb": (-11.6600, 27.4800), "thomas": (-10.7200, 25.4700), "new minerals": (-11.6600, 27.4800),
    "kambove": (-10.8800, 26.6000), "mikas": (-10.7200, 25.4700), "commus": (-10.7200, 25.4700),
    "cjcmc": (-11.6600, 27.4800), "nmi": (-11.6600, 27.4800), "mjm": (-11.6600, 27.4800),
    "sakania": (-12.7500, 28.5700), "lonshi": (-13.1800, 28.9800), "gecamine": (-10.7200, 25.4700),
    "bms": (-10.7200, 25.4700), "bsm": (-10.7200, 25.4700), "metro": (-10.7200, 25.4700),
    "luilu": (-10.7800, 25.4000), "mumi": (-10.7800, 25.4200), "ccr": (-10.7200, 25.4700),
    "glencore": (-10.7200, 25.4700),
    "dar": (-6.7924, 39.2083), "alistair": (-6.7924, 39.2083), "tunduma": (-9.3000, 32.7667),
    "rusumo": (-2.3800, 30.7800), "kingali": (-6.8000, 39.2000),
    "beira": (-19.8416, 34.8387), "maputo": (-25.9692, 32.5732), "machipanda": (-18.9800, 32.7500),
    "bcl": (-21.9700, 27.8400), "selibe": (-21.9700, 27.8400), "gaborone": (-24.6282, 25.9231),
    "francistown": (-21.1736, 27.5125), "kazungula": (-17.7916, 25.2601), "palapye": (-22.5500, 27.1333),
}


# =============================================================================
# SECTION 7 — COLUMN MAPPING
# =============================================================================
COLUMN_MAPPING = {
    "transporter":          ["Transporter", "TRANSPORTER", "Haulier", "Sub-Contractor"],
    "reg_number":           ["Reg", "HORSE REG", "REG", "Registration", "Reference", "Vehicle Reg"],
    "device_number":        ["Device No", "Device", "DEVICE NUMBER", "Device ID"],
    "internal_ref":         ["REFS", "INTERNAL REF:", "SLS Lot #", "Ref", "Client Ref"],
    "loading_location":     ["Loading Location", "Starting Location", "LOADING POINT", "Origin"],
    "offloading_location":  ["Delivery address", "OFFLOADING POINT", "Destination", "Delivery Location"],
    "escort_name":          ["Escort Name", "ESCORT", "Escort"],
    "current_location":     ["Current Location", "CURRENT LOCATION", "Position", "Check Point"],
    "status":               ["Status", "TRANSIT STATUS", "Transit Status", "Current Status"],

    # 🎯 ALL DATE COLUMNS EXPLICITLY SEPARATED
    "booking_date":         ["DATE BOOKING MADE", "Date", "Date tagged"],
    "intransit_date":       ["Loading Date"],
    "dispatch_date":        ["Dispatch Date", "Date Dispatched", "Start Date"],
    "intransit_began_date": ["Date the intransit began"],
    "drc_dispatch_date":    ["DRC REGION: DISPATCH DATE"],
    "zam_dispatch_date":    ["ZAM REGION: DISPATCH DATE"],
    "nam_dispatch_date":    ["NAM REGION: DISPATCH DATE"],
    "tan_dispatch_date":    ["TAN REGION: DISPATCH DATE"],
    "zim_dispatch_date":    ["ZIM REGION: DISPATCH DATE"],
    "bots_dispatch_date":   ["BOTS REGION: DISPATCH DATE"],
    "moz_dispatch_date":    ["MOZ REGION: DISPATCH DATE"],
    "sa_dispatch_date":     ["SA REGION: DISPATCH DATE"]
}


# =============================================================================
# SECTION 8 — HELPER FUNCTIONS
# =============================================================================
def parse_date(date_str):
    if not date_str:
        return None
    try:
        return dateutil.parser.parse(str(date_str))
    except Exception:
        return None

def normalize_modified_at(val):
    """Consistent tz-aware datetime for comparing sheet modified_at against stored state."""
    dt = parse_date(str(val)) if val else None
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def get_coords(location_str):
    if not location_str:
        return None, None
    clean = str(location_str).lower().strip()
    if clean in GEO_LOOKUP:
        return GEO_LOOKUP[clean]
    for key, coords in GEO_LOOKUP.items():
        if key in clean:
            return coords
    return None, None

def robust_api_call(func, *args, **kwargs):
    attempts = 0
    while attempts < 3:
        try:
            response = func(*args, **kwargs)
            if hasattr(response, 'result') and hasattr(response.result, 'code'):
                if response.result.code == 4003:
                    print(f"   ⚠️ Rate Limit! Cooling down 60s...")
                    time.sleep(60)
                    attempts += 1
                    continue
            if isinstance(response, smartsheet.models.Error):
                print(f"   ⚠️ Smartsheet API Error: {getattr(response.result, 'message', 'Unknown')}")
                return None
            return response
        except Exception as e:
            print(f"   ⚠️ Network/API Exception: {e}")
            return None
    return None


# =============================================================================
# SECTION 8b — STATE-ANCHORED CHANGE DETECTION (THE ACTUAL FIX)
# =============================================================================
# Old approach: "has this sheet's modified_at moved in the last N minutes from
# right now?" — broken because N (SYNC_INTERVAL_MINUTES) didn't match the real
# cron cadence, guaranteeing a blind-spot window every single cycle regardless
# of how N was tuned, and because it has no memory of whether a run actually
# succeeded.
#
# New approach: "has this sheet's modified_at moved since the last time we
# ACTUALLY synced it successfully?" — anchored to real state in the DB, so it's
# immune to cron jitter, slow runs, missed runs, or cadence changes.

def load_sync_state(conn, sheet_ids):
    """Bulk-fetch known state for every sheet in one query (cheap), instead of
    a per-sheet round trip."""
    if not sheet_ids:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT sheet_id, last_synced_modified_at FROM worldrisk_sync_sheet_state WHERE sheet_id = ANY(%s)",
        (sheet_ids,)
    )
    state = {r['sheet_id']: r['last_synced_modified_at'] for r in cur.fetchall()}
    cur.close()
    return state

def should_process_sheet(sheet_lite, known_state):
    if FORCE_FULL_SYNC:
        return True

    modified_at = normalize_modified_at(getattr(sheet_lite, 'modified_at', None))
    if modified_at is None:
        return True  # can't tell — safest is to check it

    last_synced = known_state  # already tz-aware TIMESTAMPTZ from DB, or None
    if last_synced is None:
        return True  # never synced before — must check

    return modified_at > last_synced

def update_sync_state(cur, sheet_id, sheet_name, workspace_name, folder_name, modified_at):
    cur.execute(
        """INSERT INTO worldrisk_sync_sheet_state
               (sheet_id, sheet_name, workspace_name, folder_name, last_synced_modified_at, last_synced_at)
           VALUES (%s, %s, %s, %s, %s, now())
           ON CONFLICT (sheet_id) DO UPDATE SET
               sheet_name = EXCLUDED.sheet_name,
               workspace_name = EXCLUDED.workspace_name,
               folder_name = EXCLUDED.folder_name,
               last_synced_modified_at = EXCLUDED.last_synced_modified_at,
               last_synced_at = now()""",
        (sheet_id, sheet_name, workspace_name, folder_name, modified_at)
    )


# =============================================================================
# SECTION 9 — CORE SHEET PROCESSOR
# =============================================================================
def process_sheet(conn, sheet_id, sheet_name, workspace_name, folder_name, modified_at):
    """
    Returns (rows_synced, success). success=False means the fetch/DB step failed
    and the caller must NOT update sync state — so this sheet gets retried next
    run instead of being silently marked as checked while never actually synced.
    """
    sheet = robust_api_call(ss_client.Sheets.get_sheet, sheet_id)
    if not sheet:
        return 0, False

    col_map = {col.id: col.title for col in sheet.columns}

    field_to_col_id = {}
    for db_field, candidates in COLUMN_MAPPING.items():
        lower_candidates = [c.lower().strip() for c in candidates]
        for col in sheet.columns:
            if col.title and col.title.lower().strip() in lower_candidates:
                field_to_col_id[db_field] = col.id
                break

    rows_payload = []

    date_fields_list = [
        "booking_date", "dispatch_date", "intransit_date", "intransit_began_date",
        "drc_dispatch_date", "zam_dispatch_date", "nam_dispatch_date",
        "tan_dispatch_date", "zim_dispatch_date", "bots_dispatch_date",
        "moz_dispatch_date", "sa_dispatch_date"
    ]

    for row in sheet.rows:
        row_dict = {
            col_map.get(c.column_id): str(c.display_value or c.value or "")
            for c in row.cells
        }

        sys_created_raw  = getattr(row, 'createdAt', None) or getattr(row, 'created_at', None)
        sys_modified_raw = getattr(row, 'modifiedAt', None) or getattr(row, 'modified_at', None)
        sys_created_dt   = parse_date(sys_created_raw)
        sys_modified_dt  = parse_date(sys_modified_raw)
        sys_created_iso  = sys_created_dt.replace(tzinfo=None).isoformat() if sys_created_dt else None
        sys_modified_iso = sys_modified_dt.replace(tzinfo=None).isoformat() if sys_modified_dt else None

        clean_obj = {
            "row_id":            str(row.id),
            "sheet_id":          str(sheet_id),
            "sheet_name":        sheet_name,
            "workspace_name":    workspace_name,
            "folder_name":       folder_name,
            "system_created_at": sys_created_iso,
            "row_modified_at":   sys_modified_iso,

            "booking_date":         None,
            "dispatch_date":        None,
            "intransit_date":       None,
            "intransit_began_date": None,
            "drc_dispatch_date":    None,
            "zam_dispatch_date":    None,
            "nam_dispatch_date":    None,
            "tan_dispatch_date":    None,
            "zim_dispatch_date":    None,
            "bots_dispatch_date":   None,
            "moz_dispatch_date":    None,
            "sa_dispatch_date":     None,

            "loading_lat":       None,
            "loading_lon":       None,
            "offloading_lat":    None,
            "offloading_lon":    None,
            "raw_data":          psycopg2.extras.Json(row_dict),
            "transporter":       None,
            "reg_number":        None,
            "device_number":     None,
            "internal_ref":      None,
            "loading_location":  None,
            "offloading_location": None,
            "escort_name":       None,
            "current_location":  None,
            "status":            None,
        }

        for cell in row.cells:
            for db_field, target_id in field_to_col_id.items():
                if cell.column_id == target_id:
                    val = str(cell.display_value or cell.value or "").strip()
                    if not val:
                        continue

                    if db_field == "loading_location":
                        lat, lon = get_coords(val)
                        clean_obj["loading_lat"]  = lat
                        clean_obj["loading_lon"]  = lon
                        clean_obj[db_field] = val
                    elif db_field == "offloading_location":
                        lat, lon = get_coords(val)
                        clean_obj["offloading_lat"] = lat
                        clean_obj["offloading_lon"] = lon
                        clean_obj[db_field] = val
                    elif db_field in date_fields_list:
                        dt = parse_date(val)
                        if dt:
                            clean_obj[db_field] = dt.strftime('%Y-%m-%d')
                    else:
                        clean_obj[db_field] = val

        if not clean_obj["booking_date"] and sys_created_dt:
            clean_obj["booking_date"] = sys_created_dt.replace(tzinfo=None).strftime('%Y-%m-%d')

        has_any_date = any(clean_obj[df] is not None for df in date_fields_list)
        if clean_obj.get("reg_number") or has_any_date:
            rows_payload.append(clean_obj)

    total_rows = len(rows_payload)

    try:
        cursor = conn.cursor()

        if total_rows > 0:
            print(f"      ⏳ [{folder_name}] Syncing {total_rows} rows from {sheet_name}...")
            columns    = list(rows_payload[0].keys())
            col_str    = ", ".join(columns)
            update_str = ", ".join([
                f"{col} = EXCLUDED.{col}"
                for col in columns
                if col not in ["row_id", "sheet_id"]
            ])
            query = f"""
                INSERT INTO worldrisk_tracking ({col_str})
                VALUES %s
                ON CONFLICT (row_id, sheet_id) DO UPDATE SET {update_str}
            """
            values = [tuple(row[col] for col in columns) for row in rows_payload]
            psycopg2.extras.execute_values(cursor, query, values)

        active_row_ids = [str(r.id) for r in sheet.rows]

        if active_row_ids:
            delete_query = """
                DELETE FROM worldrisk_tracking
                WHERE sheet_id = %s AND row_id != ALL(%s)
            """
            cursor.execute(delete_query, (str(sheet_id), active_row_ids))
        else:
            delete_query = "DELETE FROM worldrisk_tracking WHERE sheet_id = %s"
            cursor.execute(delete_query, (str(sheet_id),))

        deleted_count = cursor.rowcount
        if deleted_count > 0:
            print(f"      🗑️ Cleaned up {deleted_count} deleted/moved rows from {sheet_name}")

        # Update sync state in the SAME transaction — only reachable on success,
        # so a failure anywhere above leaves state untouched and this sheet gets
        # retried next run instead of being silently marked as checked.
        update_sync_state(cursor, str(sheet_id), sheet_name, workspace_name, folder_name, modified_at)

        conn.commit()
        cursor.close()
        return total_rows, True

    except Exception as e:
        err_msg = f"❌ Error in {sheet_name}: {str(e)[:100]}"
        print(err_msg)
        conn.rollback()
        send_telegram(f"🚨 *SYNC ERROR*\nSheet: `{sheet_name}`\nError: `{str(e)[:200]}`")
        return 0, False


# =============================================================================
# SECTION 10 — MAIN ENTRY POINT
# =============================================================================
def collect_all_sheet_stubs(ws):
    """Metadata-only pass across root + folders + one level of sub-folders —
    no get_sheet calls, so this is cheap and safe to run every time."""
    stubs = []  # (sheet_lite, folder_name)

    for sheet_lite in ws.sheets:
        stubs.append((sheet_lite, ws.name))

    for folder in ws.folders:
        full_folder = robust_api_call(ss_client.Folders.get_folder, folder.id)
        if not full_folder:
            continue
        print(f"   L_ Folder: {full_folder.name}")
        for sheet_lite in full_folder.sheets:
            stubs.append((sheet_lite, full_folder.name))

        for sub_folder_lite in full_folder.folders:
            sub_folder = robust_api_call(ss_client.Folders.get_folder, sub_folder_lite.id)
            if not sub_folder:
                continue
            print(f"      L_ Sub-Folder: {sub_folder.name}")
            for sheet_lite in sub_folder.sheets:
                # Parent folder_name kept (matches original Grafana mapping behavior)
                stubs.append((sheet_lite, full_folder.name))

    return stubs

def main():
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > 3600:
            print("🧟 Zombie lock detected (older than 60 mins). Clearing it...")
            os.remove(LOCK_FILE)
        else:
            print("⏳ Sync already running. Aborting to prevent overlap.")
            sys.exit(0)

    with open(LOCK_FILE, 'w') as f:
        f.write(str(time.time()))

    # One shared DB connection for the whole run instead of one per sheet —
    # avoids repeated connect/auth overhead when many sheets need syncing.
    conn = get_db_connection()

    try:
        start_time = time.time()
        start_msg  = f"🚀 *WORLDRISK SYNC STARTED*\nMode: `{SYNC_MODE_NAME}`"
        print(start_msg)

        total_sheets_checked = 0
        total_sheets_skipped = 0
        total_sheets_failed  = 0
        total_rows_synced    = 0

        for ws_id in WORLDRISK_IDS:
            ws = robust_api_call(ss_client.Workspaces.get_workspace, ws_id)
            if not ws:
                continue
            print(f"📂 Workspace: {ws.name}")

            stubs = collect_all_sheet_stubs(ws)
            sheet_ids = [str(s.id) for s, _ in stubs]
            known_state = load_sync_state(conn, sheet_ids)

            for sheet_lite, folder_name in stubs:
                sheet_id_str = str(sheet_lite.id)
                modified_at = normalize_modified_at(getattr(sheet_lite, 'modified_at', None))

                if not should_process_sheet(sheet_lite, known_state.get(sheet_id_str)):
                    total_sheets_skipped += 1
                    continue

                rows_synced, success = process_sheet(
                    conn, sheet_lite.id, sheet_lite.name, ws.name, folder_name, modified_at
                )
                if success:
                    total_sheets_checked += 1
                    total_rows_synced += rows_synced
                else:
                    total_sheets_failed += 1
                    print(f"   ⚠️ {sheet_lite.name}: sync failed, will retry next run")

        duration = round(time.time() - start_time, 2)
        end_msg = (
            f"✅ *WORLDRISK SYNC COMPLETE*\n"
            f"📊 Sheets Updated: `{total_sheets_checked}`\n"
            f"⏩ Sheets Skipped (unchanged): `{total_sheets_skipped}`\n"
            f"⚠️ Sheets Failed (will retry next run): `{total_sheets_failed}`\n"
            f"💾 Rows Synced: `{total_rows_synced}`\n"
            f"⏱️ Time: `{duration}s`"
        )
        print(end_msg)
        send_telegram(end_msg)

    except Exception as e:
        crash_msg = f"🔥 *WORLDRISK CRITICAL SCRIPT FAILURE*\nError: `{str(e)}`"
        print(crash_msg)
        send_telegram(crash_msg)

    finally:
        conn.close()
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)

if __name__ == "__main__":
    main()