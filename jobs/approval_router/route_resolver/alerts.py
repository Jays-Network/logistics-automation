"""Telegram alerting (raw send + de-duplicated unresolvable-route alert)."""

import os

import requests

from .settings import JOB_NAME


def send_telegram(message: str) -> tuple[bool, str | None]:
    """
    Raw send, same shape as every other job in this repo (Sync_ADARS.py,
    Sync_WorldRisk.py, etc.) — LOG_BOT_TOKEN + a chat id. Returns
    (delivered, delivery_error) rather than raising, so a Telegram outage
    never breaks the actual routing run — it's logged into
    automation_telegram_alert_log either way.

    Chat id: falls back to RISK_LOG_ID (already used by WorldRisk/
    MasterRotation for operational alerts) if a dedicated
    ROUTE_RESOLVER_LOG_ID isn't set — Jay can add that env var later for a
    separate channel without any code change.
    """
    token = os.getenv("LOG_BOT_TOKEN")
    chat_id = os.getenv("ROUTE_RESOLVER_LOG_ID") or os.getenv("RISK_LOG_ID")
    if not token or not chat_id:
        return False, "LOG_BOT_TOKEN or chat_id env var missing"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        if response.status_code == 200:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except Exception as exc:
        return False, str(exc)


def alert_unresolvable_route(conn, client_slug: str, route_value: str,
                               local_route_value: str | None, reason: str):
    """
    Alerts once per distinct (client, route, local_route) combination per
    24h — NOT on every cron tick. Without this de-dupe, a single unresolved
    route (e.g. a new picklist option added without its matching invoicing
    sheet) would re-alert every 10 minutes until someone fixes it, which
    trains people to ignore the channel. Every attempt — sent or
    de-duplicated-away — still gets written to automation_telegram_alert_log
    so there's a real audit trail, unlike the legacy scripts' fire-and-
    forget sends.
    """
    dedupe_key = f"{client_slug}|{route_value}|{local_route_value or ''}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM automation_telegram_alert_log "
            "WHERE job_name = %s AND message LIKE %s AND sent_at > now() - interval '24 hours' LIMIT 1",
            (JOB_NAME, f"%{dedupe_key}%"),
        )
        if cur.fetchone():
            return  # already alerted about this exact route recently

    message = (
        f"⚠️ *route_resolver*: unresolvable ROUTE on *{client_slug}*\n"
        f"ROUTE: `{route_value}`\n"
        + (f"LOCAL ROUTE: `{local_route_value}`\n" if local_route_value else "")
        + f"Reason: {reason}\n"
        f"Likely fix: create the matching invoicing sheet, same as the recent Bridge orphans.\n"
        f"`{dedupe_key}`"
    )
    delivered, delivery_error = send_telegram(message)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO automation_telegram_alert_log "
            "(severity, job_name, chat_id, message, delivered, delivery_error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("warning", JOB_NAME, os.getenv("ROUTE_RESOLVER_LOG_ID") or os.getenv("RISK_LOG_ID") or "",
             message, delivered, delivery_error),
        )
    conn.commit()
