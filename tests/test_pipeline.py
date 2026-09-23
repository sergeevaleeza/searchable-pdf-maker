"""Tests for the conversion pipeline.

Structure tests run anywhere; end-to-end tests are skipped when Tesseract/Poppler/OCRmyPDF are missing.
Run with:  python -m pytest -q
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
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

def test_soft_wraps_are_joined_and_compounds_keep_their_hyphen():
    assert app.join_lines(["a well-", "known fact that", "is state-of-the-", "art"]) == \
        "a well-known fact that is state-of-the-art"


# ---------------------------------------------------------------- fix 1: line-end hyphens

@pytest.mark.parametrize("lines, expected", [
    (["T-", "cell"], "T-cell"),
    (["beta-", "blocker"], "beta-blocker"),
    (["CD4-", "positive"], "CD4-positive"),
    (["informa-", "tion"], "information"),
    (["quantita-", "tive"], "quantitative"),
    (["The (Informa-", "tion) age"], "The (Information) age"),  # punctuation stripped for lookup, case kept
    (["soft\u00ad", "ware"], "software"),
    (["xylo\u00ad", "phonequux"], "xylophonequux"),  # soft hyphen merges even for unknown words
    (["costs rose -", "sharply"], "costs rose - sharply"),  # a free-standing dash is not a hyphen
])
def test_line_end_hyphens(lines, expected):
    assert app.join_lines(lines) == expected


def test_wordlist_file_is_used_when_present(monkeypatch, tmp_path):
    wordlist = tmp_path / "words"
    wordlist.write_text("Zymurgy\nfoo-bar\nfoobar\n", encoding="utf-8")
    monkeypatch.setattr(app, "WORDLIST_PATH", str(wordlist))
    assert app.dictionary()[1] == str(wordlist)
    assert app.join_lines(["zymur-", "gy"]) == "zymurgy"
    assert app.join_lines(["foo-", "bar"]) == "foo-bar"  # hyphenated form is itself listed -> keep it
    assert app.join_lines(["informa-", "tion"]) == "informa-tion"  # not in this list -> preserved


def test_missing_wordlist_falls_back_to_builtin_list(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "WORDLIST_PATH", str(tmp_path / "does-not-exist"))
    words, source = app.dictionary()
    assert source == "built-in fallback list" and words is app.FALLBACK_WORDS
    assert app.join_lines(["informa-", "tion"]) == "information"
    assert app.join_lines(["develop-", "ment"]) == "development"
    assert app.join_lines(["IL-", "6R"]) == "IL-6R"  # unknown term keeps its hyphen
    assert app.join_lines(["zymur-", "gy"]) == "zymur-gy"


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


# ---------------------------------------------------------------- fix 2: column detection

class FakePlumberPage:
    """Just enough of a pdfplumber page for text_layer_page."""

    def __init__(self, words, width=612.0, height=792.0):
        self.width, self.height, self._words = width, height, words

    def extract_words(self, **_):
        return [dict(w) for w in self._words]


def _layout(text, x, width, top=60.0, size=10.0, char_w=5.0):
    """Wrap text into a column starting at x; returns one list of pdfplumber-style word dicts per line."""
    lines, cur, cur_x, y = [], [], x, top
    for word in text.split():
        w = len(word) * char_w
        if cur and cur_x + w > x + width:
            lines.append(cur)
            cur, cur_x, y = [], x, y + size * 1.2
        cur.append({"text": word, "x0": cur_x, "x1": cur_x + w, "top": y, "bottom": y + size,
                    "size": size, "fontname": "Helvetica"})
        cur_x += w + char_w
    return lines + [cur]


def _across_gutter(left, right):
    """Word order of a content stream that runs straight across the gutter, row by row."""
    return [w for i in range(max(len(left), len(right)))
            for w in (left[i] if i < len(left) else []) + (right[i] if i < len(right) else [])]


def _page_text(content):
    return " ".join(app.join_lines([l.text for l in p.lines]) for p in content.paras)


LEFT = ("Alpha cells were cultured for three days under standard conditions and the first column describes "
        "how every sample was prepared in careful detail including the buffer used for each plate and the")
RIGHT = ("washing steps that followed. Beta readings were collected on the plate reader and exported for "
         "review while the second column explains how outliers were removed before the final comparison.")


def test_two_columns_are_read_one_after_the_other():
    words = _across_gutter(_layout(LEFT, 50, 250), _layout(RIGHT, 312, 250))  # 12pt gutter
    assert len(app._find_gutters(words, 612)) == 1
    # Column 1 fully, then column 2, with the sentence joined across the gutter.
    assert _page_text(app.text_layer_page(FakePlumberPage(words), 1)) == f"{LEFT} {RIGHT}"


def test_full_width_title_stays_above_the_columns():
    # A title crossing the gutter; the columns are page-length so the title is a small share of the rows.
    title = [{"text": t, "x0": 150 + i * 70, "x1": 210 + i * 70, "top": 20, "bottom": 36, "size": 16,
              "fontname": "Helvetica-Bold"} for i, t in enumerate("A Study Across Both Columns".split())]
    left, right = " ".join([LEFT] * 4), " ".join([RIGHT] * 4)
    words = title + _across_gutter(_layout(left, 50, 250), _layout(right, 312, 250))
    content = app.text_layer_page(FakePlumberPage(words), 1)
    assert content.paras[0].lines[0].text == "A Study Across Both Columns"
    assert _page_text(content) == f"A Study Across Both Columns {left} {right}"


def test_single_column_page_is_unchanged(monkeypatch):
    text = ("Ragged single column text. " * 3 + "Lines end at different places, some short. ") * 6
    words = [w for line_ in _layout(text, 72, 460) for w in line_]
    assert app._find_gutters(words, 612) == []
    with_detection = app.text_layer_page(FakePlumberPage(words), 1)
    monkeypatch.setattr(app, "_find_gutters", lambda *_: [])
    without = app.text_layer_page(FakePlumberPage(words), 1)
    assert [[l.text for l in p.lines] for p in with_detection.paras] == \
        [[l.text for l in p.lines] for p in without.paras]


def test_two_column_pdf_with_real_pdfplumber(tmp_path):
    pytest.importorskip("reportlab")
    import pdfplumber
    src = make_samples.make_two_column_pdf(tmp_path / "two_col.pdf")
    with pdfplumber.open(str(src)) as pdf:
        text = _page_text(app.text_layer_page(pdf.pages[0], 1))
    assert text.startswith("A Study of Two Columns Across the Full Page Width ")
    assert f"{make_samples.TWO_COLUMN_LEFT} {make_samples.TWO_COLUMN_RIGHT}" in text


# ---------------------------------------------------------------- fix 3: memory-aware scheduling

class FakeProc:
    returncode = 0

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self):
        pass


def _fake_free_memory(monkeypatch, mb):
    fake = SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=mb * 2**20))
    monkeypatch.setattr(app, "psutil", fake)


def test_schedule_depends_on_free_memory(monkeypatch):
    opts = app.Options(min_concurrent_mem_mb=1500)
    _fake_free_memory(monkeypatch, 800)
    assert app.choose_schedule(opts)[0] == "sequential"
    _fake_free_memory(monkeypatch, 4000)
    assert app.choose_schedule(opts)[0] == "concurrent"
    assert app.choose_schedule(app.Options(force_sequential=True))[0] == "sequential"
    monkeypatch.setattr(app, "psutil", None)  # psutil unavailable -> safe default
    assert app.choose_schedule(opts)[0] == "sequential"


@pytest.fixture
def pipeline_spy(monkeypatch, tmp_path):
    """Stub the heavy stages of _convert_pdf and record the order in which they run."""
    events = []
    monkeypatch.setattr(app, "find_tools", lambda: dict.fromkeys(
        ("tesseract", "pdftoppm", "ghostscript", "ocrmypdf"), "x"))
    monkeypatch.setattr(app, "analyze_pdf", lambda *_: app.PdfInfo(1, [(612.0, 792.0)], False, 0))  # image-only

    def extract(*_, workers=None, **__):
        events.append(("extract_start", workers))
        events.append("extract_done")
        return [page(1, [[line("Recovered text.")]])]

    def start(src, dst, opts, jobs, log):
        events.append(("ocrmypdf_start", jobs))
        return FakeProc()

    def finish(*_):
        events.append("ocrmypdf_done")
        return b"%PDF-fake"

    monkeypatch.setattr(app, "extract_pdf_pages", extract)
    monkeypatch.setattr(app, "start_ocrmypdf", start)
    monkeypatch.setattr(app, "finish_ocrmypdf", finish)
    src = tmp_path / "in.pdf"
    src.write_bytes(b"%PDF-1.4")
    return SimpleNamespace(events=events, src=src, workdir=tmp_path)


def test_sequential_mode_extracts_before_ocrmypdf(monkeypatch, pipeline_spy):
    _fake_free_memory(monkeypatch, 500)
    monkeypatch.setattr(app.os, "cpu_count", lambda: 8)
    res = app.convert(pipeline_spy.src, pipeline_spy.workdir, app.Options(),
                      on_markdown=lambda md: pipeline_spy.events.append("markdown_ready"))
    # Extraction completes (and the Markdown is handed over) before OCRmyPDF starts; each gets all cores.
    assert pipeline_spy.events == [("extract_start", 8), "extract_done", "markdown_ready",
                                   ("ocrmypdf_start", 8), "ocrmypdf_done"]
    assert res.stats["schedule"] == "sequential" and res.pdf_bytes == b"%PDF-fake"


def test_concurrent_mode_overlaps_the_stages(monkeypatch, pipeline_spy):
    _fake_free_memory(monkeypatch, 8000)
    res = app.convert(pipeline_spy.src, pipeline_spy.workdir, app.Options())
    assert pipeline_spy.events[:2] == [("ocrmypdf_start", app._worker_split()[1]), ("extract_start", None)]
    assert res.stats["schedule"] == "concurrent" and res.pdf_bytes == b"%PDF-fake"


@pytest.mark.parametrize("failing_stage", ["finish_ocrmypdf", "start_ocrmypdf"])
def test_ocrmypdf_failure_in_sequential_mode_keeps_the_markdown(monkeypatch, pipeline_spy, failing_stage):
    _fake_free_memory(monkeypatch, 500)

    def fail(*_):
        if failing_stage == "finish_ocrmypdf":
            raise app.PipelineError("OCRmyPDF was killed by the system")
        raise OSError("cannot allocate memory")

    monkeypatch.setattr(app, failing_stage, fail)
    res = app.convert(pipeline_spy.src, pipeline_spy.workdir, app.Options())
    assert res.markdown.strip() == "Recovered text."
    assert res.pdf_bytes is None
    assert any("OCRmyPDF" in w for w in res.warnings)
