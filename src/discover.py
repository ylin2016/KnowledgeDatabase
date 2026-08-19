"""Resolve what to import from the Cohost_Property_PPTs_Locations index.

The index — not the workbooks — defines the structure. Columns:

    Property.folder   directory under PROPERTIES_ROOT
    Listings          ONE ROW PER LISTING; this is the listing's name
    Property          the property the listing belongs to
    Term              STR / LTR, per listing
    Status            Active / Inactive
    File              the workbook filename for that listing

Two arrangements both occur and neither can be assumed:

    Beachwood       10 listings, each with its OWN workbook
    Bellevue 14507  3 listings (U1/U2/U3) sharing ONE workbook

So a property maps to a set of listings, and listings map to files many-to-one.
Workbooks supply field values only; they never decide which listings exist.
"""

from __future__ import annotations

import re
from pathlib import Path

from fields import _DASH_RE, clean_value, label_key
from xlsx import open_workbook, sheet_rows

DRIVE = Path("/Users/ylin/Google Drive/My Drive")
INDEX_XLSX = DRIVE / "Data and Reporting/10-Valta AI/OnboardingTemplate/PPT Info summary/Cohost_Property_PPTs_Locations.xlsx"
INDEX_SHEET = "PropertyFolder"
PROPERTIES_ROOT = DRIVE / "** Properties ** -- Valta"

_ONBOARDING_RE = re.compile(r"onboarding", re.IGNORECASE)
_EXCLUDE_RE = re.compile(r"(^~\$|^zzz_|do not use|template)", re.IGNORECASE)


# --------------------------------------------------------------- index rows


def _column_map(header: list) -> dict[str, int]:
    keys = [label_key(c) for c in header]

    def find(*names: str) -> int | None:
        for n in names:
            if n in keys:
                return keys.index(n)
        return None

    cols = {
        "folder": find("property.folder", "property folder", "propertyfolder"),
        "listing": find("listings", "listing"),
        "property": find("property"),
        "term": find("term"),
        "status": find("status"),
        "file": find("file"),
    }
    if cols["folder"] is None or cols["listing"] is None:
        raise KeyError(f"index is missing Property.folder / Listings columns: {header}")
    return cols


def active_rows(index_path: Path = INDEX_XLSX) -> list[dict]:
    """Every Active row, one per listing."""
    wb = open_workbook(index_path)
    try:
        if INDEX_SHEET not in wb.sheetnames:
            raise KeyError(f"{index_path.name} has no '{INDEX_SHEET}' sheet: {wb.sheetnames}")
        rows = sheet_rows(wb[INDEX_SHEET])
    finally:
        wb.close()

    if not rows:
        return []
    cols = _column_map(rows[0])

    out = []
    for row in rows[1:]:
        def cell(key):
            i = cols[key]
            return clean_value(row[i]) if i is not None and i < len(row) else None

        if (cell("status") or "").lower() != "active":
            continue
        listing = cell("listing")
        if not listing:
            continue

        # The folder cell is kept verbatim. A "/" in it is genuinely ambiguous —
        # see descend() — so it is resolved against the filesystem, not parsed.
        out.append({
            "folder": cell("folder"),
            "listing": listing,
            # Rows occasionally omit Property; the listing then stands alone.
            "property": cell("property") or listing,
            "term": cell("term"),
            "file": cell("file"),
        })
    return out


# ------------------------------------------------------- filesystem lookup


def _norm(name: str) -> str:
    """Fold the drift between hand-typed index text and real names.

    ":" folds to "/" because a Drive folder named `Unit E1 - live 7/17/26` is
    stored on macOS as `Unit E1 - live 7:17:26` — POSIX forbids "/" in a name,
    so Finder and Drive show ":" as "/".
    """
    s = str(name).replace("\xa0", " ").replace(":", "/")
    s = _DASH_RE.sub("-", s)
    s = re.sub(r"[\s_]+", " ", s)
    return s.strip(" -,.").lower()


