import os
import sys
import smartsheet
import requests
import time
import warnings
import threading
import queue
import logging
import psycopg2
import psycopg2.extras
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from dateutil import parser
from dotenv import load_dotenv
from tqdm import tqdm
from contextlib import contextmanager
from license_manager import JaysNetworkLicense

# =============================================================================
# 0. ENTERPRISE LOGGING
# =============================================================================
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler = RotatingFileHandler(
    '/var/log/jaysnet/border_bot.log', maxBytes=5 * 1024 * 1024, backupCount=5
)
log_handler.setFormatter(log_formatter)

logger = logging.getLogger('BorderBot')
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

# Also log to stdout so cron captures it
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# =============================================================================
# 1. ENVIRONMENT
# =============================================================================
load_dotenv()

SMARTSHEET_ACCESS_TOKEN = os.getenv("SMARTSHEET_ACCESS_TOKEN")
WORKSPACE_ID            = os.getenv("WorldRisk_Tracking_Sheets")

TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")   # Main Ops Alert Bot
LOG_BOT_TOKEN           = os.getenv("LOG_BOT_TOKEN")         # Risk Log / Admin Bot

TELEGRAM_CHAT_ID        = os.getenv("TELEGRAM_BORDER_CROSSING_OVERDUE_ID")  # Main Ops Group
RISK_LOG_ID             = os.getenv("RISK_LOG_ID")           # WorldRisk Log Group
ADMIN_ID                = os.getenv("ADMIN_TELEGRAM_ID")     # Admin Direct Message

HEALTHCHECK_URL         = os.getenv("HEALTHCHECK_URL")

_REQUIRED = [SMARTSHEET_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, LOG_BOT_TOKEN, WORKSPACE_ID]
if not all(_REQUIRED):
    logger.critical("Missing required environment variables. Exiting.")
    raise SystemExit(1)

warnings.filterwarnings("ignore", category=DeprecationWarning)

# =============================================================================
# 1b. LICENSE HANDSHAKE
# =============================================================================
logger.info("Authenticating with Gatekeeper...")
_license_key = os.getenv('LICENSE_KEY') or os.getenv('JAYS_NETWORK_LICENSE_KEY')

if not _license_key:
    err = "LICENSE ERROR: Key missing in .env — Border Bot aborted."
    logger.critical(err)
    print(err)
    sys.exit(1)

_auth       = JaysNetworkLicense(_license_key)
credentials = _auth.authenticate()

if not credentials:
    err = "ACCESS DENIED: License validation failed — Border Bot aborted."
    logger.critical(err)
    print(err)
    sys.exit(1)

logger.info("ACCESS GRANTED: Secure connection established.")

# =============================================================================
# 1c. RUN MODE
# =============================================================================
RESYNC_MODE  = "--resync"  in sys.argv
CLEANUP_MODE = "--cleanup" in sys.argv

if RESYNC_MODE:
    logger.info("=" * 60)
    logger.info("RESYNC MODE — populating DB from current Smartsheet state.")
    logger.info("No alerts will fire. No Smartsheet writes. Store-only.")
    logger.info("=" * 60)

if CLEANUP_MODE:
    logger.info("=" * 60)
    logger.info("CLEANUP MODE — clearing stale alert checkboxes from Smartsheet.")
    logger.info("Processes all sheets. DB untouched. Alerts suppressed.")
    logger.info("=" * 60)

# =============================================================================
# 2. CLIENTS & CONFIG
# =============================================================================
ss_client = smartsheet.Smartsheet(SMARTSHEET_ACCESS_TOKEN)
ss_client.errors_as_exceptions = True

MAIN_BOT_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
LOG_BOT_URL  = f"https://api.telegram.org/bot{LOG_BOT_TOKEN}/sendMessage"

