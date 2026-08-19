"""Parse one Property Onboarding workbook into plain dicts.

Workbook shape (see CLAUDE.md): three tabs, each laid out vertically with the
field label in column A and ONE COLUMN PER OBJECT to the right.

    Field                | Main listing      | Child listing 1   | ...
    Property Nickname    | Seattle 10057 W.. | Seattle 10057 L.. |
    Bedrooms             | 4                 | 2                 |

Tab names vary across template generations, so tabs are matched by substring
rather than exact name.
"""

from __future__ import annotations

from pathlib import Path

from xlsx import missing_strings, open_workbook, sheet_rows

from fields import (
    clean_value,
    is_sensitive,
    label_key,
    normalize_label,
    parse_int,
    parse_money,
    parse_number,
    split_label,
)

# Property Info labels promoted to real `listings` columns. Everything else
# falls through to listing_attrs / listing_secrets.
_LISTING_COLUMNS: dict[str, tuple[str, str]] = {
    "property nickname": ("nickname", "text"),
    "property address": ("address", "text"),
    "property type": ("property_type", "text"),
    "listing title": ("title", "text"),
    "rental type": ("rental_type", "text"),
    "account link": ("account_link", "text"),
    "3d link": ("listing_3d_link", "text"),
    "house manual link": ("house_manual_link", "text"),
    "square footage": ("square_footage", "number"),
    "bedrooms": ("bedrooms", "number"),
    "bathrooms": ("bathrooms", "number"),
    "sleeps (max guests)": ("sleeps", "int"),
    "maintenance - cleaning - cleaning fee": ("cleaning_fee", "money"),
    "maintenance - cleaning - cleaning expense to cleaner": ("cleaning_cost", "money"),
}

_OWNER_COLUMNS: dict[str, str] = {
    "full name": "full_name",
    "email address": "email",
    "phone number": "phone",
}


class ParseError(Exception):
    pass


def _find_sheet(wb, *needles: str):
    """First worksheet whose title contains any needle (case-insensitive)."""
    for ws in wb.worksheets:
        title = ws.title.strip().lower()
        if any(n in title for n in needles):
            return ws
    return None


def _rows(ws) -> list[list]:
    return sheet_rows(ws)


def _header_index(rows: list[list]) -> int | None:
    """Row index whose first cell is literally 'Field' — the column headers."""
    for i, row in enumerate(rows):
        if row and label_key(row[0]) == "field":
            return i
    return None


def _column_count(rows: list[list], start: int) -> int:
    """Widest populated column below the header.

    The header row under-reports: workbooks routinely hold a third owner or a
    fourth listing whose header cell was never filled in.
    """
    widest = 0
    for row in rows[start:]:
        for c in range(len(row) - 1, 0, -1):
            if clean_value(row[c]) is not None:
                widest = max(widest, c)
                break
    return widest


def _field_value_map(ws) -> dict[str, str]:
    """Flat Field/Value tab (the Summary sheet)."""
    out: dict[str, str] = {}
    for row in _rows(ws):
        if not row or len(row) < 2:
            continue
        key = label_key(row[0])
        val = clean_value(row[1])
        if key and key != "field" and val is not None and key not in out:
            out[key] = val
    return out


def _parse_owners(ws) -> list[dict]:
    rows = _rows(ws)
    head = _header_index(rows)
    if head is None:
        return []
    ncols = _column_count(rows, head + 1)

    owners: list[dict] = [
        {"ordinal": c, "full_name": None, "email": None, "phone": None,
         "comm_method": None, "comm_handle": None}
        for c in range(1, ncols + 1)
    ]

    seen_comm = False
    for row in rows[head + 1:]:
        if not row:
            continue
        key = label_key(row[0])
        if not key:
            continue

        # "Preferred Communication Method" appears twice: the method, then the
        # actual handle (wechat id, phone). Second occurrence is the handle.
        if key == "preferred communication method":
            target = "comm_handle" if seen_comm else "comm_method"
            seen_comm = True
        elif key in _OWNER_COLUMNS:
            target = _OWNER_COLUMNS[key]
        else:
            continue

        for c in range(1, ncols + 1):
            val = clean_value(row[c]) if c < len(row) else None
            if val is not None and owners[c - 1][target] is None:
                owners[c - 1][target] = val

    return [o for o in owners if o["full_name"]]


