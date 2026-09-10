import sys
import os
import time
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone, timedelta

SAST = timezone(timedelta(hours=2))  # South Africa Standard Time — fixed UTC+2, no DST
from dotenv import load_dotenv
import smartsheet
from license_manager import JaysNetworkLicense

# ================= SETUP =================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')  # Main ops bot — posts to the main group
LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN')             # Log/admin bot — posts to admin DMs only
DELETE_WATCH_GROUP_ID = "-1004449570420"  # Main notifications group for this bot
DELETE_ALERT_ID = os.getenv('DELETE_ALERT_ID', DELETE_WATCH_GROUP_ID)
HOURLY_REPORT_ID = os.getenv('DELETE_WATCH_HOURLY_ID', DELETE_WATCH_GROUP_ID)
ADMIN_ID = os.getenv('ADMIN_TELEGRAM_ID')
LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/deletion_watch.lock"

def send_telegram(token, chat_id, message):
    if not token or not chat_id:
        print("⚠️ Telegram token/chat_id missing, skipping alert.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to send Telegram: {e}")

# --- License / DB handshake ---
print("🔐 Authenticating with Gatekeeper...")
license_key = os.getenv('LICENSE_KEY') or os.getenv('JAYS_NETWORK_LICENSE_KEY')
if not license_key:
    send_telegram(LOG_BOT_TOKEN, ADMIN_ID, "❌ *DELETE-WATCH ERROR*: License key missing in .env")
    exit(1)

auth_system = JaysNetworkLicense(license_key)
credentials = auth_system.authenticate()
if not credentials:
    send_telegram(LOG_BOT_TOKEN, ADMIN_ID, "⛔ *DELETE-WATCH ACCESS DENIED*: License validation failed.")
    exit(1)

def get_db_conn():
    return psycopg2.connect(
        host=credentials["url"],
        user=credentials["key"],
        password=credentials["pass"],
        database="jaysnet_data",
        port="5432"
    )

ss_client = smartsheet.Smartsheet(os.getenv('SMARTSHEET_ACCESS_TOKEN'))
ss_client.errors_as_exceptions = False

EXCLUDED_FOLDERS = {"cross border archives", "zzz archive"}

def load_id_list(env_var):
    raw = os.getenv(env_var, "")
    return [int(x.strip()) for x in raw.split(',') if x.strip().isdigit()]

WORLDRISK_IDS = load_id_list('WORLDRISK_WORKSPACES')

COLUMN_MAPPING = {
    "transporter": ["Transporter", "TRANSPORTER", "Haulier", "Sub-Contractor"],
    "reg_number": ["Reg", "HORSE REG", "REG", "Registration", "Reference", "Vehicle Reg"],
    "device_number": ["Device No", "Device", "DEVICE NUMBER", "Device ID"],
    "current_location": ["Current Location", "CURRENT LOCATION", "Position", "Check Point"],
    "status": ["Status", "TRANSIT STATUS", "Transit Status", "Current Status"],
}

def robust_api_call(func, *args, **kwargs):
    attempts = 0
    while attempts < 3:
        try:
            response = func(*args, **kwargs)
            if hasattr(response, 'result') and hasattr(response.result, 'code') and response.result.code == 4003:
                print("   ⚠️ Rate limit, cooling down 60s...")
                time.sleep(60)
                attempts += 1
                continue
            if isinstance(response, smartsheet.models.Error):
                print(f"   ⚠️ Smartsheet API error: {getattr(response.result, 'message', 'Unknown')}")
                return None
            return response
        except Exception as e:
            print(f"   ⚠️ Network/API exception: {e}")
            return None
    return None

