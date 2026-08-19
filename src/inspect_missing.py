"""List what is actually on disk for every listing whose workbook was not found.

`plan()` reports "workbook not found in folder" without saying what IS there, so
the index's File cell cannot be compared against reality. This prints both.
Read-only; writes nothing.
"""

from __future__ import annotations

import sys

import discover

MAX_FILES = 40


def main() -> int:
    entries = discover.plan()
    unresolved = [
        (e, l) for e in entries for l in e["listings"] if l["reason"]
    ]
    if not unresolved:
        print("every listing resolved to a workbook")
        return 0

    print(f"{len(unresolved)} unresolved listing(s)\n")

    # Group by property folder: the listings of one property share a directory,
    # and the whole directory only needs printing once.
    seen: set[str] = set()
    for entry, listing in unresolved:
        print(f"{entry['property']} / {listing['name']}")
        print(f"  reason : {listing['reason']}")
        print(f"  index  : folder={listing['folder']!r}")
        print(f"           file={listing['file']!r}")

        folder = listing.get("resolved")
        if folder is None:
            print(f"  disk   : no directory matches {listing['folder']!r}\n")
            continue
        print(f"  disk   : {folder}")
        if listing.get("leftover"):
            print(f"           (index also named {listing['leftover']!r}; no such subfolder)")

        if str(folder) in seen:
            print("           (contents listed above)\n")
            continue
        seen.add(str(folder))

        found = sorted(
            p for p in folder.rglob("*.xlsx") if not p.name.startswith("~$")
        )
        if not found:
            print("           no .xlsx anywhere under this folder")
        for p in found[:MAX_FILES]:
            print(f"           {p.relative_to(folder)}")
        if len(found) > MAX_FILES:
            print(f"           ... and {len(found) - MAX_FILES} more")

        # The named file may exist under a drifted name. Listing every
        # onboarding-ish sheet at the property level separates "the sheet was
        # never made" from "the File cell does not match what it is called".
        scopes = discover._upward(folder, discover.PROPERTIES_ROOT)
        top = scopes[-1] if scopes else folder
        if top != folder:
            sheets = sorted(
                p for p in top.rglob("*.xlsx")
                if discover._ONBOARDING_RE.search(p.name)
                and not discover._EXCLUDE_RE.search(p.name)
            )
            print(f"  onboarding sheets under {top.name}:")
            for p in sheets[:MAX_FILES] or ["(none)"]:
                print(f"           {p.relative_to(top) if sheets else p}")
            if len(sheets) > MAX_FILES:
                print(f"           ... and {len(sheets) - MAX_FILES} more")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
