import os
import time
import smartsheet
from datetime import datetime
from dotenv import load_dotenv
from supabase import create_client, Client

# --- SETUP ---
load_dotenv()

# 1. Connect to Smartsheet
ss_client = smartsheet.Smartsheet(os.getenv('SMARTSHEET_ACCESS_TOKEN'))
ss_client.errors_as_exceptions = False 

# 2. Connect to Supabase (WITH ADMIN "SECRET" PRIVILEGES)
url = os.getenv('SUPABASE_URL')
key = os.getenv('SUPABASE_SERVICE_KEY') # <--- Using Secret Key

if not url or not key:
    print("❌ Error: Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env")
    exit(1)

supabase: Client = create_client(url, key)

# --- CONFIGURATION LOADER ---
def load_id_list(env_var):
    raw = os.getenv(env_var, "")
    if not raw: return []
    return [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]

ADARS_IDS = load_id_list('ADARS_WORKSPACES')
WORLDRISK_IDS = load_id_list('WORLDRISK_WORKSPACES')

print(f"🎯 Loaded Configuration:")
print(f"   - ADARS Workspaces to Scan: {len(ADARS_IDS)}")
print(f"   - WORLDRISK Workspaces to Scan: {len(WORLDRISK_IDS)}")

# --- HELPERS ---

CORE_MAP = {
    "reg_number": ["Reg", "HORSE REG", "REG", "Registration", "Vehicle Reg", "Registration Number"],
    "status": ["Status", "TRANSIT STATUS", "Transit Status", "Current Status", "Trip Status"],
    "location": ["Current Location", "CURRENT LOCATION", "Location", "Check Point", "Current Position"],
    "transporter": ["Transporter", "TRANSPORTER", "Sub-Contractor", "Haulier"]
}

def robust_api_call(func, *args, **kwargs):
    attempts = 0
    while attempts < 5:
        response = func(*args, **kwargs)
        # Check for Rate Limit (4003)
        if hasattr(response, 'result') and hasattr(response.result, 'code') and response.result.code == 4003:
            print(f"   ⚠️ Rate Limit! Cooling down 60s...")
            time.sleep(60)
            attempts += 1
            continue
        return response
    return None

def get_country(sheet_name, row_data):
    """Splits by Country based on Sheet Name keywords."""
    name = sheet_name.upper()
    row_str = str(row_data).upper()

    if "DRC" in name or "CONGO" in name: return "DRC"
    if "ZAM" in name or "NDOLA" in name: return "ZAM"
    if "BOTS" in name or "BOTSWANA" in name: return "BOTS"
    if "MOZ" in name or "MOZAMBIQUE" in name: return "MOZ"
    if "ZIM" in name or "ZIMBABWE" in name: return "ZIM"
    if "NAM" in name or "NAMIBIA" in name: return "NAM"
    if "TZ" in name or "TANZANIA" in name: return "TZ"
    if "SA " in name or "SOUTH AFRICA" in name or "JHB" in name or "DBN" in name: return "RSA"

    # Fallback: Scan row data for hints
    if "DRC REGION" in row_str: return "DRC"
    if "ZAM REGION" in row_str: return "ZAM"
    
    return "CROSS_BORDER"

# --- MAIN SYNC LOGIC ---

def process_sheet(sheet_id, sheet_name, workspace_name, entity):
    sheet = robust_api_call(ss_client.Sheets.get_sheet, sheet_id)
    if not sheet: return 0

    rows_payload = []
    col_map = {col.id: col.title for col in sheet.columns}

    # Determine Category (Fleet vs Other)
    category = "FLEET" if "TRACKING" in workspace_name.upper() else "OTHER"

    # --- DATE CANDIDATES LIST ---
    # The script will check these names in order and grab the first one it finds
    date_candidates = [
        "Date", "date", "Date tagged", "DATE BOOKING MADE", 
        "Loading Date", "Created", "Start Date", "Departure Date",
        "Booking Date", "Arrival Date", "ETA"
    ]

    for row in sheet.rows:
        row_data = {}
        core_data = {k: None for k in CORE_MAP.keys()}
        found_date = None # Reset date for each row
        
        # 1. Extract Data
        for cell in row.cells:
            if cell.column_id in col_map:
                col_name = col_map[cell.column_id]
                
                # Get value (Try Display Value first, fallback to raw Value)
                val = str(cell.display_value) if cell.display_value else (str(cell.value) if cell.value else None)
                
                row_data[col_name] = val
                
                # Check against Core Map (Reg, Status, etc.)
                for sql_col, aliases in CORE_MAP.items():
                    if col_name in aliases:
                        core_data[sql_col] = val
                
                # --- NEW: DATE HUNTING LOGIC ---
                # If we haven't found a date yet, AND this column is a candidate, AND it has a value
                if not found_date and val and col_name in date_candidates:
                    found_date = val

        # 2. Determine Country
        country = get_country(sheet_name, row_data)
        
        # 3. Create ID (Use Row ID to prevent crashing on duplicates)
        pk = str(row.id)

        # 4. Add to Batch
        if pk:
            rows_payload.append({
                "id": pk,
                "entity": entity,  
                "country": country,
                "workspace": workspace_name,
                "sheet_name": sheet_name,
                "category": category,
                "reg_number": core_data["reg_number"],
                "status": core_data["status"],
                "location": core_data["location"],
                "transporter": core_data["transporter"],
                "booking_date": found_date,  # <--- THIS SAVES THE DATE TO THE NEW COLUMN
                "raw_data": row_data,
                "last_updated": datetime.now().isoformat()
            })

    # 5. Upload to Supabase (Admin Mode)
    if rows_payload:
        try:
            # Batch upsert in chunks of 500
            for i in range(0, len(rows_payload), 500):
                batch = rows_payload[i:i+500]
                supabase.table("master_data").upsert(batch).execute()
            print(f"      ✅ [{entity}][{country}] Synced {len(rows_payload)} rows from {sheet_name}")
            return len(rows_payload)
        except Exception as e:
            # Clean Error Printing
            print(f"      ❌ Upload Error in {sheet_name}: {str(e)[:100]}")
            return 0
    return 0

def main():
    print("🚀 STARTING ADMIN SYNC (Secret Key Mode)...")
    workspaces = robust_api_call(ss_client.Workspaces.list_workspaces, include_all=True).data
    
    total_rows = 0
    for ws in workspaces:
        
        # --- STRICT ID FILTERING ---
        if ws.id in ADARS_IDS:
            entity = "ADARS"
        elif ws.id in WORLDRISK_IDS:
            entity = "WORLDRISK"
        else:
            # Skip irrelevant workspaces
            continue
        # ---------------------------

        print(f"📂 Processing [{entity}]: {ws.name}")
        full_ws = robust_api_call(ss_client.Workspaces.get_workspace, ws.id)
        if not full_ws: continue
        
        # Scan Sheets in Root
        for sheet in full_ws.sheets:
            total_rows += process_sheet(sheet.id, sheet.name, ws.name, entity)
        
        # Scan Sheets in Folders
        for folder in full_ws.folders:
            full_folder = robust_api_call(ss_client.Folders.get_folder, folder.id)
            if not full_folder: continue
            for sheet in full_folder.sheets:
                total_rows += process_sheet(sheet.id, sheet.name, ws.name, entity)

    print(f"🏁 DONE! Synced {total_rows} total rows.")

if __name__ == "__main__":
    main()