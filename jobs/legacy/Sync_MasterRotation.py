"""
Sync_MasterRotation.py

Continuous sync into the Master Sheet (device rotation tracker), sourced
from the worldrisk_tracking table via direct Postgres connection. Designed
to run on a 5-minute cron cadence (confirmed with Jay 2026-07-29):
    - New devices in scope that aren't on the Master Sheet yet -> added
    - Devices already on the Master Sheet -> updated with latest DB data
      (dispatch/arrival dates, trailers, etc. often get filled in later)

Change-detection (added 2026-08-05): a device is only pushed to Smartsheet
if its DB row_modified_at has advanced past what we last pushed. This is
tracked in master_rotation_sync_state (device_number PK), same pattern as
Sync_WorldRisk.py's worldrisk_sync_sheet_state and police_bot.py's sheet
state tracking. Unchanged devices cost nothing beyond a dict lookup - no
Smartsheet read/write at all - which is what gets this under the 5-minute
cron budget (first test run pushed all 3,630 rows in 465s regardless of
whether anything changed).

Only host + password come from the license handshake (JaysNetworkLicense ->
credentials['url'] -> DB_HOST override, credentials['key'] -> DB_USER,
credentials['pass'] -> DB_PASSWORD); port and dbname are static per
New_Script_Handover.md section 2.

Scope (confirmed 2026-07-29, cutoff updated 2026-08-05):
    - workspace_name = "WorldRisk Tracking Sheets" only
    - excludes folder_name in ("Cross border Archives", "ZZZ Archive")
    - booking_date >= CUTOFF_DATE

Master Sheet:   4974391422046084 (production) / 2150650774245252 (test)

Uses a lock file so an overlapping cron tick doesn't stack on top of a
still-running sync (same pattern as the current Sync_WorldRisk.py).
"""

import os
import sys
import time
import traceback
from datetime import datetime, timezone
import dateutil.parser
from dotenv import load_dotenv
import smartsheet
import requests
import psycopg2
import psycopg2.extras

from license_manager import JaysNetworkLicense

LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/master_rotation_sync.lock"

# Static per-environment DB config (per New_Script_Handover.md section 2) -
# host/user/password come from the license handshake; only port/dbname are static.
DB_PORT = 5432
DB_NAME = "jaysnet_data"

# --- 1. SETUP ---
load_dotenv()

# --- PRODUCTION MODE ---
# Switched to production 2026-08-05 after confirming change-detection
# behavior on the test sheet (2150650774245252).
MASTER_SHEET_ID = 4974391422046084

LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN')
RISK_LOG_ID = os.getenv('RISK_LOG_ID')
ADMIN_ID = os.getenv('ADMIN_TELEGRAM_ID')

# Scope (confirmed 2026-07-29): only this workspace, excluding these two
# archive folders by exact name.
SOURCE_WORKSPACE_NAME = "WorldRisk Tracking Sheets"
EXCLUDE_FOLDERS = ["Cross border Archives", "ZZZ Archive"]

# Individual sheets excluded by exact name, regardless of folder (added 2026-08-05).
EXCLUDE_SHEETS = ["Reload invoicing Sheet"]

# Closed/finished loads - not relevant to an active rotation tracker.
# DELIVERED is deliberately NOT here - it's needed for the arrival-date logic.
EXCLUDE_STATUSES = ["INVOICED", "INVOICING", "COLLECTED"]

# Starting point for both passes - don't pull the entire historical backlog.
# Updated 2026-08-05 (was 2026-06-01).
CUTOFF_DATE = "2026-08-01"