def normalize_modified_at(val):
    if not val:
        return None
    if isinstance(val, datetime):
        dt = val
    else:
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def collect_sheet_stubs(ws):
    """
    Metadata-only pass — no get_sheet calls. Returns (id, name, modified_at, is_archive)
    for every sheet in the workspace, active + archive. This is essentially free: the
    modified_at we need is already present on the lite sheet objects returned by
    get_workspace / get_folder.
    """
    stubs = []

    def add(sheet_lite, is_archive_folder):
        is_archive = is_archive_folder or "ARCHIVE" in sheet_lite.name.upper()
        stubs.append((
            str(sheet_lite.id),
            sheet_lite.name,
            normalize_modified_at(getattr(sheet_lite, 'modified_at', None)),
            is_archive
        ))

    for s in ws.sheets:
        add(s, is_archive_folder=False)

    for folder in ws.folders:
        is_archive_folder = folder.name.strip().lower() in EXCLUDED_FOLDERS
        full_folder = robust_api_call(ss_client.Folders.get_folder, folder.id)
        if not full_folder:
            continue
        for s in full_folder.sheets:
            add(s, is_archive_folder=is_archive_folder)

    return stubs

def extract_minimal_rows(sheet):
    field_to_col_id = {}
    for db_field, candidates in COLUMN_MAPPING.items():
        for col in sheet.columns:
            if col.title in candidates:
                field_to_col_id[db_field] = col.id
                break

    live_rows = {}
    for row in sheet.rows:
        row_id = str(row.id)
        vals = {k: None for k in COLUMN_MAPPING.keys()}
        for cell in row.cells:
            for db_field, target_id in field_to_col_id.items():
                if cell.column_id == target_id:
                    vals[db_field] = str(cell.display_value or cell.value or "") or None
        live_rows[row_id] = vals
    return live_rows

# ================= DB STATE HELPERS =================

def get_known_states(cur, sheet_ids):
    if not sheet_ids:
        return {}
    cur.execute(
        "SELECT sheet_id, last_modified_at FROM deletion_watch_sheet_state WHERE sheet_id = ANY(%s)",
        (sheet_ids,)
    )
    return {r['sheet_id']: r['last_modified_at'] for r in cur.fetchall()}

def update_sheet_state(cur, sheet_id, sheet_name, is_archive, modified_at):
    cur.execute(
        """INSERT INTO deletion_watch_sheet_state (sheet_id, sheet_name, is_archive, last_modified_at, last_checked_at)
           VALUES (%s, %s, %s, %s, now())
           ON CONFLICT (sheet_id) DO UPDATE SET
               sheet_name = EXCLUDED.sheet_name,
               is_archive = EXCLUDED.is_archive,
               last_modified_at = EXCLUDED.last_modified_at,
               last_checked_at = now()""",
        (sheet_id, sheet_name, is_archive, modified_at)
    )

def refresh_archive_snapshot(cur, sheet_id, live_rows):
    cur.execute("DELETE FROM worldrisk_archive_snapshot WHERE sheet_id = %s", (sheet_id,))
    if live_rows:
        data = [
            (sheet_id, rid, v["reg_number"], v["device_number"])
            for rid, v in live_rows.items()
        ]
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO worldrisk_archive_snapshot (sheet_id, row_id, reg_number, device_number) VALUES %s",
            data
        )

def get_archive_index(cur, archive_sheet_ids):
    if not archive_sheet_ids:
        return set(), set()
    cur.execute(
        "SELECT reg_number, device_number FROM worldrisk_archive_snapshot WHERE sheet_id = ANY(%s)",
        (archive_sheet_ids,)
    )
    regs, devices = set(), set()
    for r in cur.fetchall():
        if r['reg_number']:
            regs.add(r['reg_number'].strip().upper())
        if r['device_number']:
            devices.add(r['device_number'].strip().upper())
    return regs, devices

# ================= MAIN LOGIC =================

