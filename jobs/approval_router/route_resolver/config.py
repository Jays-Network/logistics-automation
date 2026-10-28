"""Shared constants for route_resolver: client registry, job name, lock settings."""

INVOICING_WORKSPACE_ID = 1702905712535428

# client_slug -> (main tracking sheet_id, invoicing folder_id)
# Confirmed live 2026-09-13 against the real Smartsheet structure — see
# route-resolver-logic.md for the full audit these came from.
MAIN_SHEETS: dict[str, dict[str, int]] = {
    "alistair":               {"main_sheet_id": 8253984251793284, "invoicing_folder_id": 5153479745398660},
    "reload":                 {"main_sheet_id": 1307544392781700, "invoicing_folder_id": 2409098722469764},
    "glencore_international": {"main_sheet_id": 3849491978342276, "invoicing_folder_id": 7194173326550916},
    "fh_bertling":            {"main_sheet_id": 636041135345540,  "invoicing_folder_id": 2127623745759108},
    "bridge":                 {"main_sheet_id": 4700548271918980, "invoicing_folder_id": 1397548024915844},
    "goldvale":               {"main_sheet_id": 3232243102207876, "invoicing_folder_id": 4493772768733060},
    "gsm":                    {"main_sheet_id": 5438660737453956, "invoicing_folder_id": 5399770350020484},
    "ixm":                    {"main_sheet_id": 6551095343009668, "invoicing_folder_id": 5012742257043332},
    "mittal":                 {"main_sheet_id": 4646970969771908, "invoicing_folder_id": 8812654442637188},
    "sls_africa":             {"main_sheet_id": 3849847846162308, "invoicing_folder_id": 6807145233573764},
    "sls_trading":            {"main_sheet_id": 4242048029773700, "invoicing_folder_id": 6701592117307268},
    "zalawi":                 {"main_sheet_id": 3698247053823876, "invoicing_folder_id": 6490485884774276},
}

JOB_NAME = "route_resolver_part1"

LOCK_FILE = "/opt/jaysnet/logistics-automation/locks/route_resolver.lock"
# Zombie threshold is measured from the lock's LAST HEARTBEAT (touched on
# every row), not from when the run started. Previously a long Reload
# backlog run (>60 min) was treated as a zombie, its lock was cleared, and
# a second run started on top of it (2026-10-09: 190 duplicate-move 404s).
LOCK_STALE_SECONDS = 1800
