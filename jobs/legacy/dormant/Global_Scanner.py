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

# Stats
global_stats = {
    "total_workspaces": 0,
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
        if hasattr(sheet, 'result') and sheet.result.code == 4001:
            print("      ⚠️ Rate Limit Hit - Sleeping 5s...")
            time.sleep(5)
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
print("🚀 STARTING GLOBAL SCAN (This may take a while)...")

# Get ALL Workspaces (Pagination handling included automatically by SDK usually, but explicit is safer)
workspaces_response = ss_client.Workspaces.list_workspaces(include_all=True)
all_workspaces = workspaces_response.data

global_stats["total_workspaces"] = len(all_workspaces)
print(f"found {len(all_workspaces)} Workspaces to scan.")

for i, ws_ref in enumerate(all_workspaces):
    print(f"\n[{i+1}/{len(all_workspaces)}] 📂 Workspace: {ws_ref.name}")
    
    # Skip irrelevant workspaces if you want (Optional)
    # if "Archive" in ws_ref.name: continue 

    try:
        full_workspace = ss_client.Workspaces.get_workspace(ws_ref.id)
        scan_container(full_workspace, ws_ref.name)
    except Exception as e:
        print(f"❌ Error scanning {ws_ref.name}: {e}")

# --- REPORT ---
print("\n" + "="*50)
print(f"✅ FINAL REPORT")
print(f"   Workspaces Scanned: {global_stats['total_workspaces']}")
print(f"   Sheets Scanned:     {global_stats['total_sheets']}")
print(f"   Unique Column Names: {len(global_stats['columns'])}")
print("="*50)

# Sort by frequency (Most common columns first)
sorted_cols = sorted(global_stats["columns"].items(), key=lambda x: x[1], reverse=True)

print(f"{'COLUMN NAME':<50} | {'FOUND IN X SHEETS'}")
print("-" * 75)
for name, count in sorted_cols[:100]:  # Print top 100
    print(f"{name:<50} | {count}")