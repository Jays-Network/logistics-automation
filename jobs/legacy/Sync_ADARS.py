import sys  # <-- Added for sys.exit()
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

# --- 1. SETUP & TELEGRAM INIT (Top Priority) ---
load_dotenv()

LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN')
ADARS_LOG_ID = os.getenv('ADARS_LOG_ID')
LOCK_FILE = "/opt/jaysnet/adars_sync.lock" # <-- Unique Lock file location for ADARS

def send_telegram(message):
    """Sends a message ONLY to the ADARS Log Group"""
    if not LOG_BOT_TOKEN or not ADARS_LOG_ID:
        print("⚠️ Telegram tokens missing, skipping alert.")
        return
    
    api_url = f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, json={
            "chat_id": ADARS_LOG_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to send Telegram: {e}")

# --- 2. SECURITY HANDSHAKE ---
print("🔐 Authenticating with Gatekeeper...")
license_key = os.getenv('LICENSE_KEY') or os.getenv('JAYS_NETWORK_LICENSE_KEY')

if not license_key:
    err_msg = "❌ *LICENSE ERROR*: Key missing in .env"
    print(err_msg)
    send_telegram(err_msg)
    exit(1)

auth_system = JaysNetworkLicense(license_key)
credentials = auth_system.authenticate()

if not credentials:
    err_msg = "⛔ *ACCESS DENIED*: License Validation Failed. Script Aborted."
    print(err_msg)
    send_telegram(err_msg) 
    exit(1)

print("✅ *ACCESS GRANTED*: Secure Connection Established.")

# --- 3. DATABASE INITIALIZATION ---
def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=credentials["url"],       
            user=credentials["key"],       
            password=credentials["pass"],  
            database="jaysnet_data",     
            port="5432"
        )
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"🔴 DB Connection Failed: {e}")
        send_telegram(f"🚨 *DATABASE ERROR*: Could not connect to Postgres.\n`{e}`")
        exit(1)

db_conn = get_db_connection()

# --- 4. SMARTSHEET INITIALIZATION ---
ss_client = smartsheet.Smartsheet(os.getenv('SMARTSHEET_ACCESS_TOKEN'))
ss_client.errors_as_exceptions = False # type: ignore

# --- CONFIGURATION (5-MINUTE DELTA MODE) ---
SYNC_INTERVAL_MINUTES = 20
current_hour = datetime.now().hour

if current_hour == 0:
    FORCE_FULL_SYNC = True
    SYNC_MODE_NAME = "🌙 Midnight Full Rescan"
else:
    FORCE_FULL_SYNC = False  # <-- Delta logic confirmed correct 2026-09-10, running for real now
    SYNC_MODE_NAME = f"☀️ Delta Sync (Last {SYNC_INTERVAL_MINUTES} mins)"

def load_id_list(env_var):
    raw = os.getenv(env_var, "")
    return [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]

ADARS_IDS = load_id_list('ADARS_WORKSPACES')