def _parse_listings(ws) -> list[dict]:
    rows = _rows(ws)
    head = _header_index(rows)
    if head is None:
        raise ParseError("Property Info tab has no 'Field' header row")
    ncols = _column_count(rows, head + 1)

    listings: list[dict] = [
        {
            "ordinal": c - 1,
            "role": "main" if c == 1 else "child",
            "attrs": [],     # (field, category, subcategory, label, value)
            "secrets": [],   # same shape, encrypted on write
            "nwmls_id": None,
        }
        for c in range(1, ncols + 1)
    ]

    for row in rows[head + 1:]:
        if not row:
            continue
        raw_label = row[0]
        key = label_key(raw_label)
        if not key or key == "field":
            continue

        field = normalize_label(raw_label)
        category, subcategory, label = split_label(raw_label)
        sensitive = is_sensitive(raw_label)
        promoted = _LISTING_COLUMNS.get(key)

        for c in range(1, ncols + 1):
            val = clean_value(row[c]) if c < len(row) else None
            if val is None:
                continue
            listing = listings[c - 1]

            if key == "nwmls id":
                listing["nwmls_id"] = val
                continue

            if promoted:
                col, kind = promoted
                listing[col] = (
                    parse_money(val) if kind == "money"
                    else parse_number(val) if kind == "number"
                    else parse_int(val) if kind == "int"
                    else val
                )
                # Promoted values are not duplicated into listing_attrs.
                continue

            bucket = "secrets" if sensitive else "attrs"
            listing[bucket].append((field, category, subcategory, label, val))

    # A template's unfilled "Child listing (if any)" column carries no data.
    return [
        l for l in listings
        if l.get("nickname") or l.get("title") or l["attrs"] or l["secrets"]
    ]


def parse(path: str | Path) -> dict:
    """Workbook -> {property, owners, listings}. Raises ParseError if unusable."""
    path = Path(path)
    wb = open_workbook(path)
    try:
        prop_ws = _find_sheet(wb, "property")
        if prop_ws is None:
            raise ParseError("no Property Info tab")

        listings = _parse_listings(prop_ws)
        if not listings:
            raise ParseError("Property Info tab has no populated listing column")

        owner_ws = _find_sheet(wb, "owner", "client")
        owners = _parse_owners(owner_ws) if owner_ws is not None else []

        # Summary is derived, but it is the only place the PROPERTY-level
        # nickname appears — listings carry unit-level names ("... Whole").
        summary_ws = _find_sheet(wb, "summary")
        summary = _field_value_map(summary_ws) if summary_ws is not None else {}

        # Column order only; build.py decides roles from the index.
        main = listings[0]
        nickname = (
            summary.get("property nickname")
            or main.get("nickname")
            or path.stem
        )

        notes = []
        lost = missing_strings(wb)
        if lost:
            notes.append(
                f"{lost} cell(s) read as blank — sharedStrings.xml is short of "
                f"what the sheets reference; those values are unrecoverable"
            )

        return {
            "property": {
                "nickname": nickname,
                "address": summary.get("property address") or main.get("address"),
                "property_type": summary.get("property type") or main.get("property_type"),
                "nwmls_id": main.get("nwmls_id"),
                "source_file_name": path.name,
            },
            "owners": owners,
            "listings": listings,
            "notes": notes,
        }
    finally:
        wb.close()


if __name__ == "__main__":
    import json
    import sys

    result = parse(sys.argv[1])
    for note in result["notes"]:
        print(f"!! {note}")
    print(json.dumps(result["property"], indent=2, ensure_ascii=False))
    print(f"\nowners: {len(result['owners'])}")
    for o in result["owners"]:
        print(f"  {o['ordinal']}. {o['full_name']}  {o['email']}  {o['phone']}")
    print(f"\nlistings: {len(result['listings'])}")
    for l in result["listings"]:
        print(
            f"  [{l['ordinal']}] {l['role']:5} {l.get('nickname')!r}"
            f"  bed={l.get('bedrooms')} bath={l.get('bathrooms')}"
            f" sleeps={l.get('sleeps')} fee={l.get('cleaning_fee')}"
            f"  attrs={len(l['attrs'])} secrets={len(l['secrets'])}"
        )
