"""File-based run lock with heartbeat (same convention as the other jobs).

Zombie detection is measured from the lock's LAST HEARTBEAT (touch_lock(),
called per row), not from when the run started -- see config.LOCK_STALE_SECONDS.
"""

import logging
import os
import time

from .config import LOCK_FILE, LOCK_STALE_SECONDS

logger = logging.getLogger("approval_router.route_resolver")

_LOCK_TOKEN = None


def touch_lock():
    try:
        os.utime(LOCK_FILE, None)
    except OSError:
        pass


def acquire() -> bool:
    """True if we now own the lock; False if another live run holds it."""
    global _LOCK_TOKEN
    if os.path.exists(LOCK_FILE):
        file_age = time.time() - os.path.getmtime(LOCK_FILE)
        if file_age > LOCK_STALE_SECONDS:
            logger.warning("Zombie lock detected (no heartbeat for %s min) — clearing it", int(file_age // 60))
            os.remove(LOCK_FILE)
        else:
            logger.info("route_resolver already running — aborting to avoid overlap")
            return False

    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    _LOCK_TOKEN = f"{os.getpid()}-{time.time()}"
    with open(LOCK_FILE, "w") as f:
        f.write(_LOCK_TOKEN)
    return True


def release():
    """Only remove the lock if it is still OURS -- never delete a lock that a
    newer run took over after clearing ours as stale."""
    try:
        with open(LOCK_FILE) as f:
            owner_ok = (f.read().strip() == _LOCK_TOKEN)
        if owner_ok:
            os.remove(LOCK_FILE)
    except OSError:
        pass
