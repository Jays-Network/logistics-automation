"""
api_gateway.py — Orchestrator (Phase 5)

FastAPI backend for the orchestrator dashboard. Reachable only via
Tailscale (never public internet, per Jay 2026-09-15) with a simple
shared API-key header -- Tailscale itself is the real access control
here, so heavier auth (OAuth etc.) on top would be redundant for this
threat model.

Endpoints:
  GET  /jobs                  -- every discovered job + its current health
  GET  /jobs/{job_name}       -- one job's health + its 20 most recent runs
  POST /jobs/{job_name}/run   -- trigger a job now (fire-and-forget,
                                  trigger_source='api'), dashboard polls
                                  GET /jobs/{job_name} afterward to watch
                                  it progress live via run_wrapper.py's
                                  periodic DB updates
  GET  /smartsheet-status     -- our own LIVE reachability check + Smartsheet's
                                  own public self-reported status (status.smartsheet.com)
  GET  /service-status        -- same live-reachability pattern for Telegram
                                  (Bot API getMe, using the same bot token every
                                  other job already uses) and GreenAPI (generic
                                  host reachability -- this IS the WhatsApp
                                  integration point in this system, there's no
                                  separate "WhatsApp server" to check)
  GET  /system-status          -- this LXC's own health: network I/O rate,
                                  Postgres connection count/DB size, and
                                  Grafana reachability (all three run on this
                                  same box, per Jay 2026-09-15)

Job names are the same dotted module paths job_registry.py discovers
(e.g. "jobs.approval_router.archive_router") -- confirmed during
health.py testing as the one correct, collision-free convention.
"""

import os
import subprocess
import time

import psutil
from collections import deque
import requests
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Header, Depends

from jobs.approval_router.backup import get_connection
from jobs.approval_router.mover import get_smartsheet_client
from orchestrator.job_registry import discover_jobs
from orchestrator.health import compute_health, get_all_health, JobHealth

REPO_ROOT = "/opt/jaysnet/logistics-automation"
API_KEY = os.getenv("ORCHESTRATOR_API_KEY")

app = FastAPI(title="OmniOrchestrator")


def require_api_key(x_api_key: str | None = Header(default=None)):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="ORCHESTRATOR_API_KEY not configured on server")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _health_to_dict(h: JobHealth) -> dict:
    return {
        "job_name": h.job_name,
        "description": h.description,
        "tags": h.tags,
        "cron_schedule": h.cron_schedule,
        "status": h.status,
        "last_started_at": h.last_started_at.isoformat() if h.last_started_at else None,
        "last_finished_at": h.last_finished_at.isoformat() if h.last_finished_at else None,
        "last_exit_code": h.last_exit_code,
        "last_trigger_source": h.last_trigger_source,
        "next_expected_at": h.next_expected_at.isoformat() if h.next_expected_at else None,
        "message": h.message,
    }


def _find_job(job_name: str):
    for job in discover_jobs():
        if job.job_name == job_name:
            return job
    return None


@app.get("/jobs", dependencies=[Depends(require_api_key)])
def list_jobs():
    return [_health_to_dict(h) for h in get_all_health()]


@app.get("/jobs/{job_name:path}", dependencies=[Depends(require_api_key)])
def get_job(job_name: str):
    job = _find_job(job_name)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")

    conn = get_connection()
    try:
        health = compute_health(conn, job)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, trigger_source, started_at, finished_at, exit_code, status, "
                "stdout_tail, stderr_tail, error_message "
                "FROM orchestrator_job_run_log WHERE job_name = %s "
                "ORDER BY started_at DESC LIMIT 20",
                (job_name,),
            )
            columns = [desc[0] for desc in cur.description]
            history = [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    for row in history:
        for key in ("started_at", "finished_at"):
            if row.get(key):
                row[key] = row[key].isoformat()

    return {**_health_to_dict(health), "recent_runs": history}


@app.post("/jobs/{job_name:path}/run", dependencies=[Depends(require_api_key)])
def trigger_job(job_name: str):
    job = _find_job(job_name)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_name}")

    command = [
        "python3", "-m", "orchestrator.run_wrapper",
        "--job-name", job_name,
        "--trigger", "api",
        "--",
        "python3", "-m", job_name,
    ]
    # Fire-and-forget: don't block the API response on the job actually
    # finishing (some take 60+ seconds) -- the dashboard polls
    # GET /jobs/{job_name} to watch it progress live via run_wrapper.py's
    # periodic DB updates.
    subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"status": "triggered", "job_name": job_name}


