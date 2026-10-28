"""Per-client self-test.  Run:

    python3 -m jobs.approval_router.route_resolver.selftest            # offline, no network
    python3 -m jobs.approval_router.route_resolver.selftest --live     # also checks the real Smartsheet
    python3 -m jobs.approval_router.route_resolver.selftest alistair   # one client only

Offline: every client file has the required contract, and each of its
SELFTEST_CASES resolves to the expected sheet id through the client's own
resolve(). Live (needs the server's .env): every expected sheet id is really in
that client's invoicing folder (or one of its sub-folders), and the folder/sheet
name match for plain clients is checked against Smartsheet itself.
Exit code 0 = all passed. The output names the client file that failed.
"""

import sys

from .clients import CLIENTS
from .util import norm, FolderSheetCache

REQUIRED = ("CLIENT_SLUG", "MAIN_SHEET_ID", "INVOICING_FOLDER_ID", "log", "SELFTEST_CASES", "resolve", "process_row")


class _FakeCache:
    """Folder cache backed by the client's own snapshot cases (offline)."""
    def __init__(self, folder_id, cases):
        self._m = {folder_id: {norm(route): sid for route, _l, _c, sid in cases}}

    def sheets_in_folder(self, folder_id):
        return self._m.get(folder_id, {})


def check_client(slug, module, folder_cache=None):
    problems = []
    for attr in REQUIRED:
        if not hasattr(module, attr):
            problems.append(f"missing {attr}")
    if problems:
        return problems
    if module.CLIENT_SLUG != slug:
        problems.append(f"CLIENT_SLUG {module.CLIENT_SLUG!r} != registry key {slug!r}")
    cache = folder_cache or _FakeCache(module.INVOICING_FOLDER_ID, module.SELFTEST_CASES)
    for route, local, commodity, expected in module.SELFTEST_CASES:
        got, reason = module.resolve(route, local, commodity, cache)
        if got != expected:
            problems.append(f"{route!r} (local={local!r}, commodity={commodity!r}) -> {got} ({reason}); expected {expected}")
    return problems


def _live_ids_in_folder(ss, folder_id, depth=0):
    folder = ss.Folders.get_folder(folder_id)
    ids = {s.id for s in (folder.sheets or [])}
    if depth < 4:
        for sub in (folder.folders or []):
            ids |= _live_ids_in_folder(ss, sub.id, depth + 1)
    return ids


def main(argv):
    live = "--live" in argv
    only = [a for a in argv if not a.startswith("--")]
    ss = None
    if live:
        from dotenv import load_dotenv
        load_dotenv()
        from jobs.approval_router.mover import get_smartsheet_client
        ss = get_smartsheet_client()
    failed = 0
    for slug, module in CLIENTS.items():
        if only and slug not in only:
            continue
        cache = FolderSheetCache(ss) if live else None
        problems = check_client(slug, module, cache)
        if live and not problems:
            live_ids = _live_ids_in_folder(ss, module.INVOICING_FOLDER_ID)
            for _r, _l, _c, expected in module.SELFTEST_CASES:
                if expected not in live_ids:
                    problems.append(f"sheet id {expected} is not in invoicing folder {module.INVOICING_FOLDER_ID}")
        status = "OK  " if not problems else "FAIL"
        print(f"{status} {slug:24s} ({module.__name__.rsplit('.', 1)[-1]}.py, {len(module.SELFTEST_CASES)} cases)")
        for p in problems:
            print(f"       - {p}")
        failed += bool(problems)
    print("ALL PASSED" if not failed else f"{failed} CLIENT(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