def send_risk_log(msg):
    if not LOG_BOT_TOKEN or not RISK_LOG_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage",
            json={"chat_id": RISK_LOG_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def send_admin(msg):
    if not LOG_BOT_TOKEN or not ADMIN_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


# --- 2. LICENSE HANDSHAKE (same pattern as Sync_WorldRisk.py) ---
license_key = os.getenv('JAYS_NETWORK_LICENSE_KEY')
if not license_key:
    print("❌ LICENSE ERROR: Key missing in .env")
    send_admin("❌ *Sync_MasterRotation*: License key missing in .env")
    sys.exit(1)

auth_system = JaysNetworkLicense(license_key)
credentials = auth_system.authenticate()
if not credentials:
    print("⛔ ACCESS DENIED: License validation failed.")
    send_admin("⛔ *Sync_MasterRotation*: License validation failed.")
    sys.exit(1)

print("✅ ACCESS GRANTED.")

# The gatekeeper returns 'localhost' as the DB host, which is only correct
# when running directly on the DB's own LXC. This script runs on
# Ubuntu-Script-Server instead, so override with the real reachable host.
# TODO: fix this server-side in the gatekeeper API so scripts don't need
# this override.
DB_HOST = "omnimetrics.dns.jays-network.com"
DB_USER = credentials['key']       # confirmed via Sync_WorldRisk.py: 'key' is the DB username
DB_PASSWORD = credentials['pass']  # confirmed via Sync_WorldRisk.py: 'pass' is the actual password

# --- 3. SMARTSHEET CLIENT (write-side) ---
ss_client = smartsheet.Smartsheet(os.getenv('SMARTSHEET_ACCESS_TOKEN'))
ss_client.errors_as_exceptions = False  # type: ignore


def robust_api_call(func, *args, **kwargs):
    attempts = 0
    while attempts < 5:
        try:
            response = func(*args, **kwargs)
            if hasattr(response, 'result') and hasattr(response.result, 'code') and response.result.code == 4003:
                print("   ⚠️ Rate limit, cooling down 60s...")
                time.sleep(60)
                attempts += 1
                continue
            if isinstance(response, smartsheet.models.Error):
                attempts += 1
                msg = getattr(response.result, 'message', 'Unknown') if hasattr(response, 'result') else 'Unknown'
                if attempts < 5:
                    wait = 10 * attempts
                    print(f"   ⚠️ Smartsheet API error: {msg} - retrying in {wait}s ({attempts}/5)...")
                    time.sleep(wait)
                    continue
                print(f"   ⚠️ Smartsheet API error: {msg} - giving up after {attempts} attempts.")
                return None
            return response
        except Exception as e:
            attempts += 1
            if attempts < 5:
                wait = 10 * attempts
                print(f"   ⚠️ Network/API exception: {e} - retrying in {wait}s ({attempts}/5)...")
                time.sleep(wait)
                continue
            print(f"   ⚠️ Network/API exception: {e} - giving up after {attempts} attempts.")
            return None
    return None


# --- 4. REGION / CLIENT MAPPING ---
REGION_CODE_MAP = {
    "DRC": "DRC", "ZAM": "ZAMBIA", "ZAMBIA": "ZAMBIA", "ZIM": "ZIMBABWE", "ZIMBABWE": "ZIMBABWE",
    "NAM": "NAMIBIA", "NAMIBIA": "NAMIBIA", "BOTS": "BOTSWANA", "BOTSWANA": "BOTSWANA",
    "MOZ": "MOZAMBIQUE", "MOZAMBIQUE": "MOZAMBIQUE", "TAN": "TANZANIA", "TANZANIA": "TANZANIA",
    "MAL": "MALAWI", "MALAWI": "MALAWI", "SA": "SOUTH AFRICA", "SOUTH AFRICA": "SOUTH AFRICA",
}

FOLDER_TO_CLIENT = {
    "Alistair Logistics": "Alistair",
    "Alliance": "Alliance stream",
    "FH Bertling": "FH bertling",
    "FQM Tracking Sheets": "FQM",
    "GSM": "GSM",
    "IXM": "IXM",
    "Reload": "Reload",
    "SLS Trading": "SLS trading",
    "Zalawi": "Zalawi",
    # Glencore has no subfolders - split by sheet name instead
}

GLENCORE_LOCAL_SHEETS = {
    "DBN Local", "JHB Local", "PE Local", "DBN-JHB", "DBN-CPT", "DBN-PE",
    "JHB-CPT", "JHB-DBN", "JHB-PE", "PE-DBN", "PE-JHB",
    "Depo to Port Breakbulk", "Depo-Port-Vessel",
}
GLENCORE_INT_SHEETS = {
    "Glencore International", "CCM Mine- Zambia", "Kansanshi Mine- Zambia",
    "KCM Mine- Zambia", "Mopani Mine- Zambia", "Moxico Mine- Zambia",
}


def resolve_client(folder_name, sheet_name):
    if folder_name == "Glencore":
        if sheet_name in GLENCORE_LOCAL_SHEETS:
            return "Glencore local"
        if sheet_name in GLENCORE_INT_SHEETS:
            return "Glencore INT"
        return None
    return FOLDER_TO_CLIENT.get(folder_name)


CITY_COUNTRY_MAP = {
    "DURBAN": "SOUTH AFRICA", "RICHARDS BAY": "SOUTH AFRICA", "JOHANNESBURG": "SOUTH AFRICA",
    "JHB": "SOUTH AFRICA", "DBN": "SOUTH AFRICA", "CAPE TOWN": "SOUTH AFRICA",
    "PORT ELIZABETH": "SOUTH AFRICA", "GERMISTON": "SOUTH AFRICA", "BENONI": "SOUTH AFRICA",
    "SOLWEZI": "ZAMBIA", "NDOLA": "ZAMBIA", "KITWE": "ZAMBIA", "CHINGOLA": "ZAMBIA",
    "LUSAKA": "ZAMBIA", "CHIRUNDU": "ZAMBIA", "LUANSHYA": "ZAMBIA", "KABWE": "ZAMBIA",
    "KASUMBALESA": "DRC", "KOLWEZI": "DRC", "LUBUMBASHI": "DRC", "LIKASI": "DRC",
    "KAKANDA": "DRC", "FUNGURUME": "DRC", "SAKANIA": "DRC",
    "DAR ES SALAAM": "TANZANIA", "TUNDUMA": "TANZANIA", "DAR": "TANZANIA",
    "WALVIS BAY": "NAMIBIA", "WALVIS": "NAMIBIA", "WINDHOEK": "NAMIBIA",
    "BEIRA": "MOZAMBIQUE", "MAPUTO": "MOZAMBIQUE",
    "HARARE": "ZIMBABWE",
    "GABORONE": "BOTSWANA", "FRANCISTOWN": "BOTSWANA",
}


def resolve_country(sheet_name, folder_name, offloading_location, raw_data):
    haystack = f"{sheet_name} {folder_name}".upper()
    for code, country in REGION_CODE_MAP.items():
        if code in haystack:
            return country
    for city, country in CITY_COUNTRY_MAP.items():
        if city in haystack:
            return country
    if offloading_location and str(offloading_location).upper() != "N/A":
        loc_upper = str(offloading_location).upper()
        for city, country in CITY_COUNTRY_MAP.items():
            if city in loc_upper:
                return country
    if raw_data:
        raw_upper = str(raw_data).upper()
        for code, country in REGION_CODE_MAP.items():
            if f"{code} REGION" in raw_upper:
                return country
    return None


REGION_DISPATCH_DATE_COL = {
    "DRC": "drc_dispatch_date", "ZAMBIA": "zam_dispatch_date", "NAMIBIA": "nam_dispatch_date",
    "TANZANIA": "tan_dispatch_date", "ZIMBABWE": "zim_dispatch_date", "BOTSWANA": "bots_dispatch_date",
    "MOZAMBIQUE": "moz_dispatch_date", "SOUTH AFRICA": "sa_dispatch_date",
}


def resolve_dispatch_date(row):
    """Multi-country routes can have several region-specific dispatch dates
    set (e.g. dispatched from SA, then re-dispatched from a DRC depot). We
    want the EARLIEST one - the actual start of the journey, not the most
    recent leg. Many sheets don't populate a dedicated dispatch_date column
    at all though, so fall back through progressively looser proxies for
    "when did this actually leave"."""
    candidates = [row.get("dispatch_date")]
    for col in REGION_DISPATCH_DATE_COL.values():
        candidates.append(row.get(col))
    dates = [d for d in candidates if d]
    if dates:
        return min(dates)

    return row.get("intransit_date") or row.get("intransit_began_date") or row.get("booking_date")


ARRIVED_STATUS_KEYWORDS = ["ARRIVED", "DELIVERED"]


def resolve_arrival_date(status, raw_data):
    """Only report an arrival date once the load has actually arrived/been
    delivered - otherwise a truck still in transit through another country
    could show a stale/irrelevant arrival date from an earlier leg."""
    if not status or not any(kw in status.upper() for kw in ARRIVED_STATUS_KEYWORDS):
        return None

    candidates = []
    for alias in RAW_DATA_ALIASES.get("date_arrived", []):
        val = raw_data.get(alias) if raw_data else None
        if val:
            parsed = None
            try:
                parsed = dateutil.parser.parse(str(val))
            except Exception:
                continue
            candidates.append(parsed)

    if not candidates:
        return None
    return max(candidates).strftime("%Y-%m-%d")


# Aliases for fields that aren't their own DB columns but may be present as
# raw column values inside raw_data (JSONB dump of every source sheet column).
RAW_DATA_ALIASES = {
    "trailer1": ["TRAILER REG: 1", "Trailer 1", "Trailer Reg 1", "TRAILER 1"],
    "trailer2": ["TRAILER REG: 2", "Trailer 2", "Trailer Reg 2", "TRAILER 2"],
    "lock_device_number": ["LOCK DEVICE NUMBER", "Lock Device", "Lock Device No"],
    # Confirmed 2026-08-05: only TRUE delivery/final-arrival columns count here -
    # deliberately excludes intermediate customs/border-crossing columns like
    # "Date Of Arrival At Zambia Customs" or "Arrival time at Mozambique Customs",
    # which mark a border crossing, not the load actually reaching its destination.
    "date_arrived": ["ARRIVAL DATE", "DRC REGION: ARRIVAL DATE", "ZAM REGION: ARRIVAL DATE",
                      "NAM REGION: ARRIVAL DATE", "TAN REGION: ARRIVAL DATE", "ZIM REGION: ARRIVAL DATE",
                      "BOTS REGION: ARRIVAL DATE", "MOZ REGION: ARRIVAL DATE", "SA REGION: ARRIVAL DATE",
                      "MAL REGION: ARRIVAL DATE",
                      "Date Arrived", "Date Delivered", "Date of Delivery", "Delivered",
                      "Confirmed Delivered", "CONFIRMATION LOAD ARRIVED"],
}


def extract_from_raw_data(raw_data, field_key):
    if not raw_data or not isinstance(raw_data, dict):
        return None
    for alias in RAW_DATA_ALIASES.get(field_key, []):
        val = raw_data.get(alias)
        if val:
            return val
    return None


# --- 5. ENSURE MASTER SHEET COUNTRY PICKLIST COVERS ALL REGIONS ---
def ensure_country_options(master_columns):
    country_col = next((c for c in master_columns if c.title == "COUNTRY"), None)
    if not country_col:
        print("   ⚠️ Could not find COUNTRY column on Master Sheet.")
        return
    current_options = set(country_col.options or [])
    needed = set(REGION_CODE_MAP.values())
    missing = needed - current_options
    if not missing:
        return
    new_options = list(current_options) + sorted(missing)
    updated_col = smartsheet.models.Column({"type": "PICKLIST", "options": new_options})
    result = robust_api_call(ss_client.Sheets.update_column, MASTER_SHEET_ID, country_col.id, updated_col)
    if result:
        print(f"   ✅ Added COUNTRY picklist options: {sorted(missing)}")
    else:
        print(f"   ⚠️ Failed to extend COUNTRY picklist - rows for {sorted(missing)} may fail to write.")


# --- 6. LOAD MASTER SHEET STATE ---
def load_master_state():
    sheet = robust_api_call(ss_client.Sheets.get_sheet, MASTER_SHEET_ID)
    if not sheet:
        raise RuntimeError("Could not load Master Sheet")

    col_map = {col.title: col.id for col in sheet.columns}
    required = [
        "Internal Ref", "CLIENT", "TRANSPORTER", "HORSE REGESTRATION", "TRAILER 1", "TRAILER 2",
        "COUNTRY", "DATE DISPATCHED", "DEVICE NUMBER", "LOCK DEVICE NUMBER", "DETAILS CAPTURED?",
        "OFFLOADING LOCATION", "OFFLOADING POINT", "DATE ARRIVED", "TO BE COLLECTED",
    ]
    missing_cols = [c for c in required if c not in col_map]
    if missing_cols:
        raise RuntimeError(f"Master Sheet is missing expected columns: {missing_cols}")

    device_to_row_id = {}
    device_col_id = col_map["DEVICE NUMBER"]
    for row in sheet.rows:
        cell = next((c for c in row.cells if c.column_id == device_col_id), None)
        if not cell:
            continue
        # Prefer display_value: if this column auto-detected as numeric,
        # cell.value could be a float (e.g. 726792.0) that won't string-match
        # the DB's plain string device numbers.
        raw_val = cell.display_value if cell.display_value else cell.value
        val = str(raw_val).strip() if raw_val else None
        if val:
            device_to_row_id[val] = row.id

    return sheet, col_map, device_to_row_id


def build_master_row(col_map, data):
    cells = []

    def add(title, value):
        if value is None or value == "":
            return
        col_id = col_map.get(title)
        if not col_id:
            return
        if hasattr(value, "isoformat"):  # date/datetime from psycopg2 - SDK needs a string
            value = value.isoformat()
        cell = {"columnId": col_id, "value": value}
        if title == "CLIENT":
            cell["strict"] = False  # sheet names won't match the fixed picklist - allow free text
        cells.append(cell)

    add("Internal Ref", data.get("internal_ref"))
    add("CLIENT", data.get("client"))
    add("TRANSPORTER", data.get("transporter"))
    add("HORSE REGESTRATION", data.get("reg_number"))
    add("TRAILER 1", data.get("trailer1"))
    add("TRAILER 2", data.get("trailer2"))
    add("COUNTRY", data.get("country"))
    add("DATE DISPATCHED", data.get("date_dispatched"))
    add("DEVICE NUMBER", data.get("device_number"))
    add("LOCK DEVICE NUMBER", data.get("lock_device_number"))
    add("OFFLOADING LOCATION", data.get("offloading_point"))
    add("OFFLOADING POINT", data.get("offloading_point"))
    add("DATE ARRIVED", data.get("date_arrived"))
    # DETAILS CAPTURED? / TO BE COLLECTED intentionally left unset

    new_row = smartsheet.models.Row()
    new_row.to_top = True
    new_row.cells = cells
    return new_row


def build_update_row(row_id, col_map, data):
    """Same field set as build_master_row, but targets an existing row_id
    instead of inserting a new one."""
    row = build_master_row(col_map, data)
    row.id = row_id
    row.to_top = None  # not valid/needed on an update
    return row


# --- 7. DB CONNECTION + QUERY (direct psycopg2 against worldrisk_tracking) ---
def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
    )


