import os
import smartsheet
import logging
import traceback
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
SMARTSHEET_ACCESS_TOKEN = os.getenv('SMARTSHEET_ACCESS_TOKEN')
WORKSPACE_ID = 3802012660852612
MASTER_SHEET_ID = 8656520565706628 

# --- BOT KEYS ---
LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_TELEGRAM_ID')       # Direct Admin (Health)
RISK_LOG_ID = os.getenv('RISK_LOG_ID')          # World Risk Log Group

# --- SMART ALIAS MAPPING ---
COLUMN_ALIASES = {
    "Transporter": ["Transporter", "TRANSPORTER"],
    "Reg": ["Reg", "HORSE REG", "Horse Reg", "REG"],
    "Device": ["Device", "DEVICE NUMBER", "Device Number"],
    "Current Location": ["Current Location", "CURRENT LOCATION", "Location"],
    "Status": ["Status", "TRANSIT STATUS", "Transit Status"],
    "Escorts": ["Escort", "ESCORT NAME", "Escort Name", "ZAM REGION: ESCORT NAME", "DRC REGION: ESCORT NAME", "NAM REGION: ESCORT NAME"]
}

# --- COUNTRY LOGIC DICTIONARY ---
COUNTRY_LOOKUP = {
    "DURBAN": "South Africa",
    "RICHARDS BAY": "South Africa",
    "JOHANNESBURG": "South Africa",
    "JHB": "South Africa",
    "SOLWEZI": "Zambia",
    "NDOLA": "Zambia",
    "KITWE": "Zambia",
    "CHINGOLA": "Zambia",
    "LUSAKA": "Zambia",
    "CHIRUNDU": "Zambia/Zim Border",
    "KASUMBALESA": "DRC Border",
    "KOLWEZI": "DRC",
    "LUBUMBASHI": "DRC",
    "LIKASI": "DRC",
    "KAKANDA": "DRC",
    "FUNGURUME": "DRC",
    "DAR ES SALAAM": "Tanzania",
    "TUNDUMA": "Tanzania",
    "WALVIS BAY": "Namibia",
    "BEIRA": "Mozambique"
}

# Initialize Client
ss_client = smartsheet.Smartsheet(SMARTSHEET_ACCESS_TOKEN)
ss_client.errors_as_exceptions = True

# --- ROUTING HELPERS ---

def log_to_risk(msg):
    """Sends operational sync summaries to the Risk Log Group"""
    url = f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": RISK_LOG_ID, "text": msg, "parse_mode": "Markdown"})
    except:
        pass

def log_to_admin(msg):
    """Sends critical system crashes directly to Admin"""
    url = f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": ADMIN_ID, "text": msg, "parse_mode": "Markdown"})
    except:
        pass

def determine_country(location_text):
    if not location_text: return "Unknown"
    loc_upper = str(location_text).upper()
    for city, country in COUNTRY_LOOKUP.items():
        if city in loc_upper:
            return country
    return "Other"

def get_all_active_sheets(workspace_id):
    print("🔍 Scanning Workspace for active sheets...")
    workspace = ss_client.Workspaces.get_workspace(workspace_id)
    sheets_found = []
    
    def dive_into_folders(folders, parent_project_name):
        for folder in folders:
            if "archive" in folder.name.lower(): continue
            project_name = folder.name if parent_project_name == "Root" else parent_project_name
            try:
                f = ss_client.Folders.get_folder(folder.id)
                for s in f.sheets:
                    sheets_found.append({"id": s.id, "name": s.name, "project": project_name})
                if f.folders:
                    dive_into_folders(f.folders, project_name)
            except Exception:
                pass

    dive_into_folders(workspace.folders, "Root")
    return sheets_found

def sync_dashboard():
    print(f"🚀 --- STARTING DASHBOARD SYNC: {datetime.now().strftime('%H:%M:%S')} ---")
    
    # 1. Get Target Master Sheet Columns
    master_sheet = ss_client.Sheets.get_sheet(MASTER_SHEET_ID)
    master_col_map = {col.title: col.id for col in master_sheet.columns}
    
    # 2. Gather Data from All Source Sheets
    all_sheets = get_all_active_sheets(WORKSPACE_ID)
    rows_to_add = []
    
    for count, s_info in enumerate(all_sheets, 1):
        if str(s_info['id']) == str(MASTER_SHEET_ID): continue
        if "master" in s_info['name'].lower() or "dashboard" in s_info['name'].lower(): continue
        if "convoy breaks" in s_info['name'].lower(): continue
            
        try:
            sheet = ss_client.Sheets.get_sheet(s_info['id'])
            source_col_map = {col.id: col.title for col in sheet.columns}
            
            field_id_map = {}
            for target_name, aliases in COLUMN_ALIASES.items():
                for col_id, col_title in source_col_map.items():
                    if col_title in aliases:
                        field_id_map[target_name] = col_id
                        break
            
            for row in sheet.rows:
                row_vals = {}
                for target_field, col_id in field_id_map.items():
                    if col_id:
                        cell = next((c for c in row.cells if c.column_id == col_id), None)
                        row_vals[target_field] = str(cell.display_value) if cell and cell.display_value else "N/A"
                    else:
                        row_vals[target_field] = "N/A"
                
                status_val = row_vals.get("Status", "").lower()
                loc_val = row_vals.get("Current Location", "").lower()
                
                if "delivered" in status_val or "delivered" in loc_val: continue
                if row_vals.get("Reg") == "N/A" and row_vals.get("Transporter") == "N/A": continue

                derived_country = determine_country(row_vals.get("Current Location", ""))
                
                new_row = smartsheet.models.Row()
                new_row.to_bottom = True
                
                def create_cell(col_name, value):
                    return smartsheet.models.Cell({'column_id': master_col_map[col_name], 'value': value})

                new_row.cells.append(create_cell("Project Name", s_info['project']))
                new_row.cells.append(create_cell("Source Sheet", sheet.name))
                new_row.cells.append(create_cell("Transporter", row_vals.get("Transporter")))
                new_row.cells.append(create_cell("Reg", row_vals.get("Reg")))
                new_row.cells.append(create_cell("Device", row_vals.get("Device")))
                new_row.cells.append(create_cell("Current Location", row_vals.get("Current Location")))
                new_row.cells.append(create_cell("Derived Country", derived_country))
                new_row.cells.append(create_cell("Status", row_vals.get("Status")))
                new_row.cells.append(create_cell("Escorts", row_vals.get("Escorts")))
                new_row.cells.append(create_cell("Last Updated", row.modified_at.strftime('%Y-%m-%d %H:%M')))
                
                rows_to_add.append(new_row)

        except Exception:
            continue

    # 3. WIPE AND REPLACE
    if master_sheet.rows:
        row_ids = [r.id for r in master_sheet.rows]
        for i in range(0, len(row_ids), 300):
            ss_client.Sheets.delete_rows(MASTER_SHEET_ID, row_ids[i:i+300])

    if rows_to_add:
        for i in range(0, len(rows_to_add), 300):
            chunk = rows_to_add[i:i+300]
            ss_client.Sheets.add_rows(MASTER_SHEET_ID, chunk)

    # SUCCESS SUMMARY -> Risk Log Group
    log_to_risk(f"✅ *Dashboard Sync:* Successfully pushed `{len(rows_to_add)}` active loads to Master Sheet.")

if __name__ == "__main__":
    try:
        sync_dashboard()
    except Exception as e:
        # CRASH -> Admin Direct DM
        err_msg = traceback.format_exc()
        log_to_admin(f"🚨 **CRASH:** `sync_dashboard.py` \n❌ `{str(e)}` \n\n`{err_msg}`")