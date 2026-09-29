"""What the index says now, against what the database holds.

The answer to "I have new listings" is not always "re-run the import": a listing
the index does not carry is invisible to the importer, however real its workbook
is. This names which of the two is behind.

Needs both Google Drive and the database, so it runs on the user's machine.

    ./kdb diff_plan.py [--db DATABASE_URL]
"""

from __future__ import annotations

import argparse
import sys

import discover
from db import connect, describe, get_dsn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="DATABASE_URL")
    args = ap.parse_args()

    entries = discover.plan()
    plan_listings = {
        (e["property"], l["name"]): l for e in entries for l in e["listings"]
    }
    print(f"index: {len(entries)} properties, {len(plan_listings)} Active listings")

    with connect(args.db) as conn, conn.cursor() as cur:
        cur.execute("""
            select p.nickname, l.nickname
            from listings l join properties p using (property_id)
        """)
        db_listings = {(p, l) for p, l in cur.fetchall()}
    print(f"db   : {describe(get_dsn(args.db))} — {len(db_listings)} listings\n")

    added = sorted(k for k in plan_listings if k not in db_listings)
    removed = sorted(k for k in db_listings if k not in plan_listings)

    if added:
        print(f"NEW — in the index, not yet in the database ({len(added)}):")
        for prop, name in added:
            l = plan_listings[(prop, name)]
            where = l["path"].name if l["path"] else f"!! {l['reason']}"
            print(f"  {prop:24} {name:26} {l['term'] or '-':4} {where}")
        # Importing the property, not the listing, is what fills these in.
        print(f"\n  ./kdb import_onboarding.py {' '.join(sorted({f'--property {p!r}' for p, _ in added}))}")
    else:
        print("NEW: none — the index has nothing the database lacks.")

    if removed:
        print(f"\nSTALE — in the database, no longer Active in the index ({len(removed)}):")
        for prop, name in removed:
            print(f"  {prop:24} {name}")
        print("\n  Re-import will NOT clear these; only reset_db.py + a full import will.")
    else:
        print("\nSTALE: none.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