def check_active_sheet(cur, sheet_id, sheet_name, ws_name, archive_regs, archive_devices):
    sheet = robust_api_call(ss_client.Sheets.get_sheet, sheet_id)
    if not sheet:
        return None  # fetch failed — caller must NOT mark this sheet as checked

    live_rows = extract_minimal_rows(sheet)
    live_ids = set(live_rows.keys())

    cur.execute(
        """SELECT row_id, reg_number, device_number, transporter, current_location, status, last_seen_at
           FROM worldrisk_deletion_watch WHERE sheet_id = %s""",
        (sheet_id,)
    )
    known_rows = {r['row_id']: r for r in cur.fetchall()}
    known_ids = set(known_rows.keys())

    missing_ids = known_ids - live_ids

    archived_ids, deleted_ids = set(), set()
    for rid in missing_ids:
        r = known_rows[rid]
        reg = (r['reg_number'] or "").strip().upper()
        device = (r['device_number'] or "").strip().upper()
        if (reg and reg in archive_regs) or (device and device in archive_devices):
            archived_ids.add(rid)
        else:
            deleted_ids.add(rid)

    if deleted_ids:
        detected_at = datetime.now(SAST).strftime('%Y-%m-%d %H:%M SAST')
        lines = []
        for rid in deleted_ids:
            r = known_rows[rid]
            if r['last_seen_at']:
                ls = r['last_seen_at']
                if ls.tzinfo is None:
                    ls = ls.replace(tzinfo=timezone.utc)  # stored value is UTC-naive
                last_seen = ls.astimezone(SAST).strftime('%Y-%m-%d %H:%M SAST')
            else:
                last_seen = 'N/A'
            lines.append(
                f"• Reg: `{r['reg_number'] or 'N/A'}` | Device: `{r['device_number'] or 'N/A'}` | "
                f"Transporter: `{r['transporter'] or 'N/A'}` | Last Loc: `{r['current_location'] or 'N/A'}` | "
                f"Status: `{r['status'] or 'N/A'}`\n"
                f"   Last confirmed present: `{last_seen}`"
            )
        sheet_url = f"https://app.smartsheet.com/sheets/{sheet_id}"
        msg = (
            f"🗑️ *ROW(S) DELETED*\n"
            f"Workspace: `{ws_name}`\n"
            f"Sheet: [{sheet_name}]({sheet_url})\n"
            f"Count: `{len(deleted_ids)}`\n"
            f"Detected at: `{detected_at}`\n\n" + "\n".join(lines)
        )
        send_telegram(TELEGRAM_BOT_TOKEN, DELETE_ALERT_ID, msg)

    all_missing = archived_ids | deleted_ids
    if all_missing:
        cur.execute(
            "DELETE FROM worldrisk_deletion_watch WHERE sheet_id = %s AND row_id = ANY(%s)",
            (sheet_id, list(all_missing))
        )

    if live_rows:
        upsert_data = [
            (sheet_id, rid, sheet_name, ws_name,
             v["reg_number"], v["device_number"], v["transporter"],
             v["status"], v["current_location"])
            for rid, v in live_rows.items()
        ]
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO worldrisk_deletion_watch
               (sheet_id, row_id, sheet_name, workspace_name, reg_number, device_number,
                transporter, status, current_location, last_seen_at)
               VALUES %s
               ON CONFLICT (sheet_id, row_id) DO UPDATE SET
                   sheet_name = EXCLUDED.sheet_name,
                   workspace_name = EXCLUDED.workspace_name,
                   reg_number = EXCLUDED.reg_number,
                   device_number = EXCLUDED.device_number,
                   transporter = EXCLUDED.transporter,
                   status = EXCLUDED.status,
                   current_location = EXCLUDED.current_location,
                   last_seen_at = now()""",
            upsert_data,
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
        )

    return len(deleted_ids), len(archived_ids)

def process_workspace(cur, ws):
    stubs = collect_sheet_stubs(ws)
    if not stubs:
        return 0, 0, 0, 0

    all_ids = [s[0] for s in stubs]
    known_states = get_known_states(cur, all_ids)

    def has_changed(sheet_id, modified_at):
        last_seen = known_states.get(sheet_id)
        if last_seen is None or modified_at is None:
            return True  # never checked before, or Smartsheet gave no timestamp — be safe
        return modified_at > last_seen

    active_stubs = [s for s in stubs if not s[3]]
    archive_stubs = [s for s in stubs if s[3]]

    active_changed = [s for s in active_stubs if has_changed(s[0], s[2])]
    archive_changed = [s for s in archive_stubs if has_changed(s[0], s[2])]

    active_skipped = len(active_stubs) - len(active_changed)
    archive_skipped = len(archive_stubs) - len(archive_changed)

    # 1. Refresh archive snapshot for any archive sheet that changed
    for sheet_id, sheet_name, modified_at, _ in archive_changed:
        sheet = robust_api_call(ss_client.Sheets.get_sheet, sheet_id)
        if not sheet:
            continue
        live_rows = extract_minimal_rows(sheet)
        refresh_archive_snapshot(cur, sheet_id, live_rows)
        update_sheet_state(cur, sheet_id, sheet_name, True, modified_at)

    # 2. Build the full archive index for this workspace (DB read only, no API cost)
    archive_sheet_ids = [s[0] for s in archive_stubs]
    archive_regs, archive_devices = get_archive_index(cur, archive_sheet_ids)

    # 3. Check only the active sheets that changed
    total_deleted = 0
    total_archived = 0
    total_failed = 0
    total_checked = 0
    for sheet_id, sheet_name, modified_at, _ in active_changed:
        result = check_active_sheet(
            cur, sheet_id, sheet_name, ws.name, archive_regs, archive_devices
        )
        if result is None:
            # Fetch failed (rate limit exhausted, 503, etc.) — do NOT update state,
            # so this sheet gets retried on the next run instead of being silently
            # marked "checked" while never actually diffed.
            total_failed += 1
            print(f"   ⚠️ {sheet_name}: fetch failed, will retry next run")
            continue

        deleted_count, archived_count = result
        update_sheet_state(cur, sheet_id, sheet_name, False, modified_at)
        total_deleted += deleted_count
        total_archived += archived_count
        total_checked += 1
        print(f"   ✅ {sheet_name}: {deleted_count} deletion(s), {archived_count} archived")

    return total_deleted, total_archived, active_skipped, archive_skipped, total_failed, total_checked

# ================= HOURLY REPORT HELPERS =================

def get_hour_bucket(dt):
    return dt.replace(minute=0, second=0, microsecond=0)

def upsert_hourly_stats(cur, hour_bucket, sheets_checked, deletions_found, archived_found):
    cur.execute(
        """INSERT INTO deletion_watch_hourly_stats (hour_bucket, runs_count, sheets_checked, deletions_found, archived_found)
           VALUES (%s, 1, %s, %s, %s)
           ON CONFLICT (hour_bucket) DO UPDATE SET
               runs_count = deletion_watch_hourly_stats.runs_count + 1,
               sheets_checked = deletion_watch_hourly_stats.sheets_checked + EXCLUDED.sheets_checked,
               deletions_found = deletion_watch_hourly_stats.deletions_found + EXCLUDED.deletions_found,
               archived_found = deletion_watch_hourly_stats.archived_found + EXCLUDED.archived_found""",
        (hour_bucket, sheets_checked, deletions_found, archived_found)
    )

def get_report_state(cur, key):
    cur.execute("SELECT value FROM deletion_watch_report_state WHERE key = %s", (key,))
    row = cur.fetchone()
    return row['value'] if row else None

def set_report_state(cur, key, value):
    cur.execute(
        """INSERT INTO deletion_watch_report_state (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, value)
    )