# --- Column Name Variants ---
VARIANTS_TRANSPORTER = ["Transporter", "TRANSPORTER", "Carrier", "Haulier", "Sub-Contractor"]
VARIANTS_REG         = ["Reg", "REG", "Registration", "Horse Reg", "HORSE REG", "Vehicle Reg", "Truck Reg", "Registration No", "Reg Number"]
VARIANTS_DEVICE      = ["Device", "DEVICE", "Device Number", "DEVICE NUMBER", "Unit Number", "Unit ID", "Tracker ID"]
VARIANTS_LOCATION    = ["Current Location", "CURRENT LOCATION", "Location", "Current Loc", "Present Location", "Ping Location"]
VARIANTS_STATUS      = [
    "Status", "STATUS", "Transit Status", "TRANSIT STATUS",
    "Current Status", "State", "Border Crossing", "BORDER CROSSING"
]

TARGET_STATUS_KEYWORD   = "BORDER CROSSING"
OVERDUE_COLUMN          = "Overdue Alert"
IMAGE_FAIL_COLUMN       = "Image Missing Alert"

# --- Timing Config (all in hours) ---
IMAGE_PHASE1_HOURS   = 12
IMAGE_PHASE2_HOURS   = 13
CYCLE_HOURS          = 3

SYNC_WINDOW_HOURS    = 0.25

# --- Runtime Config ---
MAX_RETRIES  = 3
NUM_THREADS  = int(os.getenv("BORDER_BOT_THREADS", 5))

