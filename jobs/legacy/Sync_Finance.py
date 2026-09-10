#!/usr/bin/env python3
"""
Sync_Finance.py

Pulls all sheets in Smartsheet workspace 3486055002335108 into the new
finance_expenses / finance_payment_requests / finance_payment_request_items
tables. Follows existing Sync_ADARS.py conventions: Smartsheet SDK,
psycopg2 upsert, fcntl locking, JaysNetworkLicense credential pattern.

SHEETS COVERED
--------------
No sheet IDs are hardcoded. Every run scans the entire workspace
(FINANCE_WORKSPACE_ID = 3486055002335108) — root sheets plus every folder
and subfolder, recursively — and classifies whatever it finds by name:

  - name contains "cross border finance"        -> ledger (finance_expenses)
  - name contains "payment request", no "archive" -> live request sheet
      (category parsed from the "(...)" in the name, e.g.
      "Payment Request (FUEL)" -> category "FUEL")
  - name contains "payment request" AND "archive" -> archived request sheet
      (category derived per-row from the row's own Type of Request field,
      since archive sheets mix all categories together)
  - anything else (Templates, Payment Slips, the Payments dashboard/sight,
      etc.) -> skipped, logged as unclassified

This means new sheets, new archive chunks, renamed sheets, or sheets moved
between folders are all picked up automatically on the next run — nothing
here needs to change when the workspace structure changes. If Smartsheet
ever adds a sheet whose name doesn't match either pattern but should be
synced, it will show up in the "unclassified" log line for a human to
review, not silently sync into the wrong table.

Run finance_schema.sql then finance_schema_archive_migration.sql against
jaysnet_data before first run with this version.
Suggested cron offset: :12 (ADARS=:00, WorldRisk=:03, Border Bot=:06).
"""

import os
import sys
import time
import json
import re
import traceback
import requests
import psycopg2
from psycopg2.extras import execute_values
import smartsheet
from datetime import datetime, timezone
from dotenv import load_dotenv
from license_manager import JaysNetworkLicense

load_dotenv()

LOG_BOT_TOKEN = os.getenv('LOG_BOT_TOKEN')
FINANCE_LOG_ID = os.getenv('FINANCE_LOG_ID')

LOCK_FILE = "/opt/jaysnet/finance_sync.lock"


