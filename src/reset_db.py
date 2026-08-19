"""Delete every row, keeping the schema. Requires --yes.

Needed when the shape of an import changes: `load()` is idempotent per property
nickname, so rows whose nickname no longer occurs — `Cottage 1` before OSBR's
cottages became listings — survive every re-import and can only be cleared here.

    python src/reset_db.py --db DATABASE_URL --yes
"""

from __future__ import annotations

import argparse
import sys

from db import connect, describe, get_dsn

# Order matters only for readability; the cascades do the real work.
TABLES = ("listing_secrets", "listing_attrs", "listings", "property_owners",
          "properties", "owners")


def counts(cur) -> dict[str, int]:
    out = {}
    for t in TABLES:
        cur.execute(f"select count(*) from {t}")
        out[t] = cur.fetchone()[0]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="DATABASE_URL", help=".env key naming the target")
    ap.add_argument("--yes", action="store_true", help="required; without it nothing is deleted")
    args = ap.parse_args()

    print(f"target: {describe(get_dsn(args.db))}  ({args.db})")
    with connect(args.db) as conn, conn.cursor() as cur:
        before = counts(cur)
        for t, n in before.items():
            print(f"  {t:18} {n:6}")

        if not sum(before.values()):
            print("\nalready empty")
            return 0
        if not args.yes:
            print("\nnothing deleted — pass --yes to confirm")
            return 1

        # properties/owners cascade to the rest; the others are named so the
        # statement still reads as complete.
        cur.execute("truncate properties, owners restart identity cascade")
        conn.commit()

        after = counts(cur)
        print("\nafter:")
        for t, n in after.items():
            print(f"  {t:18} {n:6}")
        if sum(after.values()):
            print("!! not empty — something was not cascaded")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