EXCLUSION_SQL = ("workspace_name = %(workspace)s AND folder_name != ALL(%(excluded_folders)s) "
                  "AND sheet_name != ALL(%(excluded_sheets)s) "
                  "AND (status IS NULL OR NOT (status ILIKE ANY(%(excluded_statuses)s)))")


def fetch_db_rows(conn):
    """DISTINCT ON (device_number) gets the most recently modified row per
    device directly in SQL - real Postgres, so no client-side dedupe needed.
    row_modified_at is selected explicitly (not just used in ORDER BY) so
    classify_rows_from_db can compare it against master_rotation_sync_state
    for change-detection."""
    query = f"""
        SELECT DISTINCT ON (device_number)
            row_id, device_number, reg_number, transporter, internal_ref,
            offloading_location, folder_name, sheet_name, workspace_name,
            booking_date, dispatch_date, intransit_date, intransit_began_date,
            status, raw_data, row_modified_at,
            drc_dispatch_date, zam_dispatch_date, nam_dispatch_date,
            tan_dispatch_date, zim_dispatch_date, bots_dispatch_date,
            moz_dispatch_date, sa_dispatch_date
        FROM worldrisk_tracking
        WHERE device_number IS NOT NULL AND device_number != ''
          AND booking_date >= %(cutoff)s
          AND {EXCLUSION_SQL}
        ORDER BY device_number, row_modified_at DESC NULLS LAST
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(query, {
        "cutoff": CUTOFF_DATE,
        "workspace": SOURCE_WORKSPACE_NAME,
        "excluded_folders": EXCLUDE_FOLDERS,
        "excluded_sheets": EXCLUDE_SHEETS,
        "excluded_statuses": [f"%{s}%" for s in EXCLUDE_STATUSES],
    })
    rows = cur.fetchall()
    cur.close()
    return rows


# --- 7b. CHANGE-DETECTION STATE (master_rotation_sync_state) ---
def _normalize_dt(dt):
    """DB timestamps can come back naive or tz-aware depending on the
    column type / driver path. Normalize everything to UTC-aware before
    comparing, otherwise Python raises on naive-vs-aware comparisons."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_sync_state(conn):
    """device_number -> last row_modified_at we successfully pushed to
    Smartsheet. A device is skipped entirely (no Smartsheet call at all)
    if the DB's row_modified_at hasn't advanced past this."""
    cur = conn.cursor()
    cur.execute("SELECT device_number, last_row_modified_at FROM master_rotation_sync_state")
    state = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    return state


