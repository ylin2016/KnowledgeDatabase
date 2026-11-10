"""Every onboarding workbook, field by field, against what the database holds.

diff_plan.py answers "which listings exist"; this answers "which VALUES are out
of date". Each Active property is built exactly as the import would build it,
then compared with the stored rows: property fields, owners, listing columns,
listing_attrs and listing_secrets. Nothing is written to the database.

Secrets are compared decrypted but only ever reported as "changed" — a door
code never reaches the report.

Needs both Google Drive and the database, so it runs on the user's machine:

    ./kdb diff_sheets.py > out/diff_sheets.txt
    ./kdb diff_sheets.py --db DATABASE_URL --property OSBR

Every difference is also written to out/diff_sheets_<date>_<time>.csv (gitignored —
it holds plaintext property data).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import re
import traceback
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import discover
from build import build
from db import ROOT, connect, describe, get_dsn
from secrets_io import get_key

_LISTING_COLS = (
    "role", "title", "rental_type", "address", "property_type", "account_link",
    "listing_3d_link", "house_manual_link", "square_footage", "bedrooms",
    "bathrooms", "sleeps", "cleaning_fee", "cleaning_cost",
)
_PROPERTY_COLS = ("address", "property_type", "nwmls_id", "source_file_name")
_OWNER_COLS = ("full_name", "email", "phone", "comm_method", "comm_handle")


def _v(x):
    """One spelling per value, so 4 / 4.0 / Decimal('4.00') compare equal."""
    if x is None or x == "":
        return None
    if isinstance(x, (int, float, Decimal)):
        d = Decimal(str(x)).normalize()
        return format(d, "f")
    return str(x)


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _kind(base: str, old, new) -> str:
    """'format' when only the spelling of a number moved ("1234.0" -> "1234").

    Not noise to ignore: the database copy is the wrong one, and a door code
    that reads 1234.0 is a wrong door code to whoever looks it up.
    """
    if old is not None and new is not None and _num(old) is not None and _num(old) == _num(new):
        return "format"
    return base


# Excel stored the phone as a number and it came back in scientific notation.
_SCI_RE = re.compile(r"^\d(\.\d+)?E\d+$", re.IGNORECASE)


def _short(x, n: int = 60) -> str:
    s = "∅" if x is None else str(x).replace("\n", " ⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _load_db(conn) -> dict:
    """Database contents keyed the way build() output is keyed."""
    key = get_key()
    db: dict = {"props": {}, "owners": defaultdict(list), "listings": {},
                "attrs": defaultdict(dict), "secrets": defaultdict(dict)}

    for row in conn.execute(
        f"select nickname, {', '.join(_PROPERTY_COLS)} from properties"
    ).fetchall():
        db["props"][row[0]] = dict(zip(_PROPERTY_COLS, row[1:]))

    for row in conn.execute(
        f"""select p.nickname, {', '.join('o.' + c for c in _OWNER_COLS)}
            from property_owners po
            join properties p using (property_id) join owners o using (owner_id)
            order by p.nickname, po.ordinal"""
    ).fetchall():
        db["owners"][row[0]].append(dict(zip(_OWNER_COLS, row[1:])))

    ids = {}
    for row in conn.execute(
        f"""select p.nickname, l.nickname, l.listing_id, {', '.join('l.' + c for c in _LISTING_COLS)}
            from listings l join properties p using (property_id)"""
    ).fetchall():
        k = (row[0], row[1])
        ids[row[2]] = k
        db["listings"][k] = dict(zip(_LISTING_COLS, row[3:]))

    for lid, field, value in conn.execute(
        "select listing_id, field, value from listing_attrs"
    ).fetchall():
        db["attrs"][ids[lid]][field] = value

    for lid, field, value in conn.execute(
        "select listing_id, field, pgp_sym_decrypt(value_enc, %s) from listing_secrets",
        (key,),
    ).fetchall():
        db["secrets"][ids[lid]][field] = value

    return db


def _diff_property(entry: dict, parsed: dict, db: dict) -> list[tuple]:
    """(kind, listing, field, old, new) for one property."""
    prop = parsed["property"]["nickname"]
    out: list[tuple] = []

    if prop not in db["props"]:
        out.append(("NEW PROPERTY", "", "", None, prop))
    else:
        for c in _PROPERTY_COLS:
            old, new = _v(db["props"][prop][c]), _v(parsed["property"][c])
            if old != new:
                out.append((_kind("property", old, new), "", c, old, new))

    # An owner is ONE row shared by every property they own, matched on
    # name+email. When two workbooks spell the same phone differently, whichever
    # imported last wins, so every other property shows a difference for ever.
    # Only identity changes are real; detail drift is reported as "owner shared".
    def ident(o):
        return (_v(o.get("full_name")), _v(o.get("email")))

    old_o = {ident(o): o for o in db["owners"].get(prop, [])}
    new_o = {ident(o): o for o in parsed["owners"]}
    for k in sorted(new_o.keys() - old_o.keys(), key=str):
        out.append(("owner added", "", "owner", None, " | ".join(x or "" for x in k)))
    for k in sorted(old_o.keys() - new_o.keys(), key=str):
        out.append(("owner removed", "", "owner", " | ".join(x or "" for x in k), None))
    for k in sorted(new_o.keys() & old_o.keys(), key=str):
        for c in _OWNER_COLS[2:]:
            old, new = _v(old_o[k].get(c)), _v(new_o[k].get(c))
            if c == "phone" and new and _SCI_RE.match(new):
                # Importing writes this into the shared row, breaking the phone
                # on every other property that owner has.
                out.append(("OWNER PHONE BAD", "", f"{k[0]} {c}", old, new))
            elif c == "phone" and old and new and old != new and (
                    _SCI_RE.match(old) or _kind("", old, new) == "format"):
                # The stored phone is the broken one (7.3E9 or 2489349770.0);
                # the sheet now reads correctly.
                out.append(("owner repair", "", f"{k[0]} {c}", old, new))
            elif old != new and new is not None:
                out.append(("owner shared", "", f"{k[0]} {c}", old, new))

    new_names = set()
    for l in parsed["listings"]:
        name = l["nickname"]
        new_names.add(name)
        k = (prop, name)
        old_attrs, old_secrets = db["attrs"].get(k, {}), db["secrets"].get(k, {})
        new_attrs = {a[0]: a[4] for a in l["attrs"]}
        new_secrets = {s[0]: s[4] for s in l["secrets"]}

        if k not in db["listings"]:
            out.append(("NEW LISTING", name, "", None,
                        f"{len(new_attrs)} attrs / {len(new_secrets)} secrets"))
            continue

        # The one change an import must never make quietly: a populated listing
        # rebuilt empty because its workbook stopped resolving (see CLAUDE.md,
        # Yacinde B1-B4).
        if not l.get("_matched") and (old_attrs or old_secrets):
            out.append(("WOULD BLANK", name, "", f"{len(old_attrs)} attrs / {len(old_secrets)} secrets",
                        "no workbook data"))
            continue

        for c in _LISTING_COLS:
            old, new = _v(db["listings"][k][c]), _v(l.get(c))
            if old != new:
                out.append((_kind("listing", old, new), name, c, old, new))

        for f in sorted(old_attrs.keys() | new_attrs.keys()):
            old, new = _v(old_attrs.get(f)), _v(new_attrs.get(f))
            if old != new:
                kind = "attr added" if old is None else "attr removed" if new is None else "attr changed"
                out.append((_kind(kind, old, new), name, f, old, new))

        for f in sorted(old_secrets.keys() | new_secrets.keys()):
            old, new = old_secrets.get(f), new_secrets.get(f)
            if _v(old) != _v(new):
                kind = _kind("secret added" if old is None else
                             "secret removed" if new is None else "secret changed", old, new)
                # Never the value — only that it moved.
                out.append((kind, name, f, None if old is None else "•••",
                            None if new is None else "•••"))

    for (p, name) in sorted(db["listings"]):
        if p == prop and name not in new_names:
            out.append(("LISTING GONE", name, "", "in database", "not in index/workbook"))

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", action="append",
                    help=".env key(s) to compare against (default: DATABASE_URL and WEATHERED_URL)")
    ap.add_argument("--property", action="append", help="only these properties")
    args = ap.parse_args()
    db_keys = args.db or ["DATABASE_URL", "WEATHERED_URL"]

    entries = discover.plan()
    if args.property:
        wanted = {p.lower() for p in args.property}
        entries = [e for e in entries if e["property"].lower() in wanted]

    # Parse every workbook once; the same build serves every database.
    built, failed, cache = [], [], {}
    for e in entries:
        try:
            parsed, notes = build(e, cache)
            built.append((e, parsed, notes))
        except Exception as ex:
            failed.append((e["property"], ex))
            print(f"PARSE FAIL {e['property']}: {type(ex).__name__}: {ex}")
            traceback.print_exc()

    today = dt.datetime.now().isoformat(timespec="minutes")
    # Timestamped: a rescan after an import must not overwrite the record of what it changed.
    csv_path = ROOT / "out" / f"diff_sheets_{today.replace(':', '')}.csv"
    rows_out = []
    by_db: dict[str, dict[str, list]] = {}

    for key in db_keys:
        with connect(key) as conn:
            db = _load_db(conn)
        changes: dict[str, list] = {}
        for e, parsed, _ in built:
            d = _diff_property(e, parsed, db)
            if d:
                changes[parsed["property"]["nickname"]] = d
                rows_out += [(today, key, parsed["property"]["nickname"], *c) for c in d]

        in_scope = {p["property"]["nickname"] for _, p, _ in built}
        if not args.property:
            for p in sorted(set(db["props"]) - in_scope):
                changes[p] = [("PROPERTY GONE", "", "", "in database", "not Active in index")]
                rows_out.append((today, key, p, *changes[p][0]))
        by_db[key] = changes

        print(f"\n{'=' * 78}\n{key} -> {describe(get_dsn(key))}\n"
              f"scanned {len(built)} properties, {len(failed)} parse failures; "
              f"{len(changes)} properties differ\n{'=' * 78}")
        for prop, d in sorted(changes.items()):
            counts = defaultdict(int)
            for c in d:
                counts[c[0]] += 1
            print(f"\n## {prop}  ({', '.join(f'{n} {k}' for k, n in sorted(counts.items()))})")
            for kind, listing, field, old, new in d:
                where = " / ".join(x for x in (listing, field) if x)
                print(f"  {kind:15} {where[:60]:60}  {_short(old, 40)}  ->  {_short(new, 40)}")

    # What to run. Blocked properties are held back: importing them destroys
    # data. Owner-shared drift alone is not worth an import — it would only move
    # the shared row's value to this workbook's spelling.
    blockers = {"WOULD BLANK": "a listing would be rebuilt empty — fix the folder/index first",
                "OWNER PHONE BAD": "owner phone reads as a number (e.g. 7.3E9) — format that cell as text first"}
    print(f"\n{'=' * 78}\nNEEDS UPDATE\n{'=' * 78}")
    for key, changes in by_db.items():
        blocked = {p: next(c[0] for c in d if c[0] in blockers)
                   for p, d in changes.items() if any(c[0] in blockers for c in d)}
        gone = sorted(p for p, d in changes.items() if any(c[0] == "PROPERTY GONE" for c in d))
        rest = [p for p in changes if p not in blocked and p not in gone]
        real = sorted(p for p in rest if any(c[0] not in ("format", "owner shared") for c in changes[p]))
        fmt = sorted(p for p in rest if p not in real and any(c[0] == "format" for c in changes[p]))
        shared = sorted(p for p in rest if p not in real and p not in fmt)
        todo = real + fmt
        print(f"\n{key}: {len(real)} with real edits + {len(fmt)} format-only to re-import; "
              f"{len(blocked)} blocked, {len(gone)} stale, {len(shared)} owner-drift only (skip)")
        print(f"  real edits : {', '.join(real) or '-'}")
        print(f"  format only: {', '.join(fmt) or '-'}")
        if todo:
            props = " ".join(f"--property {p!r}" for p in sorted(todo))
            print(f"  ./kdb import_onboarding.py --db {key} {props}")
        for p, why in sorted(blocked.items()):
            print(f"  BLOCKED {p}: {blockers[why]}")
        for p in gone:
            print(f"  STALE   {p}: no longer Active — re-import will not remove it")
        if shared:
            print(f"  skip (owner spelling drift only): {', '.join(shared)}")

    if len(by_db) == 2:
        a, b = by_db.values()
        if {p: len(d) for p, d in a.items()} != {p: len(d) for p, d in b.items()}:
            print("\nNOTE: the two databases are NOT in the same state — compare their sections.")

    csv_path.parent.mkdir(exist_ok=True)
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scanned", "db", "property", "kind", "listing", "field", "old", "new"])
        w.writerows(rows_out)
    print(f"\n{len(rows_out)} differences -> {csv_path.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
