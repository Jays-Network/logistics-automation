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

Job names are the same dotted module paths job_registry.py discovers
(e.g. "jobs.approval_router.archive_router") -- confirmed during
health.py testing as the one correct, collision-free convention.
"""

import os
import subprocess

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Header, Depends

from jobs.approval_router.backup import get_connection
from orchestrator.job_registry import discover_jobs
from orchestrator.health import compute_health, get_all_health, JobHealth

REPO_ROOT = "/opt/jaysnet/logistics-automation"
API_KEY = os.getenv("ORCHESTRATOR_API_KEY")

app = FastAPI(title="Jay's Network Orchestrator")


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