# A cheap, real API call to prove OUR connection/credentials work right
# now -- not just that we have a client object. Users.get_current_user()
# is about as lightweight as a genuinely authenticated round-trip gets.
def _check_smartsheet_live() -> dict:
    start = time.monotonic()
    try:
        ss_client = get_smartsheet_client()
        ss_client.Users.get_current_user()
        latency_ms = round((time.monotonic() - start) * 1000)
        return {"reachable": True, "latency_ms": latency_ms, "error": None}
    except Exception as exc:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {"reachable": False, "latency_ms": latency_ms, "error": str(exc)}


# Smartsheet's own public status page -- real, documented, no auth
# needed: https://status.smartsheet.com/api
def _check_smartsheet_public_status() -> dict:
    try:
        response = requests.get("https://status.smartsheet.com/api/v2/status.json", timeout=5)
        response.raise_for_status()
        data = response.json()
        return {
            "indicator": data["status"]["indicator"],  # none | minor | major | critical
            "description": data["status"]["description"],
        }
    except Exception as exc:
        return {"indicator": "unknown", "description": f"Could not reach status.smartsheet.com: {exc}"}


@app.get("/smartsheet-status", dependencies=[Depends(require_api_key)])
def smartsheet_status():
    return {
        "live": _check_smartsheet_live(),
        "public_status": _check_smartsheet_public_status(),
    }


# --- Telegram: real check via the Bot API's getMe, using the same bot
# token every alerting job in this codebase already uses. ---
def _check_telegram_live() -> dict:
    token = os.getenv("LOG_BOT_TOKEN")
    if not token:
        return {"reachable": False, "latency_ms": 0, "error": "LOG_BOT_TOKEN not set"}
    start = time.monotonic()
    try:
        response = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        latency_ms = round((time.monotonic() - start) * 1000)
        if response.ok and response.json().get("ok"):
            return {"reachable": True, "latency_ms": latency_ms, "error": None}
        return {"reachable": False, "latency_ms": latency_ms, "error": f"HTTP {response.status_code}"}
    except Exception as exc:
        return {"reachable": False, "latency_ms": round((time.monotonic() - start) * 1000), "error": str(exc)}


# --- GreenAPI (the actual WhatsApp integration point in this system --
# there's no separate thing called "WhatsApp server" to check).
# getStateInstance is GreenAPI's real, documented per-instance status
# endpoint -- confirms actual WhatsApp connection state, not just that
# the host responds. Credentials read from env, never hardcoded. ---
def _check_greenapi_live() -> dict:
    url = os.getenv("GREENAPI_URL")
    id_instance = os.getenv("GREENAPI_ID_INSTANCE")
    api_token = os.getenv("GREENAPI_API_TOKEN")
    if not all([url, id_instance, api_token]):
        return {"reachable": False, "latency_ms": 0, "error": "GREENAPI_URL/ID_INSTANCE/API_TOKEN not set", "state": None}
    start = time.monotonic()
    try:
        response = requests.get(
            f"{url}/waInstance{id_instance}/getStateInstance/{api_token}", timeout=5
        )
        latency_ms = round((time.monotonic() - start) * 1000)
        if response.ok:
            state = response.json().get("stateInstance")
            return {"reachable": True, "latency_ms": latency_ms, "error": None, "state": state}
        return {"reachable": False, "latency_ms": latency_ms, "error": f"HTTP {response.status_code}", "state": None}
    except Exception as exc:
        return {"reachable": False, "latency_ms": round((time.monotonic() - start) * 1000), "error": str(exc), "state": None}


@app.get("/service-status", dependencies=[Depends(require_api_key)])
def service_status():
    return {
        "telegram": _check_telegram_live(),
        "greenapi": _check_greenapi_live(),
    }