# =============================================================================
# 3. POSTGRESQL CONNECTION
# =============================================================================
@contextmanager
def get_pg_conn():
    conn = psycopg2.connect(
        host=credentials["url"],
        user=credentials["key"],
        password=credentials["pass"],
        database="jaysnet_data",
        port=5432
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def ensure_border_tables():
    create_border_logs = """
    CREATE TABLE IF NOT EXISTS border_logs (
        id                  SERIAL PRIMARY KEY,
        smartsheet_row_id   BIGINT UNIQUE NOT NULL,
        sheet_id            BIGINT,
        is_active           BOOLEAN DEFAULT TRUE,
        is_overdue          BOOLEAN DEFAULT FALSE,
        transporter         TEXT,
        registration        TEXT,
        current_location    TEXT,
        border_entry_time   TIMESTAMPTZ NOT NULL,
        last_alert_sent_at  TIMESTAMPTZ,
        image_check_passed  BOOLEAN DEFAULT FALSE,
        alert_phase         INTEGER DEFAULT 0,
        last_phase2_alert_at TIMESTAMPTZ,
        created_at          TIMESTAMPTZ DEFAULT NOW(),
        updated_at          TIMESTAMPTZ DEFAULT NOW()
    );
    ALTER TABLE border_logs ADD COLUMN IF NOT EXISTS alert_phase          INTEGER DEFAULT 0;
    ALTER TABLE border_logs ADD COLUMN IF NOT EXISTS last_phase2_alert_at TIMESTAMPTZ;
    CREATE INDEX IF NOT EXISTS idx_border_logs_row_id ON border_logs(smartsheet_row_id);
    CREATE INDEX IF NOT EXISTS idx_border_logs_active  ON border_logs(is_active);
    """
    create_bot_health = """
    CREATE TABLE IF NOT EXISTS bot_health (
        id                  SERIAL PRIMARY KEY,
        bot_name            TEXT UNIQUE NOT NULL,
        last_run_start      TIMESTAMPTZ,
        last_run_end        TIMESTAMPTZ,
        status              TEXT,
        sheets_processed    INTEGER DEFAULT 0,
        duration_seconds    NUMERIC DEFAULT 0
    );
    INSERT INTO bot_health (bot_name, status)
    VALUES ('border_bot', 'initialised')
    ON CONFLICT (bot_name) DO NOTHING;
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(create_border_logs)
            cur.execute(create_bot_health)
    logger.info("DB tables verified / created.")

# =============================================================================
# 4. THREADING, STATS & ALERT COLLECTOR
# =============================================================================
sheet_queue = queue.Queue()
msg_queue   = queue.Queue()

class BotStats:
    def __init__(self):
        self.sheets_scanned  = 0
        self.sheets_skipped  = 0
        self.alerts_sent     = 0
        self.errors          = 0
        self.rows_updated    = 0
        self.new_entries     = 0
        self.still_on_timer  = 0
        self.phase1_count    = 0
        self.phase2_count    = 0
        self._lock           = threading.Lock()

    def update(self, key, value=1):
        with self._lock:
            setattr(self, key, getattr(self, key) + value)

stats = BotStats()

# --- Alert collector: groups all fired alerts by sheet name ---
# Populated by collect_alert() during sheet processing.
# Flushed into a single grouped report at end of run.
alert_collector: dict      = {}
alert_collector_lock       = threading.Lock()

def collect_alert(sheet_name: str, entry: str):
    """Thread-safe collector — buffers alert lines grouped by sheet name."""
    with alert_collector_lock:
        if sheet_name not in alert_collector:
            alert_collector[sheet_name] = []
        alert_collector[sheet_name].append(entry)

# =============================================================================
# 5. TELEGRAM DISPATCH
# =============================================================================
def message_worker():
    """Asynchronous Telegram sender — drains msg_queue until sentinel None."""
    while True:
        try:
            item = msg_queue.get()
            if item is None:
                break
            url, payload = item
            try:
                requests.post(url, json=payload, timeout=15)
            except Exception as e:
                logger.error(f"Telegram send failed: {e}")
            msg_queue.task_done()
        except Exception:
            msg_queue.task_done()

def send_group_alert(text):
    """Ops report → Main Border Crossing Overdue Group (Markdown)."""
    msg_queue.put((MAIN_BOT_URL, {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}))

def send_risk_log(text):
    """Summary log → WorldRisk Log Group (Markdown)."""
    if RISK_LOG_ID:
        msg_queue.put((LOG_BOT_URL, {"chat_id": RISK_LOG_ID, "text": text, "parse_mode": "Markdown"}))

def send_system_health(text):
    """Critical error → Admin direct message (Markdown)."""
    if ADMIN_ID:
        msg_queue.put((LOG_BOT_URL, {"chat_id": ADMIN_ID, "text": text, "parse_mode": "Markdown"}))

# =============================================================================
# 6. HEALTH & MONITORING
# =============================================================================
def ping_healthcheck(status="start"):
    if not HEALTHCHECK_URL:
        return
    try:
        url = HEALTHCHECK_URL
        if status == "start": url = f"{HEALTHCHECK_URL}/start"
        elif status == "fail": url = f"{HEALTHCHECK_URL}/fail"
        requests.get(url, timeout=10)
    except Exception:
        pass

def update_db_heartbeat(status="running", duration=0):
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                if status == "running":
                    cur.execute(
                        "UPDATE bot_health SET last_run_start=%s, status=%s WHERE bot_name='border_bot'",
                        (now, "running")
                    )
                else:
                    cur.execute(
                        """UPDATE bot_health
                           SET last_run_end=%s, status=%s,
                               sheets_processed=%s, duration_seconds=%s
                           WHERE bot_name='border_bot'""",
                        (now, status, stats.sheets_scanned, duration)
                    )
    except Exception as e:
        logger.warning(f"Heartbeat update failed: {e}")

# =============================================================================
# 7. DATABASE HELPERS
# =============================================================================
def fetch_active_brain_sheets():
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT sheet_id FROM border_logs WHERE is_active=TRUE AND sheet_id IS NOT NULL")
                return {row[0] for row in cur.fetchall()}
    except Exception as e:
        logger.warning(f"fetch_active_brain_sheets failed: {e}")
        return set()

def fetch_logs_batch(row_ids: list) -> dict:
    if not row_ids:
        return {}
    logs_map = {}
    chunk_size = 40
    for i in range(0, len(row_ids), chunk_size):
        chunk = row_ids[i:i + chunk_size]
        for attempt in range(3):
            try:
                with get_pg_conn() as conn:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute(
                            "SELECT * FROM border_logs WHERE smartsheet_row_id = ANY(%s)",
                            (chunk,)
                        )
                        for log in cur.fetchall():
                            logs_map[log['smartsheet_row_id']] = dict(log)
                break
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"Batch fetch failed after 3 attempts: {e}")
                else:
                    time.sleep(1)
    return logs_map

def db_insert_border_log(row_id, sheet_id, transporter, registration, location, entry_time):
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO border_logs
                       (smartsheet_row_id, sheet_id, is_active, transporter, registration,
                        current_location, border_entry_time, alert_phase,
                        image_check_passed, last_phase2_alert_at)
                       VALUES (%s, %s, TRUE, %s, %s, %s, %s, 0, FALSE, NULL)
                       ON CONFLICT (smartsheet_row_id) DO UPDATE SET
                           is_active            = TRUE,
                           alert_phase          = 0,
                           image_check_passed   = FALSE,
                           last_phase2_alert_at = NULL,
                           border_entry_time    = EXCLUDED.border_entry_time,
                           transporter          = EXCLUDED.transporter,
                           registration         = EXCLUDED.registration,
                           current_location     = EXCLUDED.current_location,
                           updated_at           = NOW()
                       WHERE border_logs.is_active = FALSE""",
                    (row_id, sheet_id, transporter, registration, location, entry_time)
                )
    except Exception as e:
        logger.warning(f"Insert border_log failed (row {row_id}): {e}")

