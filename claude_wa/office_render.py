"""Markdown -> .xlsx / .pptx without LibreOffice (openpyxl, python-pptx).

xlsx: every Markdown table becomes a sheet (a preceding heading names it);
      numbers are stored as numbers so formulas and sums work in Excel.
pptx: each `#`/`##` heading starts a slide; the lines under it (bullets or
      paragraphs) are the body; the first `#` heading is the title slide.
"""
from __future__ import annotations

import re
from pathlib import Path

_NUM = re.compile(r"^-?\d+(?:[.,]\d+)?%?$")
_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


_EMPH = re.compile(r"^(\*\*|__|\*|_|`)(.*?)\1$")


def _plain(cell: str) -> tuple[str, bool]:
    """Cell text without Markdown emphasis, and whether it was emphasised."""
    c, bold = cell.strip(), False
    while (m := _EMPH.match(c)):
        c, bold = m.group(2).strip(), True
    return c, bold


def _cells(line: str) -> list[str]:
    return [c.strip() for c in _ROW.match(line).group(1).split("|")]


_THOUSANDS = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?$")


def _value(s: str):
    t = s.replace(" ", "")
    if _THOUSANDS.match(t):
        t = t.replace(",", "")
    elif _NUM.match(t) and "," in t and "." not in t:
        t = t.replace(",", "")  # "1,200" (no decimals) is a thousands group
    if not _NUM.match(t):
        return s
    try:
        if t.endswith("%"):
            return float(t[:-1]) / 100
        return int(t) if re.fullmatch(r"-?\d+", t) else float(t.replace(",", "."))
    except ValueError:
        return s


def tables(md: str) -> list[tuple[str, list[list[str]]]]:
    """[(sheet name, rows)] from the Markdown; headings name the next table."""
    out, name, rows, lines = [], "Sheet", [], md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("#"):
            name = line.lstrip("# ").strip()[:31] or name
        if _ROW.match(line) and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and _ROW.match(lines[i]):
                rows.append(_cells(lines[i]))
                i += 1
            out.append((name, rows))
            continue
        i += 1
    return out


def render_xlsx(md: str, out: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    tabs = tables(md)
    if not tabs:  # no table: one sheet with the text, one line per row
        tabs = [("Sheet", [[l] for l in md.splitlines() if l.strip()])]
    used: set[str] = set()
    for name, rows in tabs:
        base = re.sub(r"[\[\]\*\?/\\:]", "", name)[:28] or "Sheet"
        title, n = base, 1
        while title in used:
            n += 1
            title = f"{base} {n}"
        used.add(title)
        ws = wb.create_sheet(title)
        for r, row in enumerate(rows, 1):
            for c, cell in enumerate(row, 1):
                text, bold = _plain(cell)
                x = ws.cell(row=r, column=c, value=_value(text) if r > 1 else text)
                if bold and r > 1:
                    x.font = Font(bold=True)
        for c in range(1, (max(len(r) for r in rows) if rows else 0) + 1):
            h = ws.cell(row=1, column=c)
            h.font = Font(bold=True)
            h.fill = PatternFill("solid", fgColor="DDDDDD")
            h.alignment = Alignment(horizontal="center")
            width = max((len(str(ws.cell(row=r, column=c).value or "")) for r in range(1, len(rows) + 1)), default=8)
            ws.column_dimensions[get_column_letter(c)].width = min(max(10, width + 2), 50)
        ws.freeze_panes = "A2"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def slides(md: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for line in md.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if s.lstrip().startswith("#"):
            out.append((s.lstrip("# ").strip(), []))
        else:
            if not out:
                out.append(("", []))
            out[-1][1].append(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s).strip())
    return out


def render_pptx(md: str, out: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()
    decks = slides(md) or [("", [])]
    for i, (title, body) in enumerate(decks):
        if i == 0 and not body:
            s = prs.slides.add_slide(prs.slide_layouts[0])  # title slide
            s.shapes.title.text = title
            if len(s.placeholders) > 1:
                s.placeholders[1].text = ""
            continue
        s = prs.slides.add_slide(prs.slide_layouts[1])  # title + content
        s.shapes.title.text = title
        tf = s.placeholders[1].text_frame
        tf.clear()
        for j, line in enumerate(body[:12]):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.text = line
            p.font.size = Pt(20 if len(body) <= 6 else 16)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)
    return out
