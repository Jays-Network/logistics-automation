import os
import smartsheet
import time
import requests
import fcntl
import traceback
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from pathlib import Path

# --- ROBUST CONFIGURATION LOADER ---
# 1. Find the folder where THIS script is located
base_dir = Path(__file__).resolve().parent

# 2. Force load .env from that same folder
env_path = base_dir / '.env'

# 3. If not found, look ONE LEVEL UP (useful if script is in a subfolder)
if not env_path.exists():
    env_path = base_dir.parent / '.env'

if env_path.exists():
    print(f"📂 Loading .env from: {env_path}")
    load_dotenv(dotenv_path=env_path)
else:
    print(f"❌ Warning: .env file not found at {env_path}")

# --- CONFIGURATION ---
SMARTSHEET_ACCESS_TOKEN = os.getenv('SMARTSHEET_ACCESS_TOKEN')
SOURCE_WORKSPACE_ID = 3802012660852612
ANALYTICS_WORKSPACE_ID = 2855773450594180
STATS_SHEET_ID = 4437887723458436

# Safety Check
if not SMARTSHEET_ACCESS_TOKEN:
    print("🚨 CRITICAL ERROR: SMARTSHEET_ACCESS_TOKEN is missing or empty.")
    exit(1)

# --- BOT KEYS ---
LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_TELEGRAM_ID')       # Direct Admin (Health)
RISK_LOG_ID = os.getenv('RISK_LOG_ID')          # World Risk Log Group

LOOKBACK_MINUTES = 7 

COLUMN_ALIASES = {
    "Transporter": ["Transporter", "TRANSPORTER"],
    "Reg": ["Reg", "HORSE REG", "Horse Reg", "REG"],
    "Device": ["Device", "DEVICE NUMBER", "Device Number", "DEVICE"],
    "Current Location": ["Current Location", "CURRENT LOCATION", "Location"],
    "Status": ["Status", "TRANSIT STATUS", "Transit Status"],
    "Escorts": ["Escort", "ESCORT NAME", "Escort Name", "ZAM REGION: ESCORT NAME", "DRC REGION: ESCORT NAME"]
}

ss_client = smartsheet.Smartsheet(SMARTSHEET_ACCESS_TOKEN)
ss_client.errors_as_exceptions = False 

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

def safe_api_call(func, *args, **kwargs):
    while True:
        result = func(*args, **kwargs)
        if isinstance(result, smartsheet.models.Error):
            if result.result.error_code == 4001:
                time.sleep(5)
                continue
            return None
        return result

def determine_country(location_text):
    if not location_text or location_text == "N/A": return "Unknown"
    loc = str(location_text).upper()
    if any(k in loc for k in ["DURBAN", "JHB", "SOUTH AFRICA", "SA"]): return "South Africa"
    if any(k in loc for k in ["SOLWEZI", "LUSAKA", "NDOLA", "ZAMBIA", "ZAM"]): return "Zambia"
    if any(k in loc for k in ["KOLWEZI", "LUBUMBASHI", "DRC", "KASUMBALESA", "KISANFU"]): return "DRC"
    if any(k in loc for k in ["DAR ES SALAAM", "TANZANIA", "TAN"]): return "Tanzania"
    if any(k in loc for k in ["WALVIS BAY", "NAMIBIA", "NAM"]): return "Namibia"
    return "Other"

def update_global_stats(total_count, country_counts):
    print(f"📊 Summarizing {total_count} trucks for Dashboard...")
    stats_sheet = safe_api_call(ss_client.Sheets.get_sheet, STATS_SHEET_ID)
    if not stats_sheet: return

    primary_col_id = stats_sheet.columns[0].id
    value_col_id = next((col.id for col in stats_sheet.columns if col.title == "Value"), stats_sheet.columns[1].id)
    
    metrics_to_push = {"Total Active Trucks": total_count}
    metrics_to_push.update(country_counts)

    rows_to_update = []
    found_metrics = set()

    for row in stats_sheet.rows:
        metric_name = row.cells[0].display_value
        if metric_name in metrics_to_push:
            new_row = smartsheet.models.Row({'id': row.id})
            new_row.cells.append({'column_id': value_col_id, 'value': metrics_to_push[metric_name]})
            rows_to_update.append(new_row)
            found_metrics.add(metric_name)
    
    if rows_to_update:
        safe_api_call(ss_client.Sheets.update_rows, STATS_SHEET_ID, rows_to_update)

    missing_metrics = [m for m in metrics_to_push if m not in found_metrics]
    if missing_metrics:
        new_rows = []
        for name in missing_metrics:
            new_rows.append(smartsheet.models.Row({
                'to_bottom': True,
                'cells': [
                    {'column_id': primary_col_id, 'value': name},
                    {'column_id': value_col_id, 'value': metrics_to_push[name]}
                ]
            }))
        safe_api_call(ss_client.Sheets.add_rows, STATS_SHEET_ID, new_rows)
    
    print("✅ Summary Stats Table Refreshed.")

# --- MAIN ENGINE ---

