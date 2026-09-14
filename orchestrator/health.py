"""
health.py — Orchestrator (Phase 5)

Combines job_registry.py's discovered schedule with real execution data
from orchestrator_job_run_log to compute each job's health status.

Uses croniter (the standard Python library for cron-expression math) to
compute expected next-run times -- not a hand-rolled parser, since cron
syntax has enough real edge cases (day-of-week/month interactions,
step values, ranges) that it's not worth reinventing.

IMPORTANT CAVEAT until the crontab migration step (still pending): jobs'
real cron-triggered runs don't go through run_wrapper.py yet -- only
manual/test invocations do. Until crontab entries are updated to invoke
jobs THROUGH the wrapper, health.py will show scheduled jobs as
"overdue" shortly after any manual test, since it has no visibility into
the cron-triggered runs actually happening. This is expected and will
resolve itself once that migration step is done.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv()

from croniter import croniter

from jobs.approval_router.backup import get_connection
from orchestrator.job_registry import discover_jobs, DiscoveredJob

# How much slack before a scheduled job is considered overdue, as a
# multiple of its own interval -- e.g. a job that runs every 10 minutes
# gets a bit of buffer, since occasional slight lateness (a previous run
# still finishing, a brief rate limit) is normal and shouldn't trigger a
# false alarm.
GRACE_PERIOD_MULTIPLIER = 1.5
MIN_GRACE_PERIOD_MINUTES = 5  # a floor, so very-frequent jobs still get real slack

# A job left at status='running' for longer than this is treated as
# stuck/crashed (e.g. the wrapper process itself was killed, SIGKILL,
# reboot) regardless of its own schedule.
STUCK_THRESHOLD_MINUTES = 120


@dataclass
class JobHealth:
    job_name: str
    description: str | None
    tags: list[str]
    cron_schedule: str | None
    status: str  # "healthy" | "running" | "overdue" | "failed" | "stuck" | "never_run" | "not_scheduled"
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_exit_code: int | None
    last_trigger_source: str | None
    next_expected_at: datetime | None
    message: str


def _get_last_run(conn, job_name: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT started_at, finished_at, exit_code, status, trigger_source "
            "FROM orchestrator_job_run_log WHERE job_name = %s "
            "ORDER BY started_at DESC LIMIT 1",
            (job_name,),
        )
        return cur.fetchone()


def _grace_period_for(cron_schedule: str, now: datetime) -> timedelta:
    """Derives a reasonable grace period from the schedule itself: the
    gap between two consecutive expected runs, times a multiplier, with a
    minimum floor."""
    itr = croniter(cron_schedule, now)
    next_run = itr.get_next(datetime)
    following_run = croniter(cron_schedule, next_run).get_next(datetime)
    interval = following_run - next_run
    return max(interval * GRACE_PERIOD_MULTIPLIER, timedelta(minutes=MIN_GRACE_PERIOD_MINUTES))


def compute_health(conn, job: DiscoveredJob) -> JobHealth:
    now = datetime.now(timezone.utc)
    last_run = _get_last_run(conn, job.job_name)

    base_kwargs = dict(
        job_name=job.job_name, description=job.description, tags=job.tags,
        cron_schedule=job.cron_schedule,
    )

    if last_run is None:
        return JobHealth(
            **base_kwargs, status="never_run",
            last_started_at=None, last_finished_at=None, last_exit_code=None,
            last_trigger_source=None, next_expected_at=None,
            message="Never run through the orchestrator yet.",
        )

    started_at, finished_at, exit_code, run_status, trigger_source = last_run

    if run_status == "running":
        age = now - started_at
        if age > timedelta(minutes=STUCK_THRESHOLD_MINUTES):
            return JobHealth(
                **base_kwargs, status="stuck",
                last_started_at=started_at, last_finished_at=None, last_exit_code=None,
                last_trigger_source=trigger_source, next_expected_at=None,
                message=f"Still 'running' after {age}, likely crashed or killed.",
            )
        return JobHealth(
            **base_kwargs, status="running",
            last_started_at=started_at, last_finished_at=None, last_exit_code=None,
            last_trigger_source=trigger_source, next_expected_at=None,
            message=f"Currently running (started {age} ago).",
        )

    if run_status == "failed":
        return JobHealth(
            **base_kwargs, status="failed",
            last_started_at=started_at, last_finished_at=finished_at, last_exit_code=exit_code,
            last_trigger_source=trigger_source, next_expected_at=None,
            message=f"Last run failed (exit code {exit_code}).",
        )

    # run_status == "success" from here on
    if not job.cron_schedule:
        return JobHealth(
            **base_kwargs, status="not_scheduled",
            last_started_at=started_at, last_finished_at=finished_at, last_exit_code=exit_code,
            last_trigger_source=trigger_source, next_expected_at=None,
            message="Not on a schedule — manual/on-demand job.",
        )

    next_expected = croniter(job.cron_schedule, started_at).get_next(datetime)
    grace = _grace_period_for(job.cron_schedule, now)
    if now > next_expected + grace:
        return JobHealth(
            **base_kwargs, status="overdue",
            last_started_at=started_at, last_finished_at=finished_at, last_exit_code=exit_code,
            last_trigger_source=trigger_source, next_expected_at=next_expected,
            message=f"Expected another run by {next_expected.isoformat()}, hasn't happened yet.",
        )

    return JobHealth(
        **base_kwargs, status="healthy",
        last_started_at=started_at, last_finished_at=finished_at, last_exit_code=exit_code,
        last_trigger_source=trigger_source, next_expected_at=next_expected,
        message="Running on schedule.",
    )


def get_all_health() -> list[JobHealth]:
    conn = get_connection()
    try:
        return [compute_health(conn, job) for job in discover_jobs()]
    finally:
        conn.close()


if __name__ == "__main__":
    for health in get_all_health():
        tag_display = f" [{', '.join(health.tags)}]" if health.tags else ""
        print(f"{health.job_name}{tag_display} — {health.status.upper()}")
        print(f"    {health.message}")