# --- GEO MASTER LIST ---
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
    "point": (-29.8700, 31.0300), "walvis": (-22.9575, 14.5053), "walvis bay": (-22.9575, 14.5053),
    "windhoek": (-22.5609, 17.0658), "katima": (-17.5000, 24.2667), "uis": (-21.2200, 14.8700),
    "ndola": (-12.9716, 28.6309), "lusaka": (-15.3875, 28.3228), "kitwe": (-12.8024, 28.2132),
    "solwezi": (-12.1688, 26.3592), "kansanshi": (-12.0955, 26.3750), "kanshanshi": (-12.0955, 26.3750),
    "chirundu": (-16.0378, 28.8524), "nakonde": (-9.3175, 32.7486), "sesheke": (-17.4750, 24.2961),
    "shesheke": (-17.4750, 24.2961), "chingola": (-12.5280, 27.8427), "luanshya": (-13.1415, 28.4116),
    "luanshiya": (-13.1415, 28.4116), "kabwe": (-14.4300, 28.4500), "sable": (-14.4300, 28.4500),
    "serenje": (-13.2300, 30.2300), "zccz": (-12.5500, 27.8700), "cnmc": (-13.1400, 28.4100),
    "mmt": (-12.9700, 28.6300), "livingstone": (-17.8500, 25.8500), "lubumbashi": (-11.6609, 27.4794),
    "kolwezi": (-10.7148, 25.4725), "kasumbalesa": (-12.2576, 27.8042), "kasumbelesa": (-12.2576, 27.8042),
    "kamoa": (-10.8256, 25.2167), "kicc": (-10.8256, 25.2167), "tfm": (-10.6050, 26.1700),
    "mutanda": (-10.7850, 25.4290), "kfm": (-10.9500, 25.5500), "kisanfu": (-10.9500, 25.5500),
    "rgt": (-11.6200, 27.5500), "ruashi": (-11.6200, 27.5500), "rdc": (-11.6600, 27.4700),
    "lamikal": (-10.9800, 26.7300), "jcm": (-11.6600, 27.4800), "grb": (-11.6600, 27.4800),
    "thomas": (-10.7200, 25.4700), "new minerals": (-11.6600, 27.4800), "kambove": (-10.8800, 26.6000),
    "mikas": (-10.7200, 25.4700), "commus": (-10.7200, 25.4700), "cjcmc": (-11.6600, 27.4800),
    "nmi": (-11.6600, 27.4800), "mjm": (-11.6600, 27.4800), "sakania": (-12.7500, 28.5700),
    "lonshi": (-13.1800, 28.9800), "gecamine": (-10.7200, 25.4700), "bms": (-10.7200, 25.4700),
    "bsm": (-10.7200, 25.4700), "metro": (-10.7200, 25.4700), "luilu": (-10.7800, 25.4000),
    "mumi": (-10.7800, 25.4200), "ccr": (-10.7200, 25.4700), "glencore": (-10.7200, 25.4700),
    "dar": (-6.7924, 39.2083), "alistair": (-6.7924, 39.2083), "tunduma": (-9.3000, 32.7667),
    "rusumo": (-2.3800, 30.7800), "kingali": (-6.8000, 39.2000), "beira": (-19.8416, 34.8387),
    "maputo": (-25.9692, 32.5732), "machipanda": (-18.9800, 32.7500), "bcl": (-21.9700, 27.8400),
    "selibe": (-21.9700, 27.8400), "gaborone": (-24.6282, 25.9231), "francistown": (-21.1736, 27.5125),
    "kazungula": (-17.7916, 25.2601), "palapye": (-22.5500, 27.1333),
}

COLUMN_MAPPING = {
    "client_name": ["Client", "CLIENT", "CLEINT"], 
    "transporter": ["Transporter", "TRANSPORTER", "Haulier", "Sub-Contractor"],
    "reg_number": ["Reg", "HORSE REG", "REG", "Registration", "Reference", "Vehicle Reg"],
    "device_number": ["Device No", "Device", "DEVICE NUMBER", "Device ID"],
    "internal_ref": ["REFS", "ADARS Booking Ref", "INTERNAL REF:", "SLS Lot #", "Ref", "Client Ref"],
    "loading_location": ["Loading Location", "Starting Location", "LOADING POINT", "Origin"],
    "offloading_location": ["Delivery address", "OFFLOADING POINT", "Destination", "Delivery Location"],
    "current_location": ["Current Location", "CURRENT LOCATION", "Position", "Check Point", "Current Loacation"], 
    "status": ["Status", "TRANSIT STATUS", "Transit Status", "Current Status"],
    
    "escort_name": ["Escort Name", "ESCORT", "Escort", "RSA-BORDERS-REGION: ESCORT NAME", "RSA-DBN REGION: ESCORT NAME"],
    "escort_drc": ["DRC REGION: ESCORT NAME"],
    "escort_zam": ["ZAM REGION: ESCORT NAME"],
    "escort_bots": ["BOTS REGION: ESCORT NAME"],
    "escort_zim": ["ZIM REGION: ESCORT NAME"],
    "escort_mal": ["MAL REGION: ESCORT NAME"],
    "escort_moz": ["MOZ REGION: ESCORT NAME"],
}

DATE_CANDIDATES = [
    "Date tagged", "Date the intransit began", "DATE BOOKING MADE", 
    "Date", "Loading Date", "Start Date"
]

def parse_date(date_str):
    if not date_str: return None
    try:
        dt = dateutil.parser.parse(str(date_str))
        return dt 
    except:
        return None

def get_coords(location_str):
    if not location_str: return None, None
    clean_loc = str(location_str).lower().strip()
    if clean_loc in GEO_LOOKUP: return GEO_LOOKUP[clean_loc]
    for key, coords in GEO_LOOKUP.items():
        if key in clean_loc: return coords
    return None, None