def sync_analytics():
    print(f"🚀 SYNC START: {datetime.now().strftime('%H:%M:%S')}")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    
    source_ws = safe_api_call(ss_client.Workspaces.get_workspace, SOURCE_WORKSPACE_ID)
    analytics_ws = safe_api_call(ss_client.Workspaces.get_workspace, ANALYTICS_WORKSPACE_ID)
    
    if not source_ws:
        print(f"❌ Error: Could not load Source Workspace (ID: {SOURCE_WORKSPACE_ID})")
        return

    if not analytics_ws:
        print(f"❌ Error: Could not load Analytics Workspace (ID: {ANALYTICS_WORKSPACE_ID})")
        return

    project_changes = {}
    change_count = 0

    def scan_folders(folders, current_proj):
        nonlocal change_count
        for f in folders:
            proj = f.name if current_proj == "Root" else current_proj
            folder_obj = safe_api_call(ss_client.Folders.get_folder, f.id)
            if not folder_obj: continue 

            for s_info in folder_obj.sheets:
                if hasattr(s_info, 'modified_at') and s_info.modified_at < cutoff: continue
                
                sheet = safe_api_call(ss_client.Sheets.get_sheet, s_info.id)
                if not sheet: continue
                col_map = {col.title: col.id for col in sheet.columns}
                if proj not in project_changes: project_changes[proj] = []

                for row in sheet.rows:
                    data = {}
                    for target, aliases in COLUMN_ALIASES.items():
                        source_col_id = next((col_map[a] for a in aliases if a in col_map), None)
                        cell = next((c for c in row.cells if c.column_id == source_col_id), None) if source_col_id else None
                        data[target] = str(cell.display_value) if cell and cell.display_value else "N/A"

                    if data["Reg"] == "N/A" and data["Transporter"] == "N/A": continue

                    project_changes[proj].append({
                        "Project Name": proj, "Source Sheet": sheet.name, **data,
                        "Derived Country": determine_country(data["Current Location"]),
                        "Last Updated": row.modified_at.strftime('%H:%M')
                    })
                    change_count += 1
            if folder_obj.folders: scan_folders(folder_obj.folders, proj)

    if source_ws.folders:
        scan_folders(source_ws.folders, "Root")

    if project_changes:
        for proj_name, row_list in project_changes.items():
            master_id = next((s.id for s in analytics_ws.sheets if s.name == f"{proj_name}_MASTER"), None)
            if not master_id: continue
            master_sheet = safe_api_call(ss_client.Sheets.get_sheet, master_id)
            if not master_sheet: continue
            m_col_map = {col.title: col.id for col in master_sheet.columns}

            if master_sheet.rows:
                ids = [r.id for r in master_sheet.rows]
                for i in range(0, len(ids), 300):
                    safe_api_call(ss_client.Sheets.delete_rows, master_id, ids[i:i+300])

            new_rows = [smartsheet.models.Row({'to_bottom': True, 'cells': [{'column_id': m_col_map[k], 'value': v} for k, v in r.items()]}) for r in row_list]
            for i in range(0, len(new_rows), 300):
                safe_api_call(ss_client.Sheets.add_rows, master_id, new_rows[i:i+300])
        
        # Operational summary -> Risk Log Group
        log_to_risk(f"✅ *Sync Analytics:* {change_count} rows across {len(project_changes)} projects updated.")

    total_fleet = 0
    country_summary = {"South Africa": 0, "Zambia": 0, "DRC": 0, "Tanzania": 0, "Namibia": 0, "Other": 0}
    
    updated_ws = safe_api_call(ss_client.Workspaces.get_workspace, ANALYTICS_WORKSPACE_ID)
    
    # --- CRITICAL FIX: Safety Check for Workspace ---
    if updated_ws:
        for s_info in updated_ws.sheets:
            if "_MASTER" in s_info.name:
                m_sheet = safe_api_call(ss_client.Sheets.get_sheet, s_info.id)
                if m_sheet:
                    total_fleet += len(m_sheet.rows)
                    
                    country_col_id = next((c.id for c in m_sheet.columns if c.title == "Derived Country"), None)
                    if country_col_id:
                        for r in m_sheet.rows:
                            val = next((c.display_value for c in r.cells if c.column_id == country_col_id), "Other")
                            if val in country_summary:
                                country_summary[val] += 1
                            else:
                                country_summary["Other"] += 1
    else:
        print(f"❌ Error: Failed to retrieve Analytics Workspace for stats calculation.")
    
    update_global_stats(total_fleet, country_summary)
    print(f"🏁 Sync Finished: {datetime.now().strftime('%H:%M:%S')}")

# --- EXECUTION ---

if __name__ == "__main__":
    f = open(os.path.realpath(__file__), 'r')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except:
        os._exit(0)

    try:
        sync_analytics()
    except Exception as e:
        # System crash -> Direct Admin health check
        err_msg = traceback.format_exc()
        log_to_admin(f"🚨 *CRASH:* `sync_analytics.py` \n❌ `{str(e)}` \n\n`{err_msg}`")