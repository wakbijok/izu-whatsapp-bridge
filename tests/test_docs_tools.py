import asyncio
from pathlib import Path

import pytest

from claude_wa import docs_tools
from claude_wa.docs_tools import render, safe_name


def test_safe_name():
    assert safe_name("Surat Rasmi: cuti!!", "docx") == "Surat Rasmi cuti.docx"
    assert safe_name("../../etc/passwd", "pdf") == "passwd.pdf"
    assert safe_name("", "md") == "dokumen.md"


def test_render_text_formats(tmp_path):
    out = render("# Tajuk\n\n- satu\n- dua", "md", tmp_path / "a.md")
    assert out.read_text().startswith("# Tajuk")
    out = render("# Tajuk", "html", tmp_path / "a.html")
    assert "<h1>Tajuk</h1>" in out.read_text()


@pytest.mark.skipif(docs_tools.SOFFICE is None, reason="LibreOffice not installed")
def test_render_docx_and_pdf(tmp_path):
    md = "# Surat\n\nKepada **Puan**,\n\n| Item | Harga |\n|---|---|\n| Roti | RM3 |\n"
    docx = render(md, "docx", tmp_path / "surat.docx")
    assert docx.stat().st_size > 2000 and docx.read_bytes()[:2] == b"PK"
    pdf = render(md, "pdf", tmp_path / "surat.pdf")
    assert pdf.read_bytes()[:4] == b"%PDF"


def test_send_file_refuses_paths_outside_root(tmp_path):
    from claude_wa.docs_tools import _inside

    assert _inside(tmp_path / "a.docx", tmp_path.resolve())
    assert not _inside(tmp_path / ".." / "x", tmp_path.resolve())


def test_render_xlsx_tables_and_numbers(tmp_path):
    from openpyxl import load_workbook

    md = ("# Bajet Oktober\n\n| Item | RM |\n|---|---|\n| Sewa | 1,200 |\n| Makan | 850.50 |\n"
          "| **Jumlah** | **2,050.50** |\n\n"
          "# Senarai Beli\n\n| Barang | Kuantiti |\n|---|---|\n| Telur | 30 |\n")
    out = render(md, "xlsx", tmp_path / "bajet.xlsx")
    wb = load_workbook(out)
    assert wb.sheetnames == ["Bajet Oktober", "Senarai Beli"]
    ws = wb["Bajet Oktober"]
    assert ws["A1"].value == "Item" and ws["B2"].value == 1200 and ws["B3"].value == 850.5
    assert ws["A4"].value == "Jumlah" and ws["B4"].value == 2050.5 and ws["A4"].font.bold
    assert wb["Senarai Beli"]["B2"].value == 30


def test_render_pptx_slides(tmp_path):
    from pptx import Presentation

    md = "# Hari Keluarga 2026\n\n## Aktiviti\n- Sarapan\n- Sukaneka\n\n## Belanjawan\nRM 500 makanan\n"
    out = render(md, "pptx", tmp_path / "deck.pptx")
    prs = Presentation(out)
    titles = [s.shapes.title.text for s in prs.slides]
    assert titles == ["Hari Keluarga 2026", "Aktiviti", "Belanjawan"]
    assert "Sukaneka" in prs.slides[1].placeholders[1].text_frame.text


def test_xlsx_number_parsing():
    from claude_wa.office_render import _value

    assert _value("1,200") == 1200 and _value("2,050.50") == 2050.5 and _value("850.50") == 850.5
    assert _value("30") == 30 and _value("-3.5") == -3.5 and _value("12%") == 0.12
    assert _value("RM 1,200") == "RM 1,200" and _value("Telur") == "Telur"
