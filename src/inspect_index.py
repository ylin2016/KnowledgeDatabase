"""Dump the structure of Cohost_Property_PPTs_Locations.xlsx.

Read-only. Prints the columns and a sample of rows so the property -> listing
relationship can be mapped without guessing at column names.
"""

from __future__ import annotations

import sys

from discover import INDEX_SHEET, INDEX_XLSX
from xlsx import open_workbook, sheet_rows

SAMPLE = 18


def main() -> int:
    path = INDEX_XLSX
    print(f"file: {path}")
    if not path.exists():
        print("  !! not found — check discover.INDEX_XLSX")
        return 1

    wb = open_workbook(path)
    try:
        print(f"sheets: {wb.sheetnames}\n")
        if INDEX_SHEET not in wb.sheetnames:
            print(f"!! no '{INDEX_SHEET}' sheet")
            return 1
        rows = sheet_rows(wb[INDEX_SHEET])
    finally:
        wb.close()

    if not rows:
        print("!! sheet is empty")
        return 1

    header = [("" if c is None else str(c).strip()) for c in rows[0]]
    print(f"{len(rows) - 1} data rows, {len(header)} columns\n")
    print("columns:")
    for i, name in enumerate(header):
        filled = sum(
            1 for r in rows[1:] if i < len(r) and r[i] is not None and str(r[i]).strip()
        )
        print(f"  [{i:2}] {name[:38]:40} {filled} filled")

    # Distinct values of the small categorical columns, to confirm the filter.
    print("\ndistinct values in narrow columns:")
    for i, name in enumerate(header):
        vals = {
            str(r[i]).strip() for r in rows[1:]
            if i < len(r) and r[i] is not None and str(r[i]).strip()
        }
        if 0 < len(vals) <= 8:
            print(f"  {name[:30]:32} {sorted(vals)}")

    print(f"\nfirst {SAMPLE} rows:")
    for r in rows[1 : SAMPLE + 1]:
        cells = [
            f"{header[i][:16]}={str(v).strip()[:34]!r}"
            for i, v in enumerate(r)
            if i < len(header) and v is not None and str(v).strip()
        ]
        print("  " + " | ".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