def robust_api_call(func, *args, **kwargs):
    attempts = 0
    while attempts < 3:
        try:
            response = func(*args, **kwargs)
            if hasattr(response, 'result') and hasattr(response.result, 'code') and response.result.code == 4003:
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

def should_process_sheet(sheet_lite):
    if FORCE_FULL_SYNC: return True
    if not hasattr(sheet_lite, 'modified_at') or not sheet_lite.modified_at: return True
    last_mod = parse_date(str(sheet_lite.modified_at))
    if not last_mod: return True
    
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=SYNC_INTERVAL_MINUTES)
    if last_mod.tzinfo is None:
        last_mod = last_mod.replace(tzinfo=timezone.utc)

    if last_mod > cutoff_time: return True
    return False

def process_sheet(sheet_id, sheet_name, workspace_name, folder_name):
    sheet = robust_api_call(ss_client.Sheets.get_sheet, sheet_id)
    if not sheet: return 0

    col_map = {col.id: col.title for col in sheet.columns}
    
    field_to_col_id = {} 
    for db_field, candidates in COLUMN_MAPPING.items():
        for col in sheet.columns:
            if col.title in candidates:
                field_to_col_id[db_field] = col.id
                break
    
    date_col_id = None
    for col in sheet.columns:
        if col.title in DATE_CANDIDATES:
            date_col_id = col.id
            break

    rows_payload = []

    for row in sheet.rows:
        row_dict = {col_map.get(c.column_id): str(c.display_value or c.value or "") for c in row.cells}
        
        sys_created_raw = getattr(row, 'createdAt', None) or getattr(row, 'created_at', None)
        sys_created_dt = parse_date(sys_created_raw)
        sys_created_iso = sys_created_dt.replace(tzinfo=None).isoformat() if sys_created_dt else None
        
        # Grab the Last Modified time from Smartsheet
        sys_modified_raw = getattr(row, 'modifiedAt', None) or getattr(row, 'modified_at', None)
        sys_modified_dt = parse_date(sys_modified_raw)
        sys_modified_iso = sys_modified_dt.replace(tzinfo=None).isoformat() if sys_modified_dt else None

        # Fully pre-filled to prevent Postgres mapping errors
        clean_obj = {
            "row_id": str(row.id), "sheet_id": str(sheet_id), "sheet_name": sheet_name,
            "workspace_name": workspace_name, "folder_name": folder_name,
            "system_created_at": sys_created_iso, 
            "row_modified_at": sys_modified_iso, # 👈 Added the missing modified time
            "client_name": None, "booking_date": None,
            "loading_lat": None, "loading_lon": None, "offloading_lat": None, "offloading_lon": None,
            "escort_drc": None, "escort_mal": None, "escort_moz": None, "escort_nam": None,
            "escort_tan": None, "escort_zam": None, "escort_zim": None, "escort_bots": None,
            "raw_data": json.dumps(row_dict), 
            "transporter": None, "reg_number": None, "device_number": None, "internal_ref": None,
            "loading_location": None, "offloading_location": None, "current_location": None, 
            "status": None, "escort_name": None
        }

        for cell in row.cells:
            for db_field, target_id in field_to_col_id.items():
                if cell.column_id == target_id:
                    val = str(cell.display_value or cell.value or "")
                    clean_obj[db_field] = val
                    
                    if db_field == "loading_location":
                        lat, lon = get_coords(val)
                        clean_obj["loading_lat"] = lat
                        clean_obj["loading_lon"] = lon
                    if db_field == "offloading_location":
                        lat, lon = get_coords(val)
                        clean_obj["offloading_lat"] = lat
                        clean_obj["offloading_lon"] = lon

            if cell.column_id == date_col_id:
                human_date_dt = parse_date(cell.display_value or cell.value)
                final_date = None
                
                h_dt_naive = human_date_dt.replace(tzinfo=None) if human_date_dt else None
                s_dt_naive = sys_created_dt.replace(tzinfo=None) if sys_created_dt else None

                if h_dt_naive and s_dt_naive:
                    delta = abs((h_dt_naive - s_dt_naive).days)
                    if delta > 7: final_date = s_dt_naive
                    else: final_date = h_dt_naive
                elif h_dt_naive: final_date = h_dt_naive
                elif s_dt_naive: final_date = s_dt_naive
                
                if final_date:
                    clean_obj["booking_date"] = final_date.strftime('%Y-%m-%d')

        if not clean_obj["booking_date"] and sys_created_dt:
             clean_obj["booking_date"] = sys_created_dt.replace(tzinfo=None).strftime('%Y-%m-%d')

        if clean_obj.get("reg_number") or clean_obj.get("booking_date"):
            rows_payload.append(clean_obj)

    if rows_payload:
        total_rows = len(rows_payload)
        print(f"      ⏳ [{folder_name}] Syncing {total_rows} rows from {sheet_name}...")

        # 🚀 HIGH PERFORMANCE POSTGRES BULK UPSERT
        columns = list(rows_payload[0].keys())
        query = f"""
            INSERT INTO adars_tracking ({', '.join(columns)}) 
            VALUES %s
            ON CONFLICT (row_id, sheet_id) DO UPDATE SET
            {', '.join([f"{col} = EXCLUDED.{col}" for col in columns if col not in ['row_id', 'sheet_id']])}
        """
        
        values = [[row[col] for col in columns] for row in rows_payload]
        
        try:
            cursor = db_conn.cursor()
            psycopg2.extras.execute_values(cursor, query, values)
            cursor.close()
        except Exception as e:
            err_msg = f"❌ Error in {sheet_name}: {str(e)[:100]}"
            print(err_msg)
            db_conn.rollback() 
            send_telegram(f"🚨 *ADARS SYNC ERROR*\nSheet: `{sheet_name}`\nError: `{str(e)[:200]}`")

        return total_rows
    return 0