def maybe_send_hourly_report(cur):
    """
    Fires once per hour, on the first run after the hour rolls over, reporting
    stats for the hour that just completed. First-ever run just sets a baseline
    (nothing to report yet, since no full hour has passed).
    """
    now = datetime.now(timezone.utc)
    current_hour = get_hour_bucket(now)
    last_reported_str = get_report_state(cur, 'last_hourly_report')

    if last_reported_str is None:
        set_report_state(cur, 'last_hourly_report', current_hour.isoformat())
        return

    last_reported_hour = datetime.fromisoformat(last_reported_str)
    if current_hour <= last_reported_hour:
        return  # still within the same hour — nothing to report yet

    prev_hour = current_hour - timedelta(hours=1)
    prev_hour_display = prev_hour.astimezone(SAST)
    current_hour_display = current_hour.astimezone(SAST)
    cur.execute(
        """SELECT runs_count, sheets_checked, deletions_found, archived_found
           FROM deletion_watch_hourly_stats WHERE hour_bucket = %s""",
        (prev_hour,)
    )
    row = cur.fetchone()

    if row:
        status_line = (
            "✅ All clear — nothing deleted."
            if row['deletions_found'] == 0
            else f"⚠️ {row['deletions_found']} deletion(s) were found this hour."
        )
        report = (
            f"🕐 *Hourly Delete-Watch Report*\n"
            f"Window: `{prev_hour_display.strftime('%H:%M')} - {current_hour_display.strftime('%H:%M')} SAST`\n"
            f"{status_line}\n"
            f"Checks run: `{row['runs_count']}`\n"
            f"Sheets checked: `{row['sheets_checked']}`\n"
            f"Archived (not deleted): `{row['archived_found']}`"
        )
    else:
        report = (
            f"🕐 *Hourly Delete-Watch Report*\n"
            f"Window: `{prev_hour_display.strftime('%H:%M')} - {current_hour_display.strftime('%H:%M')} SAST`\n"
            f"⚠️ No runs recorded this hour — check cron / script health."
        )

    send_telegram(TELEGRAM_BOT_TOKEN, HOURLY_REPORT_ID, report)
    set_report_state(cur, 'last_hourly_report', current_hour.isoformat())

