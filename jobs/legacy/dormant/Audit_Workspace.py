import os
import smartsheet
from dotenv import load_dotenv

# --- SETUP ---
load_dotenv()

# Initialize Smartsheet
ss_token = os.getenv('SMARTSHEET_ACCESS_TOKEN')
if not ss_token:
    print("❌ Error: SMARTSHEET_ACCESS_TOKEN not found in .env")
    exit(1)

ss_client = smartsheet.Smartsheet(ss_token)
ss_client.errors_as_exceptions = False 

def load_id_list(env_var):
    raw = os.getenv(env_var, "")
    return [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]

WORLDRISK_IDS = load_id_list('WORLDRISK_WORKSPACES')

def main():
    print("🔍 **STARTING SMARTSHEET AUDIT** 🔍")
    print(f"Checking Workspaces IDs: {WORLDRISK_IDS}\n")

    for ws_id in WORLDRISK_IDS:
        ws = ss_client.Workspaces.get_workspace(ws_id)
        
        if hasattr(ws, 'result') and hasattr(ws.result, 'code') and ws.result.code == 1006:
            print(f"❌ Error: Workspace ID {ws_id} not found or permission denied.")
            continue
            
        print(f"🌍 **WORKSPACE:** {ws.name} (ID: {ws.id})")
        print("="*60)

        # 1. CHECK "WORKSPACE ROOT" (Sheets not in folders)
        root_sheets = [s.name for s in ws.sheets]
        print(f"📂 **CATEGORY: Workspace Root** (Sheets directly in workspace)")
        if root_sheets:
            for sheet_name in root_sheets:
                # Check for Archives
                status = "🔴 SKIPPED (Archive)" if "ARCHIVE" in sheet_name.upper() else "✅ ACTIVE"
                print(f"   ├─ {status}: {sheet_name}")
        else:
            print("   └─ (No sheets found at root level)")
        print("")

        # 2. CHECK FOLDERS
        for folder in ws.folders:
            print(f"📂 **CATEGORY: {folder.name}**")
            
            # We must fetch the full folder to see its sheets
            full_folder = ss_client.Folders.get_folder(folder.id)
            
            if not full_folder.sheets:
                print("   └─ (Empty Folder)")
            
            for sheet in full_folder.sheets:
                status = "🔴 SKIPPED (Archive)" if "ARCHIVE" in sheet.name.upper() else "✅ ACTIVE"
                print(f"   ├─ {status}: {sheet.name}")
            print("")
        
        print("="*60 + "\n")

if __name__ == "__main__":
    main()