def db_mark_overdue(row_id, now_iso):
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE border_logs
                       SET last_alert_sent_at=%s, is_overdue=TRUE, updated_at=NOW()
                       WHERE smartsheet_row_id=%s""",
                    (now_iso, row_id)
                )
    except Exception as e:
        logger.warning(f"Mark overdue failed (row {row_id}): {e}")

def db_set_alert_phase(row_id, phase: int, phase2_time_iso=None):
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                if phase == 2 and phase2_time_iso:
                    cur.execute(
                        """UPDATE border_logs
                           SET alert_phase=2, last_phase2_alert_at=%s, updated_at=NOW()
                           WHERE smartsheet_row_id=%s""",
                        (phase2_time_iso, row_id)
                    )
                else:
                    cur.execute(
                        """UPDATE border_logs
                           SET alert_phase=%s, updated_at=NOW()
                           WHERE smartsheet_row_id=%s""",
                        (phase, row_id)
                    )
    except Exception as e:
        logger.warning(f"Set alert_phase failed (row {row_id}): {e}")

def db_update_image_check(log_id, passed: bool):
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE border_logs SET image_check_passed=%s, updated_at=NOW() WHERE id=%s",
                    (passed, log_id)
                )
    except Exception as e:
        logger.warning(f"Image check update failed (log {log_id}): {e}")

def db_soft_close_logs(row_ids: list):
    if not row_ids:
        return
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE border_logs
                       SET is_active=FALSE, is_overdue=FALSE, updated_at=NOW()
                       WHERE smartsheet_row_id = ANY(%s) AND is_active=TRUE""",
                    (row_ids,)
                )
    except Exception as e:
        logger.warning(f"Soft-close logs failed: {e}")

def db_ensure_sheet_id(log_id, sheet_id):
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE border_logs SET sheet_id=%s WHERE id=%s AND sheet_id IS NULL",
                    (sheet_id, log_id)
                )
    except Exception:
        pass

# =============================================================================
# 8. SMARTSHEET HELPERS
# =============================================================================
def find_column_id(sheet_columns, variants):
    col_map = {col.title: col.id for col in sheet_columns}
    for v in variants:
        if v in col_map:
            return col_map[v]
    return None

def safe_get_sheet(sheet_id):
    """
    ss_client.Sheets.get_sheet() is expected to raise an exception on API
    errors (errors_as_exceptions=True), but this doesn't reliably cover
    every failure mode. Confirmed in production logs: a raw gateway-level
    503 with a plain-text (non-JSON) body causes the SDK's error-object
    deserialization to fall back to a bare smartsheet.models.Error instead
    of raising, since it never reaches a well-formed API error it can
    convert into an exception. Left unguarded, this crashes deep inside
    ensure_columns() with the confusing "'Error' object has no attribute
    'columns'". This check turns that into a clean, immediately-raised,
    correctly-labeled exception that the existing sheet_worker retry logic
    (3 attempts, staggered backoff) already handles correctly — no change
    needed there.
    """
    sheet = ss_client.Sheets.get_sheet(sheet_id)
    if not hasattr(sheet, "columns"):
        raise RuntimeError(
            f"get_sheet returned malformed response (no .columns) for sheet_id={sheet_id}: {sheet}"
        )
    return sheet

