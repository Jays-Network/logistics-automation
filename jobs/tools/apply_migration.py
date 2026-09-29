#!/usr/bin/env python3
"""
Apply ONE .sql migration file through the same Gatekeeper-authenticated
connection the jobs use (backup.get_connection). Use this only if you would
rather not run psql by hand -- if your normal process for applying
migrations (e.g. 017) is psql as an admin role, keep using that.

Runs the whole file in a single transaction: any error rolls everything back.
Needs a role that may CREATE TABLE and GRANT; if the Gatekeeper role can't,
you'll get a clear permission error and nothing is applied.

    python3 -m jobs.tools.apply_migration sql/migrations/018_contractor_sheet_state.sql
"""
import sys

from dotenv import load_dotenv
load_dotenv()

from jobs.approval_router.backup import get_connection


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print(f"Applied {path}")
        with conn.cursor() as cur:
            cur.execute("SELECT slug, region_code, active_sheet_id FROM contractor_sheet_state ORDER BY slug")
            for row in cur.fetchall():
                print("  ", row)
    except Exception as exc:
        conn.rollback()
        print(f"FAILED, rolled back: {exc}")
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())