"""Combine the index's structure with the workbooks' field values.

The index decides which listings exist; a workbook only fills them in. Every
Active index listing therefore produces a `listings` row, even when its workbook
is missing or has no column for it — an empty row is visible and countable,
whereas a dropped listing looks like it never existed.
"""

from __future__ import annotations

from pathlib import Path

from discover import _norm
from parse_workbook import parse as parse_workbook

# Property-level fields, taken from whichever workbook the property resolves to.
_PROPERTY_FIELDS = ("address", "property_type", "nwmls_id")


class WorkbookError(Exception):
    """A workbook failed to parse; the message names the file."""


def _match_column(index_name: str, columns: list[dict]) -> dict | None:
    """Find the workbook column belonging to an index listing name."""
    want = _norm(index_name)

    for col in columns:
        if _norm(col.get("nickname") or "") == want:
            return col
    # Nicknames drift ("Bellevue 14507U1" vs "14507 Unit 1"); accept containment
    # only when it is unambiguous.
    partial = [
        c for c in columns
        if (n := _norm(c.get("nickname") or "")) and (n in want or want in n)
    ]
    return partial[0] if len(partial) == 1 else None


def build(entry: dict, cache: dict[Path, dict] | None = None) -> tuple[dict, list[str]]:
    """One index property -> the dict shape load_db.load() expects, plus notes."""
    cache = {} if cache is None else cache
    notes: list[str] = []

    # Parse each distinct workbook once; several listings may share one.
    parsed_by_path: dict[Path, dict] = {}
    for listing in entry["listings"]:
        path = listing["path"]
        if path is None or path in parsed_by_path:
            continue
        if path not in cache:
            try:
                cache[path] = parse_workbook(path)
            except Exception as e:
                # Without the filename, a failure here names only the property,
                # which may map to several workbooks. `from e` keeps the original
                # traceback so the raising line still prints.
                raise WorkbookError(f"{path.name}: {type(e).__name__}: {e}") from e
        parsed_by_path[path] = cache[path]

    for path, parsed in parsed_by_path.items():
        for note in parsed.get("notes", []):
            notes.append(f"{path.name}: {note}")

    # Columns available per workbook, and how many index listings claim each.
    claims: dict[Path, int] = {}
    for listing in entry["listings"]:
        if listing["path"] is not None:
            claims[listing["path"]] = claims.get(listing["path"], 0) + 1

    out_listings = []
    for ordinal, listing in enumerate(entry["listings"]):
        path = listing["path"]
        parsed = parsed_by_path.get(path)
        columns = parsed["listings"] if parsed else []

        col = _match_column(listing["name"], columns) if columns else None
        if col is None and columns:
            # A workbook claimed by exactly one listing and holding exactly one
            # column is that listing, whatever the nickname says.
            if len(columns) == 1 and claims.get(path) == 1:
                col = columns[0]
            else:
                notes.append(
                    f"{listing['name']}: no matching column in {path.name} "
                    f"(has {[c.get('nickname') for c in columns]})"
                )

        row = dict(col) if col else {"attrs": [], "secrets": []}
        row["ordinal"] = ordinal
        # The index is authoritative for identity and term; the workbook is not.
        row["nickname"] = listing["name"]
        row["rental_type"] = listing["term"] or row.get("rental_type")
        row["_matched"] = col is not None
        out_listings.append(row)

        if listing["reason"]:
            notes.append(f"{listing['name']}: {listing['reason']}")

    _assign_roles(out_listings, notes)

    # Property-level attributes come from any workbook that resolved.
    first = next(iter(parsed_by_path.values()), None)
    prop = {
        "nickname": entry["property"],
        "source_file_name": ", ".join(sorted(p.name for p in parsed_by_path)) or None,
    }
    for f in _PROPERTY_FIELDS:
        prop[f] = (first or {}).get("property", {}).get(f)

    # Owners are per-workbook; dedupe across the property's workbooks.
    owners, seen = [], set()
    for parsed in parsed_by_path.values():
        for owner in parsed["owners"]:
            key = (_norm(owner["full_name"]), (owner.get("email") or "").lower())
            if key not in seen:
                seen.add(key)
                owners.append({**owner, "ordinal": len(owners) + 1})

    return {"property": prop, "owners": owners, "listings": out_listings}, notes


def _assign_roles(listings: list[dict], notes: list[str]) -> None:
    """Mark the whole-property listing, where one demonstrably exists.

    The index does not record a main/child distinction, so it is inferred: a
    listing whose bedrooms and sleeps equal the sum of the others' is the whole
    property. Many properties (Beachwood's ten separate units) have no such
    listing, and are left with none rather than promoting an arbitrary one.
    """
    for l in listings:
        l["role"] = "child"

    if len(listings) == 1:
        listings[0]["role"] = "main"
        return

    named = [i for i, l in enumerate(listings) if "whole" in (l.get("nickname") or "").lower()]
    if len(named) == 1:
        listings[named[0]]["role"] = "main"
        return

    def sums(i: int, key: str) -> bool:
        mine = listings[i].get(key)
        if mine is None:
            return False
        # Listings with no workbook data are skipped rather than poisoning the
        # sum; two populated sub-units are the minimum worth adding up.
        others = [
            l.get(key) for j, l in enumerate(listings)
            if j != i and l.get(key) is not None
        ]
        if len(others) < 2:
            return False
        return abs(float(mine) - sum(float(o) for o in others)) < 1e-6

    scores = {i: sum(sums(i, k) for k in ("bedrooms", "sleeps")) for i in range(len(listings))}
    best = max(scores.values())
    top = [i for i, s in scores.items() if s == best]
    if best >= 1 and len(top) == 1:
        listings[top[0]]["role"] = "main"
        if top[0] != 0:
            notes.append(
                f"whole-property listing is {listings[top[0]]['nickname']!r}, "
                f"not the first column"
            )