def ensure_columns(sheet_id, full_sheet):
    col_map  = {col.title: col.id for col in full_sheet.columns}
    new_cols = []
    current_index = len(full_sheet.columns)

    if OVERDUE_COLUMN not in col_map:
        new_cols.append(smartsheet.models.Column({
            "title": OVERDUE_COLUMN, "type": "CHECKBOX",
            "hidden": True, "locked": True, "index": current_index
        }))
    if IMAGE_FAIL_COLUMN not in col_map:
        new_cols.append(smartsheet.models.Column({
            "title": IMAGE_FAIL_COLUMN, "type": "CHECKBOX",
            "hidden": True, "locked": True, "index": current_index + len(new_cols)
        }))
    if new_cols:
        try:
            result = ss_client.Sheets.add_columns(sheet_id, new_cols)
            full_sheet.columns.extend(result.result)
        except Exception as e:
            logger.warning(f"Could not add columns to sheet {sheet_id}: {e}")
    return full_sheet

def get_cell_val(row_obj, col_id):
    if col_id is None:
        return ""
    c = next((x for x in row_obj.cells if x.column_id == col_id), None)
    return str(c.value).strip() if c and c.value is not None else ""

# =============================================================================
# 9. CORE SHEET PROCESSING
# =============================================================================
def process_single_sheet(sheet_id, sheet_name, resync=False, cleanup=False):
    """
    Called every 10-min cron pass for:
      (a) Sheets modified in the last 15 min  — catches NEW border crossings
      (b) Sheets in active_brain_sheets       — watches EXISTING border crossings

    Three fully decoupled operations run each pass:
      ┌─ CHECK 1: STATUS     (every 10 min) ─ Is the truck still BORDER CROSSING?
      ├─ CHECK 2: ATTACHMENT (every 10 min) ─ Has an image been uploaded?
      └─ FIRE:    ALERT CLOCK (phase only)  ─ P1@12h → P2@13h → 3h cycle

    Alerts are NOT sent immediately — they are collected via collect_alert()
    and flushed as a single grouped report at the end of the run.

    resync=True  : store-only mode — no alerts, no Smartsheet writes.
    cleanup=True : clears stale checkboxes on non-BORDER CROSSING rows only.
    """
    sheet = safe_get_sheet(sheet_id)
    sheet = ensure_columns(sheet_id, sheet)

    status_id      = find_column_id(sheet.columns, VARIANTS_STATUS)
    reg_id         = find_column_id(sheet.columns, VARIANTS_REG)
    transporter_id = find_column_id(sheet.columns, VARIANTS_TRANSPORTER)
    location_id    = find_column_id(sheet.columns, VARIANTS_LOCATION)

    if not status_id or not reg_id:
        return

    col_map     = {col.title: col.id for col in sheet.columns}
    overdue_id  = col_map.get(OVERDUE_COLUMN)
    img_fail_id = col_map.get(IMAGE_FAIL_COLUMN)

    if not overdue_id or not img_fail_id:
        return

    rows_to_update = []
    now            = datetime.now(timezone.utc)

    # =========================================================================
    # CHECK 1 — STATUS CHECK
    # =========================================================================
    target_rows = []
    other_rows  = []
    for row in sheet.rows:
        status_val = get_cell_val(row, status_id).upper()
        if TARGET_STATUS_KEYWORD in status_val:
            target_rows.append(row)
        else:
            other_rows.append(row)

    target_ids      = [r.id for r in target_rows]
    active_logs_map = fetch_logs_batch(target_ids)

    for row in target_rows:
        reg_val    = get_cell_val(row, reg_id)
        trans_val  = get_cell_val(row, transporter_id)
        loc_val    = get_cell_val(row, location_id)
        active_log = active_logs_map.get(row.id)

        if not active_log:
            if resync:
                raw_mod = getattr(row, 'modifiedAt', None) or getattr(row, 'modified_at', None)
                entry_time = str(raw_mod) if raw_mod else now.isoformat()
            else:
                entry_time = now.isoformat()

            db_insert_border_log(
                row_id=row.id, sheet_id=sheet_id,
                transporter=trans_val, registration=reg_val,
                location=loc_val, entry_time=entry_time
            )
            stats.update("new_entries")

            if resync:
                logger.info(f"  [RESYNC] Stored: {reg_val} | entry_time={entry_time[:19]}")
            continue

        if not active_log.get('sheet_id'):
            db_ensure_sheet_id(active_log['id'], sheet_id)

        if resync:
            continue

        stats.update("still_on_timer")

        db_start       = parser.isoparse(str(active_log['border_entry_time']))
        duration_hours = (now - db_start).total_seconds() / 3600

        # =====================================================================
        # CHECK 2 — ATTACHMENT CHECK
        # =====================================================================
        alert_phase  = active_log.get("alert_phase") or 0
        img_fail_val = get_cell_val(row, img_fail_id).lower()
        hours_str    = f"{int(duration_hours)}h"

        if duration_hours >= IMAGE_PHASE1_HOURS and alert_phase >= 1:
            try:
                atts      = ss_client.Attachments.list_row_attachments(sheet_id, row.id)
                has_image = len(atts.data) > 0
            except Exception:
                has_image = False

            if has_image:
                db_set_alert_phase(row.id, 1, now.isoformat())
                if img_fail_val == "true":
                    c_dict   = {'columnId': img_fail_id, 'value': False}
                    existing = next((r for r in rows_to_update if r.id == row.id), None)
                    if existing: existing.cells.append(smartsheet.models.Cell(c_dict))
                    else: rows_to_update.append(smartsheet.models.Row({'id': row.id, 'cells': [c_dict]}))
                logger.info(
                    f"Attachment found: {reg_val} ({int(duration_hours)}h) "
                    f"— P1 hold reset, next alert check in {CYCLE_HOURS}h."
                )
                continue

        if cleanup:
            continue

        # =====================================================================
        # FIRE — ALERT CLOCK
        # Alerts collected here — NOT sent immediately.
        # They are grouped by sheet and sent as one report at end of run.
        # =====================================================================
        if duration_hours >= IMAGE_PHASE1_HOURS:

            # ── Phase 1: first alert at 12h ───────────────────────────────────
            if alert_phase == 0:
                collect_alert(
                    sheet_name,
                    f"• {reg_val} | {trans_val} — {hours_str} | ⚠️ PHASE 1 | {loc_val}"
                )
                if img_fail_val != "true":
                    c_dict = {'columnId': img_fail_id, 'value': True}
                    rows_to_update.append(smartsheet.models.Row({'id': row.id, 'cells': [c_dict]}))
                db_set_alert_phase(row.id, 1, now.isoformat())
                stats.update("alerts_sent")
                stats.update("phase1_count")
                logger.info(f"Phase 1 collected: {reg_val} ({int(duration_hours)}h)")

            # ── Cycle engine: P2 at 13h, then P1/P2 every CYCLE_HOURS ─────────
            elif alert_phase >= 1 and duration_hours >= IMAGE_PHASE2_HOURS:
                last_cycle_str    = active_log.get("last_phase2_alert_at")
                hours_since_cycle = 999

                if last_cycle_str:
                    last_cycle_time   = parser.isoparse(str(last_cycle_str))
                    hours_since_cycle = (now - last_cycle_time).total_seconds() / 3600

                if hours_since_cycle >= CYCLE_HOURS:
                    next_phase = alert_phase + 1
                    is_phase2  = (next_phase % 2 == 0)

                    if img_fail_val != "true":
                        c_dict   = {'columnId': img_fail_id, 'value': True}
                        existing = next((r for r in rows_to_update if r.id == row.id), None)
                        if existing: existing.cells.append(smartsheet.models.Cell(c_dict))
                        else: rows_to_update.append(smartsheet.models.Row({'id': row.id, 'cells': [c_dict]}))

                    if is_phase2:
                        collect_alert(
                            sheet_name,
                            f"• {reg_val} | {trans_val} — {hours_str} | 🚨 PHASE 2 | {loc_val}"
                        )
                        stats.update("phase2_count")
                    else:
                        collect_alert(
                            sheet_name,
                            f"• {reg_val} | {trans_val} — {hours_str} | ⚠️ PHASE 1 REMINDER | {loc_val}"
                        )
                        stats.update("phase1_count")

                    db_set_alert_phase(row.id, next_phase, now.isoformat())
                    stats.update("alerts_sent")
                    logger.info(
                        f"{'Phase 2' if is_phase2 else 'Phase 1 reminder'} collected: "
                        f"{reg_val} ({int(duration_hours)}h, phase_counter={next_phase})"
                    )

    # =========================================================================
    # STATUS CLOSE — trucks no longer in BORDER CROSSING
    # =========================================================================
    other_ids = [r.id for r in other_rows]
    if other_ids:
        if cleanup:
            cleared = 0
            for row_obj in other_rows:
                cl_cells = []
                if get_cell_val(row_obj, overdue_id).lower() == "true":
                    cl_cells.append({'columnId': overdue_id, 'value': False})
                if get_cell_val(row_obj, img_fail_id).lower() == "true":
                    cl_cells.append({'columnId': img_fail_id, 'value': False})
                if cl_cells:
                    rows_to_update.append(smartsheet.models.Row({'id': row_obj.id, 'cells': cl_cells}))
                    cleared += 1
            if cleared:
                logger.info(f"  [CLEANUP] {sheet_name}: clearing {cleared} stale checkbox rows")
        else:
            stale_logs          = fetch_logs_batch(other_ids)
            rows_with_stale_log = [rid for rid in other_ids if rid in stale_logs]

            if rows_with_stale_log:
                db_soft_close_logs(rows_with_stale_log)

                for row_obj in other_rows:
                    if row_obj.id not in stale_logs:
                        continue
                    cl_cells = []
                    if get_cell_val(row_obj, overdue_id).lower() == "true":
                        cl_cells.append({'columnId': overdue_id, 'value': False})
                    if get_cell_val(row_obj, img_fail_id).lower() == "true":
                        cl_cells.append({'columnId': img_fail_id, 'value': False})
                    if cl_cells:
                        existing = next((r for r in rows_to_update if r.id == row_obj.id), None)
                        if existing:
                            for c in cl_cells: existing.cells.append(smartsheet.models.Cell(c))
                        else:
                            rows_to_update.append(smartsheet.models.Row({'id': row_obj.id, 'cells': cl_cells}))

    if rows_to_update and not resync:
        ss_client.Sheets.update_rows(sheet_id, rows_to_update)
        stats.update("rows_updated", len(rows_to_update))