def main():
    start_time = time.time()
    print(f"🚀 DELETE-WATCH START: {datetime.now().strftime('%H:%M:%S')}")

    total_deleted = 0
    total_archived = 0
    total_active_skipped = 0
    total_archive_skipped = 0
    total_failed = 0
    total_checked = 0

    conn = get_db_conn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        for ws_id in WORLDRISK_IDS:
            ws = robust_api_call(ss_client.Workspaces.get_workspace, ws_id)
            if not ws:
                continue
            print(f"📂 Workspace: {ws.name}")

            try:
                deleted, archived, active_skipped, archive_skipped, failed, checked = process_workspace(cur, ws)
                conn.commit()
                total_deleted += deleted
                total_archived += archived
                total_active_skipped += active_skipped
                total_archive_skipped += archive_skipped
                total_failed += failed
                total_checked += checked
                print(f"   ⏩ Skipped (unchanged): {active_skipped} active, {archive_skipped} archive")
            except Exception as e:
                conn.rollback()
                print(f"   ❌ Error processing workspace {ws.name}: {e}")

        # Feed this run's numbers into the current hour's rolling counter
        hour_bucket = get_hour_bucket(datetime.now(timezone.utc))
        upsert_hourly_stats(cur, hour_bucket, total_checked, total_deleted, total_archived)
        conn.commit()

        duration = round(time.time() - start_time, 2)
        summary = (
            f"🏁 *Delete-Watch Complete*\n"
            f"🗑️ Deletions Found: `{total_deleted}`\n"
            f"📦 Archived (not deleted): `{total_archived}`\n"
            f"✅ Sheets Checked: `{total_checked}`\n"
            f"⏩ Sheets Skipped (unchanged): `{total_active_skipped + total_archive_skipped}`\n"
            f"⚠️ Sheets Failed (will retry next run): `{total_failed}`\n"
            f"⏱️ Time: `{duration}s`"
        )
        print(summary)
        # Immediate alert only on real events — the hourly report below covers
        # the "all clear, checked X times" heartbeat instead of pinging every run.
        if total_deleted > 0 or total_failed > 0:
            send_telegram(TELEGRAM_BOT_TOKEN, DELETE_ALERT_ID, summary)

        # Fires once per hour, on the first run after the hour rolls over
        maybe_send_hourly_report(cur)
        conn.commit()

    except Exception as e:
        send_telegram(LOG_BOT_TOKEN, ADMIN_ID, f"🔥 *DELETE-WATCH CRITICAL FAILURE*\nError: `{str(e)}`")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > 3600:
            print("🧟 Zombie lock detected (older than 60 mins). Clearing it...")
            os.remove(LOCK_FILE)
        else:
            print("⏳ Delete-watch already running. Aborting to prevent overlap.")
            sys.exit(0)

    with open(LOCK_FILE, 'w') as f:
        f.write(str(time.time()))

    try:
        main()
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)