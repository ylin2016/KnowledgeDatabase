"""CLI: load onboarding data into Postgres, using the index as the structure.

    python src/import_onboarding.py --dry-run          # resolve + parse, no writes
    python src/import_onboarding.py                    # every Active property
    python src/import_onboarding.py --property Beachwood
    python src/import_onboarding.py --dir inputs/      # loose workbooks, no index
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import traceback
from pathlib import Path

import discover
from build import build
from db import connect, describe, get_dsn
from load_db import load
from parse_workbook import parse


def _entries_from_dir(directory: str) -> list[dict]:
    """Treat each loose workbook as its own property — used without the index."""
    out = []
    for p in sorted(Path(directory).glob("*.xlsx")):
        if p.name.startswith("~$"):
            continue
        out.append({
            "property": p.stem,
            "folder": None,
            "listings": [{"name": p.stem, "term": None, "file": p.name,
                          "folder": None, "path": p, "reason": None}],
            "_loose": True,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--property", action="append", help="import only these properties")
    ap.add_argument("--dir", help="import loose .xlsx files, ignoring the index")
    ap.add_argument("--dry-run", action="store_true", help="parse only; write nothing")
    ap.add_argument("--limit", type=int, help="stop after N properties")
    ap.add_argument(
        "--db", default="DATABASE_URL",
        help="which .env connection string to write to (default DATABASE_URL)",
    )
    args = ap.parse_args()

    entries = _entries_from_dir(args.dir) if args.dir else discover.plan()
    if args.property:
        wanted = {p.lower() for p in args.property}
        entries = [e for e in entries if e["property"].lower() in wanted]
    if args.limit:
        entries = entries[: args.limit]
    if not entries:
        print("nothing to import")
        return 1

    n_listings = sum(len(e["listings"]) for e in entries)
    # Which database is being written to is never implied — more than one Neon
    # project carries this schema.
    target = (
        " — DRY RUN, no writes" if args.dry_run
        else f" -> {describe(get_dsn(args.db))}"
    )
    print(f"{len(entries)} properties / {n_listings} listings{target}\n")

    ok, failed, all_notes = [], [], []
    cache: dict[Path, dict] = {}
    conn = None if args.dry_run else connect(args.db)
    try:
        for entry in entries:
            try:
                parsed, notes = build(entry, cache)
                filled = sum(1 for l in parsed["listings"] if l.get("_matched"))
                for l in parsed["listings"]:
                    l.pop("_matched", None)

                if args.dry_run:
                    stats = {
                        "nickname": parsed["property"]["nickname"],
                        "owners": len(parsed["owners"]),
                        "listings": len(parsed["listings"]),
                        "attrs": sum(len(l["attrs"]) for l in parsed["listings"]),
                        "secrets": sum(len(l["secrets"]) for l in parsed["listings"]),
                    }
                else:
                    paths = [l["path"] for l in entry["listings"] if l["path"]]
                    mtime = max(
                        (dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc)
                         for p in paths),
                        default=None,
                    )
                    stats = load(conn, parsed, source_modified_at=mtime)
                    conn.commit()

                gap = "" if filled == stats["listings"] else \
                      f"   <- {stats['listings'] - filled} listing(s) with no workbook data"
                print(
                    f"  ok  {stats['nickname'][:30]:32} "
                    f"owners={stats['owners']} listings={stats['listings']} "
                    f"attrs={stats['attrs']:4} secrets={stats['secrets']:3}{gap}"
                )
                ok.append(stats)
                all_notes += [(stats["nickname"], n) for n in notes]

            except Exception as e:
                if conn is not None:
                    conn.rollback()
                failed.append((entry["property"], e))
                print(f"  FAIL {entry['property']}: {type(e).__name__}: {e}")
                # Full traceback: a truncated one hides the line that raised,
                # which is the only part worth reading.
                traceback.print_exc()
    finally:
        if conn is not None:
            conn.close()

    print(f"\n{len(ok)} properties imported, {len(failed)} failed")
    if ok:
        print(
            f"totals: listings={sum(s['listings'] for s in ok)} "
            f"attrs={sum(s['attrs'] for s in ok)} "
            f"secrets={sum(s['secrets'] for s in ok)}"
        )
    if all_notes:
        print(f"\nnotes ({len(all_notes)}):")
        for prop, note in all_notes:
            print(f"  {prop}: {note}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