# =============================================================================
# 10. WORKER THREADS
# =============================================================================
def sheet_worker(pbar, active_brain_sheets):
    while True:
        try:
            item = sheet_queue.get(block=False)
        except queue.Empty:
            return

        sheet_id, sheet_name, sheet_mod_at, retry_count = item

        is_recent = True
        if sheet_mod_at:
            try:
                mod_time = sheet_mod_at if isinstance(sheet_mod_at, datetime) else parser.isoparse(sheet_mod_at)
                if mod_time.tzinfo is None:
                    mod_time = mod_time.replace(tzinfo=timezone.utc)
                is_recent = (datetime.now(timezone.utc) - mod_time).total_seconds() < (SYNC_WINDOW_HOURS * 3600)
            except Exception:
                is_recent = True

        is_active_in_brain = sheet_id in active_brain_sheets

        if not RESYNC_MODE and not CLEANUP_MODE and not is_recent and not is_active_in_brain:
            stats.update("sheets_skipped")
            pbar.update(1)
            sheet_queue.task_done()
            continue

        try:
            process_single_sheet(sheet_id, sheet_name, resync=RESYNC_MODE, cleanup=CLEANUP_MODE)
            stats.update("sheets_scanned")
            pbar.update(1)

        except Exception as e:
            if retry_count < MAX_RETRIES:
                time.sleep((retry_count + 1) * 5)
                sheet_queue.put((sheet_id, sheet_name, sheet_mod_at, retry_count + 1))
                pbar.update(1)
            else:
                logger.error(f"Sheet {sheet_name} failed after {MAX_RETRIES} retries: {e}")
                send_system_health(f"❌ *Sheet Failed*\nName: `{sheet_name}`\nError: `{str(e)}`")
                stats.update("errors")
                pbar.update(1)

        sheet_queue.task_done()