def main():
    # --- DYNAMIC SMART LOCK ---
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > 3600:  # 3600 seconds = 60 minutes
            print("🧟 Zombie lock detected (older than 60 mins). Clearing it...")
            os.remove(LOCK_FILE)
        else:
            print("⏳ Sync is currently running (Day or Night). Aborting to prevent overlap.")
            sys.exit(0) # Exit cleanly without error
            
    # Create the lock
    with open(LOCK_FILE, 'w') as f:
        f.write(str(time.time()))

    try:
        start_time = time.time()
        
        start_msg = f"🚀 *ADARS SYNC STARTED*\nMode: `{SYNC_MODE_NAME}`"
        print(start_msg)

        total_sheets_checked = 0
        total_rows_synced = 0
        
        for ws_id in ADARS_IDS:
            ws = robust_api_call(ss_client.Workspaces.get_workspace, ws_id)
            if not ws: continue

            print(f"📂 Workspace: {ws.name}")
            
            for sheet_lite in ws.sheets:
                if "ARCHIVE" in sheet_lite.name.upper(): continue 
                
                if should_process_sheet(sheet_lite):
                    total_sheets_checked += 1
                    count = process_sheet(sheet_lite.id, sheet_lite.name, ws.name, ws.name)
                    total_rows_synced += count
                else:
                    if FORCE_FULL_SYNC: print(f"   Note: Force Sync Active for {sheet_lite.name}")

            for folder in ws.folders:
                full_folder = robust_api_call(ss_client.Folders.get_folder, folder.id)
                if not full_folder: continue
                print(f"   L_ Folder: {full_folder.name}")
                
                for sheet_lite in full_folder.sheets:
                     if "ARCHIVE" in sheet_lite.name.upper(): continue
                     
                     if should_process_sheet(sheet_lite):
                         total_sheets_checked += 1
                         count = process_sheet(sheet_lite.id, sheet_lite.name, ws.name, full_folder.name)
                         total_rows_synced += count

        duration = round(time.time() - start_time, 2)
        end_msg = (
            f"✅ *ADARS SYNC COMPLETE*\n"
            f"📊 Sheets Updated: `{total_sheets_checked}`\n"
            f"💾 Rows Synced: `{total_rows_synced}`\n"
            f"⏱️ Time: `{duration}s`"
        )
        print(end_msg)
        
        send_telegram(end_msg)

    except Exception as e:
        crash_msg = f"🔥 *ADARS CRITICAL SCRIPT FAILURE*\nError: `{str(e)}`"
        print(crash_msg)
        send_telegram(crash_msg)
    finally:
        # --- RELEASE THE LOCK ---
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        if db_conn:
            db_conn.close()

if __name__ == "__main__":
    main()