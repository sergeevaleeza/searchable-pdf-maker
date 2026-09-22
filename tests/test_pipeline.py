"""Tests for the conversion pipeline.

Structure tests run anywhere; end-to-end tests are skipped when Tesseract/Poppler/OCRmyPDF are missing.
Run with:  python -m pytest -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
import make_samples  # noqa: E402

TOOLS = app.find_tools()
HAS_OCR = all(TOOLS.get(t) for t in ("tesseract", "pdftoppm"))
HAS_OCRMYPDF = HAS_OCR and TOOLS.get("ocrmypdf") and TOOLS.get("ghostscript")


def line(text, size=11.0, top=100.0, left=72.0, right=500.0, conf=95.0, bold=False):
    return app.Line(text, size, conf, left, top, right, top + size, bold)


def page(number, paras, source="ocr", height=792.0):
    sizes = [l.size for p in paras for l in p for _ in l.text.split()]
    return app.PageContent(number, 612.0, height, source, [app.Para(p) for p in paras], sizes)


# ---------------------------------------------------------------- structure reconstruction

def test_soft_wraps_are_joined_and_hyphenation_undone():
    assert app.join_lines(["a well-", "known fact that", "is co-", "Operative"]) == \
        "a wellknown fact that is co- Operative"


def test_headings_are_tiered_by_size():
    md = app.build_markdown([page(1, [
        [line("Big Title", 26, top=80)],
        [line("Body text " * 8, top=130), line("more body text here.", top=145)],
        [line("Section", 18, top=200)],
        [line("Body text " * 8, top=240)],
        [line("Subsection", 14, top=280)],
        [line("Body text " * 8, top=310)],
    ])])
    assert "# Big Title" in md
    assert "\n## Section" in md
    assert "\n### Subsection" in md


def test_wrapped_heading_fragments_merge():
    md = app.build_markdown([page(1, [
        [line("A Long Heading That", 20, top=100)],
        [line("Wraps Onto Two Lines", 20, top=124)],
        [line("Body " * 20, top=170)],
    ])])
    assert "# A Long Heading That Wraps Onto Two Lines" in md


def test_ordered_list_and_merged_items():
    md = app.build_markdown([page(1, [
        [line("1. First item", top=100), line("2. Second item that", top=114),
         line("wraps onto a new line", top=128), line("3. Third. 4. Fourth", top=142)],
    ])])
    assert "1. First item\n2. Second item that wraps onto a new line\n3. Third.\n4. Fourth" in md


def test_page_numbers_and_running_headers_removed():
    pages = [page(n, [
        [line("Company Confidential", 9, top=20)],
        [line(f"Body paragraph on page {n}.", top=300)],
        [line(f"Page {n}", 9, top=760)],
    ]) for n in range(1, 5)]
    md = app.build_markdown(pages)
    assert "Confidential" not in md and "Page " not in md
    assert md.count("Body paragraph") == 4


def test_page_label_glued_to_body_line_is_stripped():
    md = app.build_markdown([page(12, [[line("the end of the sentence. Page 12", top=740)]])])
    assert md.strip() == "the end of the sentence."


def test_figure_junk_dropped():
    md = app.build_markdown([page(1, [
        [line("Real sentence here.", top=100)],
        [line("~ | = -- %", conf=40, top=300)],
        [line("0 10 20 30", conf=60, top=320)],
        [line("x", conf=50, top=340)],
    ])])
    assert md.strip() == "Real sentence here."


def test_paragraph_continues_across_pages():
    md = app.build_markdown([
        page(1, [[line("This sentence starts on one page and", top=700)]]),
        page(2, [[line("finishes on the next one.", top=80)]]),
    ])
    assert md.strip() == "This sentence starts on one page and finishes on the next one."


def test_bullets_including_ocr_misreads():
    md = app.build_markdown([page(1, [
        [line("• Alpha", top=100), line("¢ Beta", top=114), line("e Gamma", top=128)],
    ])])
    assert "- Alpha\n- Beta\n- Gamma" in md


def test_glyph_size_normalises_letter_shapes():
    # Same font size, different letter shapes -> similar estimated size.
    estimates = [app._glyph_size("Risks", 7.2), app._glyph_size("was", 5.2), app._glyph_size("Appendix", 9.3)]
    assert max(estimates) / min(estimates) < 1.05


# ---------------------------------------------------------------- end to end

def test_docx_to_markdown(tmp_path):
    md = app.docx_to_markdown(make_samples.make_docx(tmp_path / "s.docx"))
    assert md.startswith("# Team Handbook")
    assert "## Getting Started" in md and "### Tools" in md
    assert "**carefully**" in md and "*first day*" in md
    assert "1. Collect your laptop\n2. Set up your accounts" in md
    assert "- Chat for quick questions\n- Email for decisions" in md
    assert "| Chat | IT |" in md


def test_text_pdf_detected_and_extracted(tmp_path):
    pytest.importorskip("reportlab")
    src = make_samples.make_text_pdf(tmp_path / "t.pdf")
    info = app.analyze_pdf(src, TOOLS)
    assert not info.image_only
    res = app.convert(src, tmp_path, app.Options(make_pdf=False))
    assert res.stats["OCR pages"] == 0
    assert "# Project Charter" in res.markdown and "## Goals" in res.markdown
    assert "1. Move all services" in res.markdown and "Page 1" not in res.markdown


@pytest.mark.skipif(not HAS_OCR, reason="Tesseract/Poppler not installed")
def test_image_only_pdf_end_to_end(tmp_path):
    src = make_samples.make_image_pdf(tmp_path / "scan.pdf")
    assert app.analyze_pdf(src, TOOLS).image_only

    res = app.convert(src, tmp_path, app.Options(make_pdf=bool(HAS_OCRMYPDF)))
    md = res.markdown
    assert md.startswith("# Annual Operations Review")
    assert "## Logistics Performance and Warehouse Consolidation Across All Northern Regions" in md
    assert "## Risks and Outlook" in md and "### Staffing Risk" in md
    assert "3. Renegotiate carrier contracts.\n4. Improve stock accuracy." in md
    assert "while the move was under way" in md  # joined across the page break
    assert "- Pilot electric vans on two urban routes." in md
    assert "Page 1" not in md and "ACME" not in md

    if HAS_OCRMYPDF:
        assert res.pdf_bytes and res.pdf_bytes.startswith(b"%PDF")
        out = tmp_path / "searchable.pdf"
        out.write_bytes(res.pdf_bytes)
        info = app.analyze_pdf(out, TOOLS)
        assert not info.image_only and info.pages == 3


def test_cli_writes_both_outputs(tmp_path):
    if not HAS_OCRMYPDF:
        pytest.skip("OCR toolchain not installed")
    src = make_samples.make_image_pdf(tmp_path / "scan.pdf")
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(Path(app.__file__)), str(src), "-o", str(out)],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    assert (out / "scan.md").read_text(encoding="utf-8").startswith("# Annual Operations Review")
    assert (out / "scan_searchable.pdf").stat().st_size > 0
