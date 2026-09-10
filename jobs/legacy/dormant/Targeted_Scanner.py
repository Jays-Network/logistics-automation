import os
import smartsheet
import time
from dotenv import load_dotenv

# --- SETUP ---
load_dotenv()
SMARTSHEET_ACCESS_TOKEN = os.getenv('SMARTSHEET_ACCESS_TOKEN')

if not SMARTSHEET_ACCESS_TOKEN:
    print("🚨 Error: SMARTSHEET_ACCESS_TOKEN is missing from .env")
    exit(1)

ss_client = smartsheet.Smartsheet(SMARTSHEET_ACCESS_TOKEN)
ss_client.errors_as_exceptions = False

# --- LOAD TARGET IDs FROM .ENV ---
def load_id_list(env_var):
    raw = os.getenv(env_var, "")
    if not raw: return []
    # Clean up commas and ensure they are integers
    return [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]

# Combine both lists so we scan everything relevant in one go
ADARS_IDS = load_id_list('ADARS_WORKSPACES')
WORLDRISK_IDS = load_id_list('WORLDRISK_WORKSPACES')
TARGET_IDS = list(set(ADARS_IDS + WORLDRISK_IDS)) # Remove duplicates just in case

# Stats
global_stats = {
    "workspaces_scanned": 0,
    "total_sheets": 0,
    "columns": {}
}

def scan_container(container_obj, container_name):
    """Recursively scans folders and sheets inside a workspace/folder"""
    
    # 1. Scan Sheets in this container
    for sheet_ref in container_obj.sheets:
        global_stats["total_sheets"] += 1
        print(f"   📄 Scanning Sheet: {sheet_ref.name}...")
        
        # Get sheet details (just columns, no rows needed yet)
        sheet = ss_client.Sheets.get_sheet(sheet_ref.id, page_size=1)
        
        # Rate Limit Handling (Error 4003 or 4001)
        if hasattr(sheet, 'result') and (sheet.result.code == 4001 or sheet.result.code == 4003):
            print("      ⚠️ Rate Limit Hit - Sleeping 10s...")
            time.sleep(10)
            sheet = ss_client.Sheets.get_sheet(sheet_ref.id, page_size=1)
            
        if not sheet or not hasattr(sheet, 'columns'): 
            continue

        for col in sheet.columns:
            c_name = col.title.strip()
            if c_name not in global_stats["columns"]:
                global_stats["columns"][c_name] = 0
            global_stats["columns"][c_name] += 1

    # 2. Scan Sub-Folders recursively
    for folder_ref in container_obj.folders:
        folder = ss_client.Folders.get_folder(folder_ref.id)
        scan_container(folder, f"{container_name} > {folder.name}")

# --- MAIN EXECUTION ---
print("🚀 STARTING TARGETED SCAN...")
print(f"🎯 Loading IDs from .env...")
print(f"   - ADARS Workspaces: {len(ADARS_IDS)}")
print(f"   - WORLDRISK Workspaces: {len(WORLDRISK_IDS)}")
print(f"   - TOTAL TARGETS: {len(TARGET_IDS)}")

if len(TARGET_IDS) == 0:
    print("❌ No IDs found in .env! Please check your ADARS_WORKSPACES and WORLDRISK_WORKSPACES variables.")
    exit()

for i, ws_id in enumerate(TARGET_IDS):
    try:
        # Get the full workspace directly using the ID
        full_workspace = ss_client.Workspaces.get_workspace(ws_id)
        
        if hasattr(full_workspace, 'result') and full_workspace.result.code == 404:
            print(f"\n[{i+1}/{len(TARGET_IDS)}] ❌ Workspace ID {ws_id} NOT FOUND (Skipping)")
            continue
            
        print(f"\n[{i+1}/{len(TARGET_IDS)}] 📂 Workspace: {full_workspace.name}")
        scan_container(full_workspace, full_workspace.name)
        global_stats["workspaces_scanned"] += 1
        
    except Exception as e:
        print(f"❌ Error scanning Workspace ID {ws_id}: {e}")

# --- REPORT ---
print("\n" + "="*50)
print(f"✅ FINAL REPORT")
print(f"   Workspaces Scanned: {global_stats['workspaces_scanned']}")
print(f"   Sheets Scanned:     {global_stats['total_sheets']}")
print(f"   Unique Column Names: {len(global_stats['columns'])}")
print("="*50)

# Sort by frequency (Most common columns first)
sorted_cols = sorted(global_stats["columns"].items(), key=lambda x: x[1], reverse=True)

print(f"{'COLUMN NAME':<50} | {'FOUND IN X SHEETS'}")
print("-" * 75)
for name, count in sorted_cols[:100]:  # Print top 100
    print(f"{name:<50} | {count}")