# --- This LXC's own health: network I/O rate, Postgres, Grafana. All
# three run on the same box as this API, per Jay 2026-09-15. ---

# Network I/O rate needs two samples over time -- psutil's counters are
# cumulative since boot. Kept as simple module-level state between polls
# (the dashboard polls this every few seconds anyway) rather than
# over-engineering a proper time-series store for a live gauge.
#
# History buffer (2026-09-16, per Jay's reference mockup wanting a real
# traffic chart): a rolling in-memory deque of recent samples, so the
# frontend can render an actual trend line instead of a single instant
# reading. Resets on API restart -- an honest, acceptable limitation for
# a live-monitoring tool, not a historical analytics platform.
_last_net_sample: dict | None = None
_network_history: deque = deque(maxlen=60)  # ~15 min at the dashboard's 15s poll interval


def _get_network_io() -> dict:
    global _last_net_sample
    counters = psutil.net_io_counters()
    now = time.monotonic()
    sent_rate = 0
    recv_rate = 0
    if _last_net_sample is not None:
        elapsed = now - _last_net_sample["t"]
        if elapsed > 0:
            sent_rate = round((counters.bytes_sent - _last_net_sample["sent"]) / elapsed)
            recv_rate = round((counters.bytes_recv - _last_net_sample["recv"]) / elapsed)
    _last_net_sample = {"t": now, "sent": counters.bytes_sent, "recv": counters.bytes_recv}
    _network_history.append({
        "timestamp": time.time(),
        "bytes_sent_per_sec": sent_rate,
        "bytes_recv_per_sec": recv_rate,
    })
    return {
        "bytes_sent_per_sec": sent_rate,
        "bytes_recv_per_sec": recv_rate,
        "history": list(_network_history),
    }


# Same rolling-history pattern as network I/O -- real recent samples for
# a trend chart, not a fabricated line. Resets on API restart.
_postgres_history: deque = deque(maxlen=60)
_grafana_history: deque = deque(maxlen=60)


def _get_postgres_stats(conn) -> dict:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity")
            connections = cur.fetchone()[0]
            cur.execute("SELECT pg_database_size(current_database())")
            db_size_bytes = cur.fetchone()[0]
        _postgres_history.append({"timestamp": time.time(), "connections": connections})
        return {
            "reachable": True, "connections": connections, "db_size_bytes": db_size_bytes,
            "error": None, "history": list(_postgres_history),
        }
    except Exception as exc:
        return {
            "reachable": False, "connections": None, "db_size_bytes": None,
            "error": str(exc), "history": list(_postgres_history),
        }


def _check_grafana_live() -> dict:
    start = time.monotonic()
    try:
        response = requests.get("http://localhost:3000/api/health", timeout=5)
        latency_ms = round((time.monotonic() - start) * 1000)
        _grafana_history.append({"timestamp": time.time(), "latency_ms": latency_ms})
        return {
            "reachable": response.ok, "latency_ms": latency_ms,
            "error": None if response.ok else f"HTTP {response.status_code}",
            "history": list(_grafana_history),
        }
    except Exception as exc:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {
            "reachable": False, "latency_ms": latency_ms, "error": str(exc),
            "history": list(_grafana_history),
        }


@app.get("/system-status", dependencies=[Depends(require_api_key)])
def system_status():
    conn = get_connection()
    try:
        postgres = _get_postgres_stats(conn)
    finally:
        conn.close()
    return {
        "network": _get_network_io(),
        "postgres": postgres,
        "grafana": _check_grafana_live(),
    }


if __name__ == "__main__":
    import uvicorn
    # NOT YET DECIDED (2026-09-15): whether to bind specifically to the
    # box's Tailscale interface IP for defense in depth, or bind 0.0.0.0
    # and rely on the box having no public-facing route to this port
    # plus Tailscale ACLs. Defaulting to 0.0.0.0 for now -- confirm and
    # tighten before treating this as a finished deployment.
    host = os.getenv("ORCHESTRATOR_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("ORCHESTRATOR_PORT", "8420"))
    uvicorn.run(app, host=host, port=port)