def send_telegram(message):
    """Sends a message ONLY to the Finance Log Group"""
    if not LOG_BOT_TOKEN or not FINANCE_LOG_ID:
        print("⚠️ Telegram tokens missing, skipping alert.")
        return
    api_url = f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(api_url, json={
            "chat_id": FINANCE_LOG_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to send Telegram: {e}")

# --- workspace scan config -------------------------------------------------
FINANCE_WORKSPACE_ID = 3486055002335108

# At 1,440 runs/day (every-minute delta cron), transient Smartsheet API
# hiccups or rate limits are far more likely to be hit than at the old
# once-per-cron-cycle cadence. Same retry/cooldown pattern Sync_WorldRisk.py
# uses, rather than letting one bad API call abort an entire delta run.
def robust_api_call(func, *args, **kwargs):
    attempts = 0
    while attempts < 3:
        try:
            response = func(*args, **kwargs)
            if hasattr(response, "result") and hasattr(response.result, "code"):
                if response.result.code == 4003:
                    print("   ⚠️ Rate Limit! Cooling down 60s...")
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

# Archive sheets don't have a fixed one-category-per-sheet layout like the
# live sheets do — every row's actual category lives in its own
# "Type of Request" cell. Normalize those values to match the live-sheet
# category labels so request_category means the same thing everywhere.
# CPO kept as its own bucket rather than folded into "Other" — flag to
# revisit with Jay if that's wrong.
ARCHIVE_CATEGORY_NORMALIZE = {
    "Fuel": "Fuel",
    "Other": "Other",
    "Food Money": "Food",
    "Transport money": "Transport",
    "Cross-Border $": "Cross-Border",
    "CPO": "CPO",
}


def classify_sheet(name):
    """Returns ('ledger'|'request_live'|'request_archived'|None, category_or_none).
    category_or_none is only meaningful for request_live (parsed from the
    sheet name's parenthesized category)."""
    lname = name.lower()

    if "cross border finance" in lname:
        return ("ledger", None)

    if "payment request" in lname:
        if "archive" in lname:
            return ("request_archived", None)
        m = re.search(r"\(([^)]+)\)", name)
        category = m.group(1).strip() if m else "Unspecified"
        return ("request_live", category)

    return (None, None)


def discover_finance_sheets(ss_client):
    """Scans FINANCE_WORKSPACE_ID recursively (root + folders + one level
    of subfolders, matching the traversal depth Sync_WorldRisk.py uses) and
    classifies every sheet found. Returns a list of
    (sheet_id, sheet_name, classification, category, modified_at) tuples
    for sheets that matched a known pattern, plus logs anything skipped.
    This is metadata-only (no row data fetched) — cheap enough to run every
    delta cycle, same justification Sync_WorldRisk.py uses for its own
    collect_all_sheet_stubs()."""
    ws = robust_api_call(ss_client.Workspaces.get_workspace, FINANCE_WORKSPACE_ID)
    stubs = []  # sheet_lite objects

    for sheet_lite in ws.sheets:
        stubs.append(sheet_lite)

    for folder in ws.folders:
        full_folder = robust_api_call(ss_client.Folders.get_folder, folder.id)
        if not full_folder:
            continue
        for sheet_lite in full_folder.sheets:
            stubs.append(sheet_lite)
        for sub_folder_lite in full_folder.folders:
            sub_folder = robust_api_call(ss_client.Folders.get_folder, sub_folder_lite.id)
            if not sub_folder:
                continue
            for sheet_lite in sub_folder.sheets:
                stubs.append(sheet_lite)

    classified = []
    for sheet_lite in stubs:
        kind, category = classify_sheet(sheet_lite.name)
        if kind is None:
            print(f"   ⏭️  Skipping unclassified sheet: {sheet_lite.name}")
            continue
        modified_at = normalize_modified_at(getattr(sheet_lite, "modified_at", None))
        classified.append((sheet_lite.id, sheet_lite.name, kind, category, modified_at))

    return classified


def normalize_modified_at(val):
    """Consistent tz-aware datetime for comparing a sheet's modified_at
    against stored delta-sync state — same helper Sync_WorldRisk.py uses.
    The Smartsheet SDK usually already returns a datetime; dateutil handles
    the rare case it comes through as a string instead."""
    if not val:
        return None
    if hasattr(val, "tzinfo"):
        dt = val
    else:
        import dateutil.parser
        try:
            dt = dateutil.parser.parse(str(val))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_sync_state(conn, sheet_ids):
    """Bulk-fetch known delta-sync state for every discovered sheet in one
    query, instead of a per-sheet round trip."""
    if not sheet_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sheet_id, last_synced_modified_at FROM finance_sync_sheet_state WHERE sheet_id = ANY(%s::bigint[])",
            (list(sheet_ids),),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def should_process_sheet(modified_at, last_synced, force_full_sync):
    if force_full_sync:
        return True
    if modified_at is None:
        return True  # can't tell — safest is to check it
    if last_synced is None:
        return True  # never synced before
    return modified_at > last_synced


def update_sync_state(cur, sheet_id, sheet_name, kind, category, modified_at):
    cur.execute(
        """INSERT INTO finance_sync_sheet_state
               (sheet_id, sheet_name, kind, category, last_synced_modified_at, last_synced_at)
           VALUES (%s, %s, %s, %s, %s, now())
           ON CONFLICT (sheet_id) DO UPDATE SET
               sheet_name = EXCLUDED.sheet_name,
               kind = EXCLUDED.kind,
               category = EXCLUDED.category,
               last_synced_modified_at = EXCLUDED.last_synced_modified_at,
               last_synced_at = now()""",
        (sheet_id, sheet_name, kind, category, modified_at),
    )


# --- ledger column mapping (Cross Border Finance sheets) -----------------
LEDGER_COLUMN_MAP = {
    "DAILY EXPENSES":               "expense_date",
    "Ref / Tracking #":             "ref_tracking",
    "Country":                      "country",
    "Requested by":                 "requested_by",
    "Casual":                       "is_casual",
    "Reason:":                      "reason",            # multi-picklist -> list
    "Detail: (Reg. No)":            "reg_number",
    "Funds for":                    "funds_for",
    "Currency":                     "currency",
    "Amount":                       "amount",
    "Number of Trucks":             "number_of_trucks",
    "Number of Escorts:":           "number_of_escorts",
    "Number of Days:":              "number_of_days",
    "OB Number - (ALL Responses)":  "ob_number",
    "Client:":                      "client_name",
    "ODO OPEN":                     "odo_open",
    "ODO CLOSE":                    "odo_close",
    "Kms Driven":                   "kms_driven",
    "Litres":                       "litres",
    "Comment":                      "comment",
    "Archive":                      "is_archived",
}

# --- payment request column mapping (all 5 form sheets share this shape) -
REQUEST_COLUMN_MAP = {
    "FINANCE REF":                       "finance_ref",
    "Date Submitted ":                   "date_submitted",
    "Date :":                            "request_date",
    "Type of Request":                   "type_of_request",
    "Requested By :":                    "requested_by",
    "Payee Name :":                      "payee_name",
    "Description":                       "description",
    "Amount of Items or Vehicles":       "item_or_vehicle_count",
    "Amount":                            "amount",
    "Total IN ZAR":                      "total_zar",
    "Total in USD":                      "total_usd",
    "Conversion Rate":                   "conversion_rate",
    "Are the above amounts vatable?":    "is_vatable",
    "Paid ":                             "paid_status",
    "Paid by":                           "paid_by",       # Fuel sheet only
    "Paid by ":                          "paid_by",       # trailing-space variant seen on some sheets
    "Is the request high Priority":      "is_high_priority",
    "standard approval":                 "standard_approval",
    "Executlve manager":                 "executive_manager",
    "executive management approval":     "executive_approval",
    "return approval":                   "return_approval",
    "Signature":                         "signature",
    "Signature  authoriser":             "signature_authoriser",
}

# line-item block pattern, e.g. "Posting Code 3", "Vehicle Reg 3", ...
LINE_ITEM_RE = re.compile(
    r"^(Posting Code|Vehicle Reg|Odometer Reading|Description|Amount)\s*(\d+)?\s*$"
)


def normalize_title(title):
    """Archive sheets and live sheets use slightly different header casing/
    spacing for the same field ('Total IN ZAR' vs 'Total In ZAR', 'Paid by'
    vs 'Paid by '). Collapse whitespace and lowercase so lookups match
    regardless of which sheet a header came from."""
    return re.sub(r"\s+", " ", str(title).strip()).lower()


NORMALIZED_LEDGER_COLUMN_MAP = {normalize_title(k): v for k, v in LEDGER_COLUMN_MAP.items()}
NORMALIZED_REQUEST_COLUMN_MAP = {normalize_title(k): v for k, v in REQUEST_COLUMN_MAP.items()}

# Fields that must be coerced from Smartsheet's display-formatted strings
# (currency symbols, comma decimal separators used in SA Rand formatting,
# thousands separators) into actual numeric/boolean types before insert.
LEDGER_NUMERIC_FIELDS = {"amount", "odo_open", "odo_close", "kms_driven", "litres"}
LEDGER_INT_FIELDS = {"number_of_trucks", "number_of_escorts", "number_of_days"}
LEDGER_BOOL_FIELDS = {"is_casual", "is_archived"}

REQUEST_NUMERIC_FIELDS = {"amount", "total_zar", "total_usd", "conversion_rate"}
REQUEST_BOOL_FIELDS = {"is_vatable", "is_high_priority"}

LINE_ITEM_NUMERIC_FIELDS = {"odometer_reading", "amount"}

# Postgres column precision/scale bounds, matched exactly to finance_schema.sql,
# so an out-of-range value is caught and logged here (with the actual
# offending value visible) instead of crashing the whole sync run at insert
# time with a bare "numeric field overflow". A value this large in an
# odometer/amount field is virtually always bad data (a phone number, an ID,
# a mis-typed cell) rather than a real reading — dropped to NULL, not
# silently truncated or rounded into range.
NUMERIC_12_2_MAX = 10**10   # NUMERIC(12,2): 10 digits before the decimal
NUMERIC_14_2_MAX = 10**12   # NUMERIC(14,2): 12 digits before the decimal
NUMERIC_14_6_MAX = 10**8    # NUMERIC(14,6): 8 digits before the decimal

FIELD_MAX_ABS = {
    # ledger (finance_expenses), all NUMERIC(12,2)
    "amount_ledger": NUMERIC_12_2_MAX,
    "odo_open": NUMERIC_12_2_MAX,
    "odo_close": NUMERIC_12_2_MAX,
    "kms_driven": NUMERIC_12_2_MAX,
    "litres": NUMERIC_12_2_MAX,
    # payment requests, NUMERIC(14,2) except conversion_rate NUMERIC(14,6)
    "amount": NUMERIC_14_2_MAX,
    "total_zar": NUMERIC_14_2_MAX,
    "total_usd": NUMERIC_14_2_MAX,
    "conversion_rate": NUMERIC_14_6_MAX,
    # line items
    "odometer_reading": NUMERIC_12_2_MAX,
}


def parse_numeric(val, field_name=None, context=None):
    """Strips currency symbols/spaces and normalizes comma-vs-dot decimal
    separators (Smartsheet display values come through as things like
    'R0,00', 'R 1,150.00', or '1150.0' depending on the sheet's column
    formatting). Returns None rather than raising on anything unparseable
    so a single bad cell doesn't kill the whole sync run.

    field_name (a key into FIELD_MAX_ABS) enables a bounds check against
    the actual destination column's precision/scale — a value that would
    overflow gets logged with its raw form and dropped to NULL instead of
    crashing the insert. context is a short string (e.g. row_id) included
    in that log line so the offending Smartsheet row can be found."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        result = float(val)
    else:
        s = str(val).strip()
        if not s:
            return None
        s = re.sub(r"[^0-9,.\-]", "", s)  # strip currency letters/symbols/spaces
        if not s or s in ("-", ".", ","):
            return None
        if "," in s and "." in s:
            # whichever separator appears last is the decimal point
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            tail = s.split(",")[-1]
            s = s.replace(",", ".") if len(tail) == 2 else s.replace(",", "")
        try:
            result = float(s)
        except ValueError:
            return None

    max_abs = FIELD_MAX_ABS.get(field_name) if field_name else None
    if max_abs is not None and abs(result) >= max_abs:
        print(f"   ⚠️ Numeric overflow: field={field_name} value={val!r} context={context} — dropped to NULL")
        return None

    return result


def parse_int(val):
    n = parse_numeric(val)
    return int(round(n)) if n is not None else None


def parse_bool(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("true", "yes", "1", "checked"):
        return True
    if s in ("false", "no", "0", "unchecked", ""):
        return False
    return None

def get_db_connection():
    """Same JaysNetworkLicense handshake as Sync_WorldRisk.py — never raw env creds."""
    print("🔐 Authenticating with Gatekeeper...")
    license_key = os.getenv('LICENSE_KEY') or os.getenv('JAYS_NETWORK_LICENSE_KEY')
    if not license_key:
        err_msg = "❌ *LICENSE ERROR*: Key missing in .env"
        print(err_msg)
        send_telegram(err_msg)
        sys.exit(1)

    auth_system = JaysNetworkLicense(license_key)
    credentials = auth_system.authenticate()
    if not credentials:
        err_msg = "⛔ *ACCESS DENIED*: License Validation Failed. Script Aborted."
        print(err_msg)
        send_telegram(err_msg)
        sys.exit(1)

    print("✅ *ACCESS GRANTED*: Secure Connection Established.")

    try:
        return psycopg2.connect(
            host=credentials["url"],
            user=credentials["key"],
            password=credentials["pass"],
            database="jaysnet_data",
            port="5432"
        )
    except Exception as e:
        print(f"🔴 DB Connection Failed: {e}")
        send_telegram(f"🚨 *DATABASE ERROR*: Could not connect to Postgres.\n`{e}`")
        sys.exit(1)


def get_smartsheet_client():
    token = os.getenv('SMARTSHEET_ACCESS_TOKEN')
    if not token:
        err_msg = "❌ *CONFIG ERROR*: SMARTSHEET_ACCESS_TOKEN missing in .env"
        print(err_msg)
        send_telegram(err_msg)
        sys.exit(1)
    client = smartsheet.Smartsheet(token)
    client.errors_as_exceptions = False
    return client


def acquire_lock():
    """Zombie-lock pattern matching Sync_WorldRisk.py — file-based, not fcntl,
    so behavior is identical to the rest of /opt/jaysnet/ scripts."""
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


def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)


def cell_value(cell):
    return cell.display_value if cell.display_value is not None else cell.value


def cell_numeric_raw(cell):
    """For numeric fields, prefer the raw underlying value over display_value.
    Confirmed against live data: a cell with raw value 0.0 (a "2GB" data
    request with no cost) renders display_value as 'R0,00' — the sheet's
    column-level Rand currency format applied on top of the number. Parsing
    that formatted string is fragile; the raw value is already numeric-clean."""
    v = cell.value
    if v is not None:
        return v
    return cell.display_value


def sync_ledger_sheet(ss_client, conn, sheet_id, source_label):
    sheet = robust_api_call(ss_client.Sheets.get_sheet, sheet_id)
    if not sheet:
        raise RuntimeError(f"get_sheet failed for {source_label} (sheet_id={sheet_id}) after retries")
    col_titles = {c.id: c.title for c in sheet.columns}

    records = []
    for row in sheet.rows:
        record = {
            "row_id": row.id,
            "sheet_id": sheet_id,
            "source_sheet": source_label,
            # Case-insensitive AND typo-tolerant: the real sheet is named
            # "Arcive of Cross Border Finance" (missing the 'h') — confirmed
            # against the actual workspace listing, not a guess. Matching
            # "archiv" or "arciv" catches both spellings.
            "is_archived": any(k in source_label.lower() for k in ("archiv", "arciv")),
        }
        raw = {}
        reason_values = []
        for cell in row.cells:
            title = col_titles.get(cell.column_id)
            if title is None:
                continue
            value = cell_value(cell)
            raw[title] = value
            if title == "Reason:":
                if value:
                    reason_values = [v.strip() for v in str(value).split(",") if v.strip()]
                continue
            mapped = NORMALIZED_LEDGER_COLUMN_MAP.get(normalize_title(title))
            if mapped:
                if mapped in LEDGER_NUMERIC_FIELDS:
                    # "amount" collides in name with the payment-requests
                    # table's wider NUMERIC(14,2) amount column — use the
                    # ledger-specific bound (NUMERIC(12,2)) for this one.
                    bounds_key = "amount_ledger" if mapped == "amount" else mapped
                    value = parse_numeric(cell_numeric_raw(cell), field_name=bounds_key, context=f"ledger row_id={row.id}")
                elif mapped in LEDGER_INT_FIELDS:
                    value = parse_int(cell_numeric_raw(cell))
                elif mapped in LEDGER_BOOL_FIELDS:
                    value = parse_bool(value)
                record[mapped] = value
        record["reason"] = reason_values or None
        record["raw_data"] = raw
        record["row_modified_at"] = row.modified_at.isoformat() if row.modified_at else None
        records.append(record)

    if not records:
        return 0

    columns = [
        "row_id", "sheet_id", "source_sheet", "expense_date", "ref_tracking",
        "country", "requested_by", "is_casual", "reason", "reg_number",
        "funds_for", "currency", "amount", "number_of_trucks",
        "number_of_escorts", "number_of_days", "ob_number", "client_name",
        "odo_open", "odo_close", "kms_driven", "litres", "comment",
        "is_archived", "raw_data", "row_modified_at", "last_synced_at",
    ]
    values = []
    for r in records:
        values.append(tuple(
            json.dumps(r.get("raw_data", {})) if col == "raw_data"
            else r.get("reason") if col == "reason"
            else datetime.now(timezone.utc).isoformat() if col == "last_synced_at"
            else r.get(col)
            for col in columns
        ))

    placeholders = ", ".join(
        "%s::jsonb" if c == "raw_data" else "%s::text[]" if c == "reason" else "%s"
        for c in columns
    )
    cols_sql = ", ".join(columns)
    upsert_sql = f"""
        INSERT INTO finance_expenses ({cols_sql})
        VALUES %s
        ON CONFLICT (row_id) DO UPDATE SET
            {', '.join(f"{c} = EXCLUDED.{c}" for c in columns if c != 'row_id')}
    """
    with conn:
        with conn.cursor() as cur:
            execute_values(cur, upsert_sql, values, template=f"({placeholders})", page_size=500)

    return len(values)


def sync_request_sheet(ss_client, conn, sheet_id, category_label=None, is_archived=False, archive_source_sheet=None):
    sheet = robust_api_call(ss_client.Sheets.get_sheet, sheet_id)
    if not sheet:
        label = archive_source_sheet or category_label or sheet_id
        raise RuntimeError(f"get_sheet failed for {label} (sheet_id={sheet_id}) after retries")
    col_titles = {c.id: c.title for c in sheet.columns}

    parent_records = []
    for row in sheet.rows:
        record = {
            "row_id": row.id,
            "sheet_id": sheet_id,
            "is_archived": is_archived,
            "archive_source_sheet": archive_source_sheet,
        }
        raw = {}
        line_items = {}  # line_number -> {field: value}

        for cell in row.cells:
            title = col_titles.get(cell.column_id)
            if title is None:
                continue
            value = cell_value(cell)
            raw[title] = value

            m = LINE_ITEM_RE.match(title.strip())
            if m:
                field, num = m.groups()
                # unsuffixed = line 1 (e.g. "Posting Code", "Vehicle Reg")
                line_no = int(num) if num else 1
                field_map = {
                    "Posting Code": "posting_code",
                    "Vehicle Reg": "vehicle_reg",
                    "Odometer Reading": "odometer_reading",
                    "Description": "description",
                    "Amount": "amount",
                }
                # Skip the top-level "Description"/"Amount" columns (line 1
                # duplicates the parent's own Description/Amount fields) —
                # only treat suffixed Description N / Amount N (N>=2) as
                # line items to avoid double-counting line 1 against the
                # parent record.
                if field in ("Description", "Amount") and not num:
                    pass
                else:
                    line_field = field_map[field]
                    if line_field in LINE_ITEM_NUMERIC_FIELDS:
                        value = parse_numeric(cell_numeric_raw(cell), field_name=line_field, context=f"row_id={row.id} line={line_no}")
                    line_items.setdefault(line_no, {})[line_field] = value
                continue

            mapped = NORMALIZED_REQUEST_COLUMN_MAP.get(normalize_title(title))
            if mapped:
                if mapped in REQUEST_NUMERIC_FIELDS:
                    value = parse_numeric(cell_numeric_raw(cell), field_name=mapped, context=f"row_id={row.id}")
                elif mapped in REQUEST_BOOL_FIELDS:
                    value = parse_bool(value)
                record[mapped] = value

        # request_category: live sheets have one category per sheet (the
        # sheet itself IS the category); archive sheets mix all categories
        # in one place, so derive it per-row from Type of Request instead.
        if is_archived:
            raw_type = record.get("type_of_request")
            record["request_category"] = ARCHIVE_CATEGORY_NORMALIZE.get(raw_type, raw_type or "Archived - Unspecified")
        else:
            record["request_category"] = category_label

        record["raw_data"] = raw
        record["row_modified_at"] = row.modified_at.isoformat() if row.modified_at else None
        record["_line_items"] = line_items
        parent_records.append(record)

    active_row_ids = [row.id for row in sheet.rows]

    if not parent_records:
        # Sheet is now empty (e.g. every pending request in a live category
        # sheet got paid and moved to archive) — still need to clear out
        # any rows we previously synced from it, or they go stale forever.
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM finance_payment_requests WHERE sheet_id = %s", (sheet_id,))
        return 0, 0

    parent_columns = [
        "row_id", "sheet_id", "request_category", "finance_ref",
        "date_submitted", "request_date", "type_of_request", "requested_by",
        "payee_name", "description", "item_or_vehicle_count", "amount",
        "total_zar", "total_usd", "conversion_rate", "is_vatable",
        "paid_status", "paid_by", "is_high_priority", "standard_approval",
        "executive_manager", "executive_approval", "return_approval",
        "signature", "signature_authoriser", "is_archived",
        "archive_source_sheet", "raw_data", "row_modified_at", "last_synced_at",
    ]
    values = []
    for r in parent_records:
        values.append(tuple(
            json.dumps(r.get("raw_data", {})) if col == "raw_data"
            else datetime.now(timezone.utc).isoformat() if col == "last_synced_at"
            else r.get(col)
            for col in parent_columns
        ))

    placeholders = ", ".join("%s::jsonb" if c == "raw_data" else "%s" for c in parent_columns)
    cols_sql = ", ".join(parent_columns)
    upsert_sql = f"""
        INSERT INTO finance_payment_requests ({cols_sql})
        VALUES %s
        ON CONFLICT (row_id) DO UPDATE SET
            {', '.join(f"{c} = EXCLUDED.{c}" for c in parent_columns if c != 'row_id')}
        RETURNING id, row_id
    """

    id_by_row_id = {}
    with conn:
        with conn.cursor() as cur:
            # execute_values(..., fetch=True) consumes the cursor internally
            # and returns the combined RETURNING rows as ITS OWN return
            # value — it does not leave anything for a separate cur.fetchall()
            # call afterward. The previous version discarded this return
            # value and called cur.fetchall() itself, which got nothing,
            # silently leaving id_by_row_id empty for every row on every
            # run — the actual cause of line items always coming out at 0.
            returned_rows = execute_values(
                cur, upsert_sql, values, template=f"({placeholders})", page_size=500, fetch=True
            )
            for req_id, row_id in returned_rows:
                id_by_row_id[row_id] = req_id

            # Remove rows that used to live in this sheet but are no longer
            # present (moved to archive, deleted, etc.) — same pattern as
            # Sync_WorldRisk.py's active_row_ids cleanup. Without this, a
            # paid request stays stuck under its old row_id/sheet_id here
            # AND gets re-synced under a new row_id in the archive sheet,
            # double-counting it in totals.
            cur.execute(
                "DELETE FROM finance_payment_requests WHERE sheet_id = %s AND row_id != ALL(%s::bigint[])",
                (sheet_id, active_row_ids),
            )

    # line items — delete + reinsert per request is simplest/safest given
    # small per-request item count (<=8) and avoids partial-update drift
    item_rows = []
    for r in parent_records:
        req_id = id_by_row_id.get(r["row_id"])
        if not req_id:
            continue
        for line_no, fields in r["_line_items"].items():
            if not any(fields.values()):
                continue
            item_rows.append((
                req_id, line_no,
                fields.get("posting_code"), fields.get("vehicle_reg"),
                fields.get("odometer_reading"), fields.get("description"),
                fields.get("amount"),
            ))

    if item_rows:
        request_ids = tuple({r[0] for r in item_rows})
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM finance_payment_request_items WHERE request_id = ANY(%s::uuid[])",
                    (list(request_ids),),
                )
                execute_values(
                    cur,
                    """INSERT INTO finance_payment_request_items
                       (request_id, line_number, posting_code, vehicle_reg,
                        odometer_reading, description, amount)
                       VALUES %s""",
                    item_rows,
                    page_size=500,
                )

    return len(values), len(item_rows)


def main():
    acquire_lock()
    conn = None
    try:
        start_time = time.time()

        # Precise once-daily full rescan at exactly 00:00 — NOT the whole
        # midnight hour, since an every-minute cron would otherwise trigger
        # 60 consecutive full scans during hour 0. Every other minute of
        # the day is a delta run: cheap metadata scan, only fetch/process a
        # sheet's full row data if its modified_at moved since last sync.
        now = datetime.now()
        force_full_sync = (now.hour == 0 and now.minute == 0)
        sync_mode = "🌙 Midnight Full Rescan" if force_full_sync else "☀️ Delta Sync (state-anchored)"
        print(f"🚀 *FINANCE SYNC STARTED* — Mode: {sync_mode}")

        ss_client = get_smartsheet_client()
        conn = get_db_connection()

        sheets = discover_finance_sheets(ss_client)
        sheet_ids = [s[0] for s in sheets]
        known_state = load_sync_state(conn, sheet_ids)

        ledger_total = 0
        request_total, item_total, archive_total = 0, 0, 0
        ledger_count, request_live_count, request_archived_count = 0, 0, 0
        sheets_checked, sheets_skipped, sheets_failed = 0, 0, 0

        for sheet_id, sheet_name, kind, category, modified_at in sheets:
            last_synced = known_state.get(sheet_id)
            if not should_process_sheet(modified_at, last_synced, force_full_sync):
                sheets_skipped += 1
                continue

            try:
                if kind == "ledger":
                    ledger_count += 1
                    ledger_total += sync_ledger_sheet(ss_client, conn, sheet_id, sheet_name)

                elif kind == "request_live":
                    request_live_count += 1
                    r, i = sync_request_sheet(ss_client, conn, sheet_id, category_label=category, is_archived=False)
                    request_total += r
                    item_total += i

                elif kind == "request_archived":
                    request_archived_count += 1
                    r, i = sync_request_sheet(ss_client, conn, sheet_id, is_archived=True, archive_source_sheet=sheet_name)
                    request_total += r
                    item_total += i
                    archive_total += r

                # State updated ONLY on success — same guarantee
                # Sync_WorldRisk.py gives: a failure anywhere above leaves
                # this sheet's state untouched, so it gets retried on the
                # very next delta run instead of being silently marked
                # checked while never actually synced.
                with conn.cursor() as cur:
                    update_sync_state(cur, sheet_id, sheet_name, kind, category, modified_at)
                conn.commit()
                sheets_checked += 1

            except Exception as e:
                sheets_failed += 1
                # str(e) alone can be genuinely unhelpful (some exceptions —
                # e.g. certain psycopg2/KeyError cases — stringify to
                # something as opaque as "(None, None)"). Full traceback so
                # a real failure is diagnosable from the log without having
                # to reproduce it separately.
                print(f"   ⚠️ {sheet_name}: sync failed, will retry next run")
                print(f"      Exception type: {type(e).__name__}")
                traceback.print_exc()
                conn.rollback()

        duration = round(time.time() - start_time, 2)
        summary = (
            f"📂 Sheets: checked `{sheets_checked}`, skipped (unchanged) `{sheets_skipped}`, "
            f"failed `{sheets_failed}`\n"
            f"📒 Ledger rows: `{ledger_total}` (`{ledger_count}` ledger sheets)\n"
            f"🧾 Payment requests: `{request_total}` (live sheets: `{request_live_count}`, "
            f"archive sheets: `{request_archived_count}`, archived rows: `{archive_total}`)\n"
            f"📋 Line items: `{item_total}`\n"
            f"⏱️ Time: `{duration}s`"
        )
        print(summary)

        # Every-minute cadence means a purely quiet delta tick (nothing
        # changed, nothing failed) happens most of the time — sending a
        # Telegram message every single one of those would be ~1,400
        # messages/day of noise. Only alert on the daily full rescan, or
        # any run that actually did something or hit a failure.
        if force_full_sync or sheets_checked > 0 or sheets_failed > 0:
            send_telegram(f"✅ *FINANCE SYNC* — {sync_mode}\n{summary}")

    except Exception as e:
        crash_msg = f"🔥 *FINANCE SYNC CRITICAL FAILURE*\nError: `{str(e)}`"
        print(crash_msg)
        send_telegram(crash_msg)

    finally:
        if conn:
            conn.close()
        release_lock()


if __name__ == "__main__":
    main()