# =============================================================================
# 11. MAIN EXECUTION
# =============================================================================
def run_production_bot():
    start_time = time.time()
    duration   = 0              # defined before try so except can always read it
    alert_collector.clear()     # fresh slate — safe for repeated in-process runs

    logger.info("=" * 60)
    logger.info("Border Bot starting...")

    ensure_border_tables()
    ping_healthcheck("start")
    update_db_heartbeat("running")

    msg_thread = threading.Thread(target=message_worker, daemon=True)
    msg_thread.start()

    if CLEANUP_MODE:
        mode_label = "🧹 CLEANUP — Clearing Stale Checkboxes"
    elif RESYNC_MODE:
        mode_label = "🔄 RESYNC — Store-Only, No Alerts"
    else:
        mode_label = "Production | PostgreSQL Backend"

    send_risk_log(f"🏁 *Border Bot Starting*\nMode: `{mode_label}`")

    try:
        active_brain_sheets = fetch_active_brain_sheets()
        logger.info(f"Active brain sheets: {len(active_brain_sheets)}")

        workspace  = ss_client.Workspaces.get_workspace(WORKSPACE_ID)
        all_sheets = list(workspace.sheets or [])

        def dive(folders):
            for f in folders:
                if "archive" in f.name.lower():
                    continue
                try:
                    folder_obj = ss_client.Folders.get_folder(f.id)
                    all_sheets.extend(folder_obj.sheets or [])
                    if folder_obj.folders:
                        dive(folder_obj.folders)
                except Exception:
                    pass

        if workspace.folders:
            dive(workspace.folders)

        valid_sheets = [s for s in all_sheets if "invoicing" not in s.name.lower()]
        total_count  = len(valid_sheets)
        logger.info(f"Sheets to evaluate: {total_count}")

        for s in valid_sheets:
            sheet_queue.put((s.id, s.name, getattr(s, 'modified_at', None), 0))

        threads = []
        with tqdm(total=total_count, desc="Processing Sheets", unit="sheet") as pbar:
            for _ in range(NUM_THREADS):
                t = threading.Thread(target=sheet_worker, args=(pbar, active_brain_sheets))
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

        duration = round(time.time() - start_time, 1)

        # ── Main ops group: single grouped report ─────────────────────────────
        # All alerts collected during sheet processing are flushed here as one
        # message (or chunked at 4000 chars if very large).
        if alert_collector:
            lines = ["🚨 *BORDER CROSSING ALERT REPORT*\n"]
            for sheet_name in sorted(alert_collector.keys()):
                lines.append(f"📋 *{sheet_name}*")
                for entry in alert_collector[sheet_name]:
                    lines.append(f"  {entry}")
                lines.append("")  # blank line between sheets
            report = "\n".join(lines).strip()
            # Telegram hard limit is 4096 chars — chunk if needed
            for i in range(0, len(report), 4000):
                send_group_alert(report[i:i + 4000])
        else:
            send_group_alert("✅ Border Crossing Check Complete — No active alerts this run.")

        # ── Risk log: clean run summary ───────────────────────────────────────
        summary = (
            f"✅ *Run Complete*\n"
            f"📂 Sheets Scanned: `{stats.sheets_scanned}`\n"
            f"🆕 New Entries: `{stats.new_entries}`\n"
            f"⏱️ Still on Timer: `{stats.still_on_timer}`\n"
            f"📎 Phase 1 Alerts: `{stats.phase1_count}`\n"
            f"🚨 Phase 2 Alerts: `{stats.phase2_count}`\n"
            f"⏱️ Duration: `{duration}s`"
        )
        send_risk_log(summary)

        # Drain AFTER everything is queued — order is critical
        msg_queue.put(None)
        msg_thread.join()

        logger.info(f"Run complete in {duration}s")
        ping_healthcheck("success")
        update_db_heartbeat("completed", duration)

    except Exception as e:
        logger.exception("Critical bot failure")
        send_system_health(f"🚨 *CRITICAL FAILURE*\nError: `{str(e)}`")
        msg_queue.put(None)
        msg_thread.join()
        ping_healthcheck("fail")
        update_db_heartbeat("failed", duration)
        raise

if __name__ == "__main__":
    run_production_bot()