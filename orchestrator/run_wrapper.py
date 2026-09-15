"""
run_wrapper.py — Orchestrator (Phase 5)

Wraps a job's actual invocation without touching the job's own code at
all -- confirmed against real industry practice (Cronitor et al.,
checked 2026-09-15): read the schedule, wrap the command, capture
start/end/exit-code/output, "without changes to the job itself."

Usage (both cron and manual/API invocation use the identical form):
    python3 -m orchestrator.run_wrapper --job-name route_resolver \
        --trigger cron -- /usr/bin/python3 -m jobs.approval_router.route_resolver

LIVE STREAMING (2026-09-15, per Jay: "most important is to be able to
monitor everything from the UI live"): the child's stdout/stderr are
read line-by-line as they're produced, immediately re-emitted to this
process's own stdout/stderr (so the existing shell redirection,
">> logs/X.log 2>&1", still captures everything exactly as before this
wrapper existed), AND pushed to the DB periodically WHILE the job is
still running -- not just once at the end. That's what lets the
dashboard show a job's real progress instead of a frozen screen until
it finishes. Two reader threads (stdout/stderr) avoid the classic
subprocess deadlock (blocking on one pipe while the other fills up).

If the wrapper itself is killed (SIGKILL, reboot) mid-run, the DB row is
left at status='running' forever -- the correct, intended signal for a
stuck/crashed job, same pattern already used elsewhere in this codebase
(e.g. approval_router_row_move_log's stuck-status entries).

CONFIRMED BUG FOUND LIVE 2026-09-15 AND FIXED: Python only line-buffers
stdout when it's a real terminal. The instant a child's stdout is
redirected into a pipe (exactly what subprocess.Popen does here), plain
print() calls silently switch to full buffering and sit invisible until
the buffer fills or the process exits -- while logging.StreamHandler-based
output keeps appearing live, since it flushes explicitly. Caught this by
testing mid-run: backup.py's Gatekeeper handshake print (the FIRST thing
the child does) didn't actually appear in our captured tail until the
process had already finished. Fixed by setting PYTHONUNBUFFERED=1 on the
child's environment -- respected by the interpreter regardless of how the
child is invoked, so this fixes legacy scripts too without touching their
code at all.
"""

import sys
import os
import subprocess
import argparse
import threading
import time
import logging

from dotenv import load_dotenv

load_dotenv()

from jobs.approval_router.backup import get_connection

logger = logging.getLogger("orchestrator.run_wrapper")

# Bounded tail length -- avoid unbounded growth in the DB. Plenty for
# watching progress and diagnosing a failure; not a full log archive --
# the actual log FILE (via the preserved shell redirection) remains the
# full record if more detail is ever needed.
MAX_OUTPUT_TAIL_CHARS = 5000

# How often the live tail gets pushed to the DB while the job is still
# running. A few seconds feels "live" in a UI without hammering the DB.
LIVE_UPDATE_INTERVAL_SECONDS = 2


def _start_run(conn, job_name: str, trigger_source: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orchestrator_job_run_log (job_name, trigger_source, status, started_at) "
            "VALUES (%s, %s, 'running', clock_timestamp()) RETURNING id",
            (job_name, trigger_source),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def _update_live_tail(conn, run_id: int, stdout_tail: str, stderr_tail: str):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE orchestrator_job_run_log SET stdout_tail = %s, stderr_tail = %s WHERE id = %s",
            (stdout_tail, stderr_tail, run_id),
        )
    conn.commit()


def _finish_run(conn, run_id: int, exit_code: int, stdout_tail: str, stderr_tail: str,
                 error_message: str | None = None):
    status = "success" if exit_code == 0 else "failed"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE orchestrator_job_run_log SET finished_at = clock_timestamp(), "
            "exit_code = %s, status = %s, stdout_tail = %s, stderr_tail = %s, "
            "error_message = %s WHERE id = %s",
            (exit_code, status, stdout_tail, stderr_tail, error_message, run_id),
        )
    conn.commit()


class _StreamCapture:
    """Reads a subprocess pipe line-by-line in a background thread,
    re-emitting immediately to a sink stream (so the outer shell
    redirection keeps working unchanged) while keeping a bounded,
    thread-safe tail buffer the main thread can read at any time --
    including while the child is still running."""

    def __init__(self, pipe, sink_stream):
        self._pipe = pipe
        self._sink_stream = sink_stream
        self._buffer = ""
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)

    def start(self):
        self._thread.start()

    def _read_loop(self):
        for line in iter(self._pipe.readline, ""):
            self._sink_stream.write(line)
            self._sink_stream.flush()
            with self._lock:
                self._buffer += line
                if len(self._buffer) > MAX_OUTPUT_TAIL_CHARS:
                    self._buffer = self._buffer[-MAX_OUTPUT_TAIL_CHARS:]
        self._pipe.close()

    def tail(self) -> str:
        with self._lock:
            return self._buffer

    def join(self):
        self._thread.join()


def main():
    parser = argparse.ArgumentParser(description="Wraps a job's execution for unified, live-trackable tracking.")
    parser.add_argument("--job-name", required=True, help="Human-readable name for this job in the dashboard.")
    parser.add_argument("--trigger", default="cron", choices=["cron", "manual", "api"],
                         help="How this run was triggered (default: cron).")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                         help="The real command to run, unmodified, after '--'.")
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("No command given to run (pass it after '--').", file=sys.stderr)
        sys.exit(2)

    conn = None
    conn_lock = threading.Lock()
    run_id = None
    try:
        conn = get_connection()
        with conn_lock:
            run_id = _start_run(conn, args.job_name, args.trigger)
    except Exception as exc:
        # If we can't even reach the DB, don't block the actual job from
        # running -- tracking is a nice-to-have, the job itself is not.
        logger.error("Could not start orchestrator tracking for %s: %s — running job untracked", args.job_name, exc)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    stdout_capture = _StreamCapture(process.stdout, sys.stdout)
    stderr_capture = _StreamCapture(process.stderr, sys.stderr)
    stdout_capture.start()
    stderr_capture.start()

    live_thread = None
    if conn and run_id:
        stop_live_updates = threading.Event()

        def _live_update_loop():
            # Shares the same connection as the main thread (down from a
            # separate connection each, per Jay 2026-09-15: no reason for
            # 2 separate Gatekeeper handshakes for one wrapper run) --
            # conn_lock ensures the two threads never actually execute a
            # query on it at the same time, which is the real requirement
            # for sharing a psycopg2 connection across threads, not
            # needing a second connection entirely.
            while not stop_live_updates.wait(LIVE_UPDATE_INTERVAL_SECONDS):
                try:
                    with conn_lock:
                        _update_live_tail(conn, run_id, stdout_capture.tail(), stderr_capture.tail())
                except Exception as exc:
                    logger.error("Live tail update failed for %s: %s", args.job_name, exc)

        live_thread = threading.Thread(target=_live_update_loop, daemon=True)
        live_thread.start()

    exit_code = process.wait()
    stdout_capture.join()
    stderr_capture.join()

    if conn and run_id:
        if live_thread:
            # Stop and properly wait for the live-update thread to fully
            # exit its loop before the final write -- previously this
            # wasn't joined, a benign race (separate connections meant no
            # corruption either way) but worth doing correctly now that
            # both threads share one connection.
            stop_live_updates.set()
            live_thread.join()
        try:
            with conn_lock:
                _finish_run(conn, run_id, exit_code, stdout_capture.tail(), stderr_capture.tail())
        except Exception as exc:
            logger.error("Could not record completion for %s: %s", args.job_name, exc)
        finally:
            conn.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()