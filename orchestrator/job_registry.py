"""
job_registry.py — Orchestrator (Phase 5)

Auto-discovers every job by scanning the filesystem and the real
crontab -- deliberately NOT a hand-maintained list. Per Jay (2026-09-14):
a manually-maintained registry means every new script is a "go edit 3
files" chore nobody keeps up with. This module treats the filesystem and
crontab as the only source of truth and just reads them.

Discovery sources:
  1. Every .py file under jobs/ (recursively -- includes jobs/legacy/ and
     jobs/legacy/dormant/), matching the existing convention that every
     job already lives there.
  2. `crontab -l` parsed for lines invoking any discovered script --
     gives us its real schedule automatically. A script with no matching
     cron line is correctly reported as "not scheduled / manual" because
     nobody had to remember to mark it that way -- it's just genuinely
     absent from the crontab.
  3. Each script's own module docstring (first line) as its description,
     if it has one -- free documentation, not duplicated anywhere else.

Adding a new job tomorrow means writing the script and (optionally)
adding a crontab line -- exactly what you'd do anyway. Nothing here
needs updating.
"""

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

JOBS_ROOT = Path("/opt/jaysnet/logistics-automation/jobs")
REPO_ROOT = Path("/opt/jaysnet/logistics-automation")


@dataclass
class DiscoveredJob:
    job_name: str            # dotted module path, e.g. "jobs.approval_router.archive_router"
    file_path: str           # absolute path to the .py file
    description: str | None  # first line of its module docstring, if any
    cron_schedule: str | None = None   # raw cron schedule string, e.g. "*/10 * * * *", or None if not scheduled
    cron_line: str | None = None       # the full raw crontab line, for reference/debugging
    tags: list[str] = field(default_factory=list)  # e.g. ["legacy"], ["dormant"], [] for new jobs


def _module_path_from_file(py_file: Path) -> str:
    """Converts an absolute .py file path into the dotted module path
    used to invoke it via `python3 -m <path>`, relative to the repo root."""
    rel = py_file.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _first_docstring_line(py_file: Path) -> str | None:
    """Reads a script's module docstring without importing it (importing
    every discovered script just to read its docstring would be slow and
    -- for legacy/dormant scripts especially -- risky, since some of them
    execute top-level code on import). ast.parse + get_docstring is safe:
    it only reads the file's syntax tree, never executes anything."""
    try:
        source = py_file.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
        if docstring:
            return docstring.strip().splitlines()[0].strip()
    except (SyntaxError, UnicodeDecodeError, OSError):
        pass
    return None


def _tags_for(py_file: Path) -> list[str]:
    parts = py_file.relative_to(REPO_ROOT).parts
    tags = []
    if "legacy" in parts:
        tags.append("legacy")
    if "dormant" in parts:
        tags.append("dormant")
    return tags


def discover_job_files() -> list[Path]:
    """Every .py file under jobs/, recursively, excluding __init__.py and
    any __pycache__ contents."""
    return sorted(
        p for p in JOBS_ROOT.rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    )


# Matches a standard 5-field cron schedule at the start of a crontab
# line, e.g. "*/10 * * * *" or "0 6 * * *".
_CRON_SCHEDULE_RE = re.compile(
    r"^\s*(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$"
)


def parse_crontab() -> list[tuple[str, str]]:
    """Returns [(schedule, full_command), ...] for every real (non-comment,
    non-blank) line in the current user's crontab. Uses `crontab -l`
    directly rather than reading /var/spool/cron/... so this works
    identically regardless of how cron is set up on the box."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    entries = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _CRON_SCHEDULE_RE.match(stripped)
        if match:
            schedule, command = match.groups()
            entries.append((schedule, command))
    return entries


def discover_jobs() -> list[DiscoveredJob]:
    """The main entry point: scans jobs/ and cross-references crontab,
    returning a fresh list every time it's called -- deliberately not
    cached, since the whole point is that this always reflects the
    CURRENT state of the filesystem and crontab, not a snapshot that can
    drift stale."""
    cron_entries = parse_crontab()
    jobs = []

    for py_file in discover_job_files():
        job_name = _module_path_from_file(py_file)
        description = _first_docstring_line(py_file)
        tags = _tags_for(py_file)

        cron_schedule = None
        cron_line = None
        for schedule, command in cron_entries:
            # Matches if the module path appears anywhere in the cron
            # command -- covers "python3 -m jobs.X", a direct script
            # path, or a wrapped invocation via run_wrapper.
            if job_name in command or py_file.name in command:
                cron_schedule = schedule
                cron_line = command
                break

        jobs.append(DiscoveredJob(
            job_name=job_name,
            file_path=str(py_file),
            description=description,
            cron_schedule=cron_schedule,
            cron_line=cron_line,
            tags=tags,
        ))

    return jobs


if __name__ == "__main__":
    # Quick manual check: `python3 -m orchestrator.job_registry`
    for job in discover_jobs():
        schedule_display = job.cron_schedule or "not scheduled"
        tag_display = f" [{', '.join(job.tags)}]" if job.tags else ""
        print(f"{job.job_name}{tag_display} — {schedule_display}")
        if job.description:
            print(f"    {job.description}")