def _dir_index(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {_norm(d.name): d for d in root.iterdir() if d.is_dir()}


def resolve_folder(
    folder_name: str | None,
    root: Path = PROPERTIES_ROOT,
    fuzzy: bool = True,
) -> Path | None:
    """One directory directly under `root`, tolerating name drift.

    `fuzzy` enables prefix matching. descend() turns it off while testing
    multi-segment names, where a prefix match would swallow the segment that
    still needs resolving.
    """
    if not folder_name:
        return None
    for candidate in (folder_name, folder_name.replace("/", ":")):
        if (root / candidate).is_dir():
            return root / candidate

    index = _dir_index(root)
    want = _norm(folder_name)
    if want in index:
        return index[want]
    if not fuzzy:
        return None

    partial = [p for k, p in index.items() if k.startswith(want) or want.startswith(k)]
    return partial[0] if len(partial) == 1 else None


def descend(cell: str | None, root: Path = PROPERTIES_ROOT) -> tuple[Path | None, str | None]:
    """Walk a `Property.folder` cell down the tree; return (deepest dir, leftover).

    A "/" in the cell means one of three things, and only the filesystem can
    say which:

        ".../Cottage 1"           a real subdirectory holding that workbook
        ".../710 ADU"             no such directory — the workbook is in the parent
        ".../Unit E1 - live 7/17/26"  part of the directory's own name

    So each "/" is a candidate boundary, tried longest-run-first: a name that
    exists as written wins over any split of it. Prefix guessing is allowed only
    on a lone trailing segment, or the parent would match the whole cell and
    consume the subdirectory it was supposed to lead to.

    The leftover is the tail that matched nothing — `710 ADU`, which is not a
    directory at all — and it is the caller's cue to search the parent instead.
    """
    if not cell:
        return None, None
    parts = [p.strip() for p in str(cell).split("/") if p.strip()]

    node, i = root, 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            found = resolve_folder("/".join(parts[i:j]), node, fuzzy=(j == i + 1))
            if found is not None:
                node, i = found, j
                break
        else:
            return (node if node != root else None), "/".join(parts[i:])
    return node, None


def resolve_file(folder: Path, file_name: str | None) -> Path | None:
    """Locate one listing's workbook inside its property folder.

    The live sheet sits at the folder's top level; subfolders hold older copies,
    so those are only consulted if the top level has no match.
    """
    if folder is None:
        return None

    if file_name:
        exact = folder / file_name
        if exact.is_file():
            return exact
        want = _norm(file_name)
        for scope in (folder.glob("*"), folder.rglob("*")):
            for p in scope:
                if p.is_file() and _norm(p.name) == want:
                    return p
        # The index sometimes records the name without its extension.
        stem = _norm(Path(file_name).stem)
        for p in folder.glob("*.xlsx"):
            if _norm(p.stem) == stem:
                return p

    # No usable File cell: fall back to the single onboarding sheet, if there is
    # exactly one. More than one is ambiguous and is left for a human.
    hits = [
        p for p in folder.glob("*.xlsx")
        if _ONBOARDING_RE.search(p.name) and not _EXCLUDE_RE.search(p.name)
    ]
    return hits[0] if len(hits) == 1 else None


def _upward(folder: Path | None, root: Path) -> list[Path]:
    """`folder` and its ancestors, deepest first, stopping below `root`.

    Searching `root` itself would let any property's workbook answer for any
    other, so the walk ends at the property directory.
    """
    out = []
    node = folder
    while node is not None and node != root and node != node.parent:
        out.append(node)
        node = node.parent
    return out


# ------------------------------------------------------------------- plan


def plan(index_path: Path = INDEX_XLSX, root: Path = PROPERTIES_ROOT) -> list[dict]:
    """One entry per property, carrying every listing and its resolved workbook.

    Nothing is dropped: a listing whose workbook cannot be found still appears,
    with path=None and a reason, so it can be created as an empty row and
    counted rather than silently vanishing.
    """
    by_property: dict[str, dict] = {}

    for row in active_rows(index_path):
        entry = by_property.setdefault(
            row["property"],
            {"property": row["property"], "folder": row["folder"], "listings": []},
        )
        folder, leftover = descend(row["folder"], root)
        # Deepest first: the subdirectory scopes the search, keeping archived
        # copies in sibling folders out of reach. But a subdirectory existing
        # does not mean the workbook is in it — `Seattle 906 Lower/` is real and
        # empty, the sheet sits one level up — so each ancestor is tried in turn,
        # stopping short of PROPERTIES_ROOT.
        path = None
        for scope in _upward(folder, root):
            path = resolve_file(scope, row["file"])
            if path is not None:
                break

        if folder is None:
            reason = "folder not found"
        elif path is None:
            reason = "workbook not found in folder"
        else:
            reason = None

        entry["listings"].append({
            "name": row["listing"],
            "term": row["term"],
            "file": row["file"],
            "folder": row["folder"],
            "resolved": folder,
            "leftover": leftover,
            "path": path,
            "reason": reason,
        })

    return sorted(by_property.values(), key=lambda e: e["property"])


if __name__ == "__main__":
    import sys

    needle = sys.argv[1].lower() if len(sys.argv) > 1 else None
    entries = plan()
    if needle:
        entries = [
            e for e in entries
            if needle in e["property"].lower()
            or needle in (e["folder"] or "").lower()
            or any(needle in (l["name"] + " " + (l["file"] or "")).lower()
                   for l in e["listings"])
        ]
        print(f"filter: {needle!r}\n")
    n_listings = sum(len(e["listings"]) for e in entries)
    unresolved = [
        (e["property"], l) for e in entries for l in e["listings"] if l["reason"]
    ]
    files = {l["path"] for e in entries for l in e["listings"] if l["path"]}

    print(f"{len(entries)} properties, {n_listings} listings, {len(files)} distinct workbooks\n")
    for e in entries:
        shared = len({l["path"] for l in e["listings"] if l["path"]})
        note = "  [1 workbook shared]" if shared == 1 and len(e["listings"]) > 1 else ""
        print(f"  {e['property'][:30]:32} {len(e['listings'])} listings{note}")
        for l in e["listings"]:
            mark = l["path"].name if l["path"] else f"!! {l['reason']}"
            print(f"      {l['name'][:26]:28} {l['term'] or '-':4} {mark}")

    if unresolved:
        print(f"\nunresolved listings ({len(unresolved)}):")
        for prop, l in unresolved:
            print(f"  {prop} / {l['name']}: {l['reason']}")
            print(f"      folder={l['folder']!r} file={l['file']!r}")
