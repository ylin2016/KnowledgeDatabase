"""Rename the backup-code label in an onboarding workbook; move its row where possible.

Why raw XML for the rename: 43 of the 96 workbooks reference
xl/drawings/drawing1.xml which is absent from the archive, so openpyxl's full
reader (required for writing) raises KeyError and cannot open them at all.

The rename resolves the label through the CELL, not by text search. The shared
string table often holds duplicate entries of the same text (one sample has the
label at si#69 and si#182, with only #182 referenced by A45); editing by text
alone is ambiguous, and editing an orphan would change nothing while reporting
success. So: find the A-column cell whose text is the label, take the si index it
points at, and rewrite exactly that entry. If that si is shared with any OTHER
cell, refuse -- rewriting it would rename an unrelated cell too.
"""
from __future__ import annotations
import re, sys, zipfile
from pathlib import Path

sys.path.insert(0, "/Users/ylin/ValtaWork/Claude_projects/KnowledgeDatabase/src")
from fields import label_key  # noqa: E402

OLD_KEY = "maintenance - access - backup code"
NEW_KEY = "maintenance - access - guest access backup code"
PROPERTY_SHEET_HINT = "property"


def _sheet_targets(z: zipfile.ZipFile) -> dict[str, str]:
    """Sheet display name -> worksheet xml path."""
    wbx = z.read("xl/workbook.xml").decode("utf8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf8")
    rid_to_target = {
        m.group(1): m.group(2)
        for m in re.finditer(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels)
    }
    out = {}
    for m in re.finditer(r'<sheet[^>]*name="([^"]+)"[^>]*?r:id="([^"]+)"', wbx):
        name, rid = m.group(1), m.group(2)
        t = rid_to_target.get(rid, "")
        if t:
            out[name] = "xl/" + t.lstrip("/")
    if not out:  # some files order attributes differently
        for m in re.finditer(r"<sheet\b([^>]*)>", wbx):
            attrs = m.group(1)
            n = re.search(r'name="([^"]+)"', attrs)
            r = re.search(r'r:id="([^"]+)"', attrs)
            if n and r and r.group(1) in rid_to_target:
                out[n.group(1)] = "xl/" + rid_to_target[r.group(1)].lstrip("/")
    return out


def _si_texts(ss: str) -> list[tuple[int, int, int, str]]:
    """(index, text_start, text_end, text) for each <si>, using its first <t>."""
    out = []
    for i, m in enumerate(re.finditer(r"<si>.*?</si>", ss, re.S)):
        block = m.group(0)
        t = re.search(r"<t(?:\s[^>]*)?>([^<]*)</t>", block)
        if not t:
            out.append((i, -1, -1, ""))
            continue
        out.append((i, m.start() + t.start(1), m.start() + t.end(1), t.group(1)))
    return out


def rename(path: Path, *, dry_run: bool = False) -> tuple[bool, str]:
    z = zipfile.ZipFile(path)
    try:
        names = z.namelist()
        if "xl/sharedStrings.xml" not in names:
            return False, "no sharedStrings.xml"
        ss = z.read("xl/sharedStrings.xml").decode("utf8")
        sis = _si_texts(ss)

        sheets = _sheet_targets(z)
        prop = next((p for n, p in sheets.items() if PROPERTY_SHEET_HINT in n.lower()), None)
        if prop is None or prop not in names:
            return False, f"no Property sheet (sheets={list(sheets)})"
        sheet_xml = z.read(prop).decode("utf8")

        # A-column cells of type shared-string, mapped to their si index.
        acells = {
            m.group(1): int(m.group(2))
            for m in re.finditer(r'<c r="(A\d+)"[^>]*t="s"[^>]*><v>(\d+)</v></c>', sheet_xml)
        }
        hits = [(ref, si) for ref, si in acells.items()
                if si < len(sis) and label_key(sis[si][3]) == OLD_KEY]
        if not hits:
            if any(label_key(t) == NEW_KEY for *_ , t in sis):
                return False, "already renamed"
            return False, "label not found in any A-column cell"
        if len(hits) > 1:
            return False, f"label in multiple cells: {hits}"

        ref, si = hits[0]
        # Refuse if this si is referenced anywhere else (any sheet, any column).
        other = []
        for n in names:
            if not n.startswith("xl/worksheets/sheet"):
                continue
            xml = z.read(n).decode("utf8")
            for m in re.finditer(r'<c r="([A-Z]+\d+)"[^>]*t="s"[^>]*><v>(\d+)</v></c>', xml):
                if int(m.group(2)) == si and not (n == prop and m.group(1) == ref):
                    other.append(f"{n}:{m.group(1)}")
        if other:
            return False, f"si#{si} shared with {other[:4]}"

        _, s0, s1, old_text = sis[si]
        new_text = old_text.replace("Backup Code", "Guest Access Backup Code")
        if new_text == old_text:
            return False, "no textual change"
        row = int(ref[1:])
        if dry_run:
            return True, f"{ref} (row {row}) si#{si}: {old_text!r} -> {new_text!r}"

        data = {n: z.read(n) for n in names}
        data["xl/sharedStrings.xml"] = (ss[:s0] + new_text + ss[s1:]).encode("utf8")
    finally:
        z.close()

    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        for n in names:
            zo.writestr(n, data[n])
    tmp.replace(path)
    return True, f"{ref} (row {row}): {old_text!r} -> {new_text!r}"


def can_move(path: Path) -> bool:
    from openpyxl import load_workbook
    try:
        load_workbook(path).close()
        return True
    except Exception:
        return False


def move_row(path: Path, target_row: int = 42) -> tuple[bool, str]:
    from openpyxl import load_workbook
    wb = load_workbook(path)
    try:
        ws = next((w for w in wb.worksheets if PROPERTY_SHEET_HINT in w.title.lower()), None)
        if ws is None:
            return False, "no Property sheet"
        src = next((r for r in range(1, ws.max_row + 1)
                    if label_key(ws.cell(row=r, column=1).value) == NEW_KEY), None)
        if src is None:
            return False, "renamed label not found"
        if src == target_row:
            return False, f"already at row {target_row}"
        # Carry the STYLE across, not just the value. insert_rows() gives the new
        # row default formatting; the A-column label happens to inherit from the
        # row above, but the value cells (B onward) lose their borders, white
        # fill, wrap-text, alignment and font size. Copying _style (the style
        # index) reproduces the cell's formatting exactly.
        ncols = ws.max_column
        cells = []
        for c in range(1, ncols + 1):
            src_cell = ws.cell(row=src, column=c)
            cells.append((src_cell.value, src_cell._style))
        src_h = ws.row_dimensions[src].height

        ws.delete_rows(src)
        ws.insert_rows(target_row)
        for c, (v, style) in enumerate(cells, start=1):
            tgt = ws.cell(row=target_row, column=c)
            tgt.value = v
            tgt._style = style
        if src_h is not None:
            ws.row_dimensions[target_row].height = src_h
        wb.save(path)
        return True, f"row {src} -> {target_row}"
    finally:
        wb.close()