def save_sync_state(conn, pushed_devices):
    """pushed_devices: list of (device_number, row_modified_at) tuples for
    devices that were just successfully written to Smartsheet. Devices in a
    chunk that failed to write are intentionally left out so they retry
    next run."""
    if not pushed_devices:
        return
    cur = conn.cursor()
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO master_rotation_sync_state (device_number, last_row_modified_at, last_pushed_at)
        VALUES %s
        ON CONFLICT (device_number) DO UPDATE SET
            last_row_modified_at = EXCLUDED.last_row_modified_at,
            last_pushed_at = EXCLUDED.last_pushed_at
        """,
        [(d, m, datetime.now(timezone.utc)) for d, m in pushed_devices],
    )
    conn.commit()
    cur.close()


def classify_rows_from_db(db_rows, device_to_row_id, sync_state):
    rows_to_add = []
    rows_to_update = []
    skipped_unchanged = 0

    for r in db_rows:
        device = str(r.get("device_number") or "").strip()
        if not device:
            continue

        row_modified_at = r.get("row_modified_at")
        existing_row_id = device_to_row_id.get(device)

        # Change-detection: only skip for devices ALREADY on the Master
        # Sheet. New devices always go through regardless of state, since
        # they've never been pushed. If row_modified_at is missing, always
        # push - safer to send an unnecessary update than to silently miss
        # a real change with no timestamp to compare.
        if existing_row_id and existing_row_id != "PENDING" and row_modified_at:
            last_pushed = sync_state.get(device)
            if last_pushed and _normalize_dt(row_modified_at) <= _normalize_dt(last_pushed):
                skipped_unchanged += 1
                continue

        folder_name = r.get("folder_name") or ""
        sheet_name = r.get("sheet_name") or ""
        country = resolve_country(sheet_name, folder_name, r.get("offloading_location"), r.get("raw_data"))
        raw_data = r.get("raw_data")

        data = {
            "internal_ref": r.get("row_id"),  # DB row_id - stable link back to the exact source row
            "client": sheet_name,  # traceability: know exactly which sheet this came from
            "transporter": r.get("transporter"),
            "reg_number": r.get("reg_number"),
            "trailer1": extract_from_raw_data(raw_data, "trailer1"),
            "trailer2": extract_from_raw_data(raw_data, "trailer2"),
            "country": country,
            "date_dispatched": resolve_dispatch_date(r),
            "device_number": device,
            "lock_device_number": extract_from_raw_data(raw_data, "lock_device_number"),
            "offloading_point": r.get("offloading_location"),
            "date_arrived": resolve_arrival_date(r.get("status"), raw_data),
            "_row_modified_at": row_modified_at,  # carried through for state-saving after write, stripped before build_master_row
        }

        if existing_row_id and existing_row_id != "PENDING":
            rows_to_update.append((existing_row_id, data))
        else:
            rows_to_add.append(data)
            device_to_row_id[device] = "PENDING"  # avoid double-adding within this same run

    return rows_to_add, rows_to_update, skipped_unchanged


# --- 8. WRITE TO MASTER SHEET ---
CHUNK_SIZE = 250
CHUNK_PAUSE_SECONDS = 2


def write_new_rows(col_map, rows_to_add):
    """Returns (total_added, pushed_devices) where pushed_devices is a list
    of (device_number, row_modified_at) for devices in chunks that wrote
    successfully - used to update master_rotation_sync_state."""
    if not rows_to_add:
        print("Nothing to add.")
        return 0, []

    total_added = 0
    pushed_devices = []
    for i in range(0, len(rows_to_add), CHUNK_SIZE):
        chunk_data = rows_to_add[i:i + CHUNK_SIZE]
        smartsheet_rows = [build_master_row(col_map, data) for data in chunk_data]
        result = robust_api_call(ss_client.Sheets.add_rows, MASTER_SHEET_ID, smartsheet_rows)
        if result:
            total_added += len(chunk_data)
            pushed_devices.extend(
                (data["device_number"], data["_row_modified_at"]) for data in chunk_data
            )
        else:
            print(f"   ❌ Failed to add a chunk of {len(chunk_data)} rows.")
        if i + CHUNK_SIZE < len(rows_to_add):
            time.sleep(CHUNK_PAUSE_SECONDS)
    return total_added, pushed_devices


def write_updated_rows(col_map, rows_to_update):
    """Returns (total_updated, pushed_devices) - see write_new_rows."""
    if not rows_to_update:
        print("Nothing to update.")
        return 0, []

    total_updated = 0
    pushed_devices = []
    for i in range(0, len(rows_to_update), CHUNK_SIZE):
        chunk = rows_to_update[i:i + CHUNK_SIZE]
        smartsheet_rows = [build_update_row(row_id, col_map, data) for row_id, data in chunk]
        result = robust_api_call(ss_client.Sheets.update_rows, MASTER_SHEET_ID, smartsheet_rows)
        if result:
            total_updated += len(chunk)
            pushed_devices.extend(
                (data["device_number"], data["_row_modified_at"]) for _row_id, data in chunk
            )
        else:
            print(f"   ❌ Failed to update a chunk of {len(chunk)} rows.")
        if i + CHUNK_SIZE < len(rows_to_update):
            time.sleep(CHUNK_PAUSE_SECONDS)
    return total_updated, pushed_devices


# --- 9. MAIN ---
def run_sync():
    start = time.time()
    print(f"🚀 Sync_MasterRotation started: {datetime.now().strftime('%H:%M:%S')}")

    master_sheet, col_map, device_to_row_id = load_master_state()
    ensure_country_options(master_sheet.columns)

    conn = get_db_connection()
    db_rows = fetch_db_rows(conn)
    sync_state = load_sync_state(conn)

    rows_to_add, rows_to_update, skipped_unchanged = classify_rows_from_db(
        db_rows, device_to_row_id, sync_state
    )
    print(f"   {len(rows_to_add)} new device(s) to add, {len(rows_to_update)} existing device(s) to update, "
          f"{skipped_unchanged} unchanged (skipped).")

    added, added_devices = write_new_rows(col_map, rows_to_add)
    updated, updated_devices = write_updated_rows(col_map, rows_to_update)

    save_sync_state(conn, added_devices + updated_devices)
    conn.close()

    duration = round(time.time() - start, 1)
    summary = (
        f"✅ *Master Rotation Sync Complete*\n"
        f"🆕 New rows added: `{added}`\n"
        f"🔄 Existing rows updated: `{updated}`\n"
        f"⏭️ Unchanged (skipped): `{skipped_unchanged}`\n"
        f"⏱️ Time: `{duration}s`"
    )
    print(summary)
    send_risk_log(summary)


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

    try:
        run_sync()
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = traceback.format_exc()
        print(err)
        send_admin(f"🚨 *CRASH:* `Sync_MasterRotation.py`\n❌ `{str(e)}`\n\n`{err[:500]}`")