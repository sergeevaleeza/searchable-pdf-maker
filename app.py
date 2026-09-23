"""Searchable PDF Maker.

Upload a PDF (text-based or scanned/image-only) or a Word document and get back:
  * a clean, structured Markdown file, and
  * a searchable PDF (original pages + invisible OCR text layer, via OCRmyPDF).

Run the UI:   streamlit run app.py
Run headless: python app.py input.pdf -o out/
"""

from __future__ import annotations

import argparse
import bisect
import functools
import glob
import hashlib
import logging
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Callable

try:
    import psutil
except ImportError:  # without it free memory is unknown, so we always use the safe sequential schedule
    psutil = None

# Tesseract uses OpenMP; we parallelise across pages ourselves, so keep each process single-threaded.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

log = logging.getLogger("searchable_pdf_maker")

ProgressFn = Callable[[float, str], None]

MAX_PAGE_PIXELS = 60_000_000  # cap render size for very large pages (posters, drawings)
MIN_TEXT_CHARS_PER_PAGE = 10  # fewer alphanumerics than this on a page => treat the page as image-only


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Memory-aware scheduling (see choose_schedule). Tesseract and OCRmyPDF are both RAM-heavy; running them
# side by side on a small host (Streamlit Community Cloud has ~1 GB) gets the container OOM-killed.
MIN_CONCURRENT_MEM_MB = _env_int("SPM_MIN_CONCURRENT_MEM_MB", 1500)  # free RAM needed to run both at once
FORCE_SEQUENTIAL = os.environ.get("SPM_FORCE_SEQUENTIAL", "").strip().lower() in ("1", "true", "yes", "on")

# Column detection for text-layer pages (see _find_gutters).
COLUMN_MIN_GUTTER_WIDTH_FRAC = 0.012  # a gutter is at least this wide, as a fraction of page width (~7pt)
COLUMN_MIN_GUTTER_HEIGHT_FRAC = 0.75  # ...and free of words in at least this share of the text rows
COLUMN_MIN_SIDE_WORD_FRAC = 0.15  # every column must hold at least this share of the page's words
COLUMN_MAX_COLUMNS = 4
COLUMN_MIN_WORDS = 30  # fewer words than this: too little evidence, treat as a single column


class PipelineError(Exception):
    """An error with a message that is safe and useful to show to the user."""


# --------------------------------------------------------------------------------------
# System tools
# --------------------------------------------------------------------------------------

def _add_common_tool_dirs_to_path() -> None:
    """Make tools installed in their default (non-PATH) locations discoverable."""
    if os.name == "nt":
        patterns = [
            r"C:\Program Files\Tesseract-OCR",
            r"C:\Program Files (x86)\Tesseract-OCR",
            r"C:\Program Files\LibreOffice\program",
            r"C:\Program Files (x86)\LibreOffice\program",
            r"C:\Program Files\gs\gs*\bin",
            r"C:\Program Files\poppler*\Library\bin",
            r"C:\Program Files\poppler*\bin",
        ]
    else:
        patterns = ["/Applications/LibreOffice.app/Contents/MacOS", "/opt/homebrew/bin", "/usr/local/bin"]
    current = os.environ.get("PATH", "").split(os.pathsep)
    extra = [d for p in patterns for d in sorted(glob.glob(p)) if os.path.isdir(d) and d not in current]
    if extra:
        os.environ["PATH"] = os.pathsep.join(current + extra)


_add_common_tool_dirs_to_path()


def find_tools() -> dict[str, str | None]:
    """Locate the external programs the pipeline relies on (None = not found)."""
    return {
        "tesseract": shutil.which("tesseract"),
        "pdftoppm": shutil.which("pdftoppm"),
        "pdffonts": shutil.which("pdffonts"),
        "pdftotext": shutil.which("pdftotext"),
        "ghostscript": shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c"),
        "soffice": shutil.which("soffice") or shutil.which("libreoffice"),
        "ocrmypdf": "python -m ocrmypdf" if find_spec("ocrmypdf") else None,
    }


TOOL_HINTS = {
    "tesseract": "Tesseract OCR is not installed (apt: tesseract-ocr, brew: tesseract, "
                 "Windows: UB-Mannheim installer).",
    "pdftoppm": "Poppler is not installed (apt: poppler-utils, brew: poppler).",
    "ghostscript": "Ghostscript is not installed (apt: ghostscript, brew: ghostscript).",
    "soffice": "LibreOffice is not installed (apt: libreoffice, brew: --cask libreoffice).",
    "ocrmypdf": "The ocrmypdf Python package is not installed (pip install ocrmypdf).",
}


def tesseract_languages() -> list[str]:
    try:
        import pytesseract
        return sorted(l for l in pytesseract.get_languages(config="") if l != "osd")
    except Exception:
        return []


def _run(cmd: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


# --------------------------------------------------------------------------------------
# Data model shared by the OCR and text-layer extractors
# --------------------------------------------------------------------------------------

@dataclass
class Line:
    text: str
    size: float  # median font size in points (OCR: estimated from word box heights)
    conf: float  # mean word confidence 0-100 (100 for a real text layer)
    left: float
    top: float
    right: float
    bottom: float
    bold: bool = False

    @property
    def n_words(self) -> int:
        return len(self.text.split())


@dataclass
class Para:
    lines: list[Line]


@dataclass
class PageContent:
    number: int  # 1-based
    width: float
    height: float
    source: str  # "ocr" or "text"
    paras: list[Para] = field(default_factory=list)
    word_sizes: list[float] = field(default_factory=list)
    conf: float | None = None


@dataclass
class Block:
    """A rendered Markdown element."""
    kind: str  # "h", "p", "ol", "ul"
    page: int
    text: str = ""
    level: int = 0
    items: list[list] = field(default_factory=list)  # [marker/number, [text lines]]
    rel: float = 1.0  # size relative to body text
    top: float = 0.0
    bottom: float = 0.0
    size: float = 0.0


@dataclass
class Options:
    dpi: int = 300
    force_ocr: bool = False
    lang: str = "eng"
    heading_ratio: float = 1.5
    make_pdf: bool = True
    force_sequential: bool = FORCE_SEQUENTIAL
    min_concurrent_mem_mb: int = MIN_CONCURRENT_MEM_MB


@dataclass
class Result:
    markdown: str
    pdf_bytes: bytes | None
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# PDF inspection
# --------------------------------------------------------------------------------------

@dataclass
class PdfInfo:
    pages: int
    page_sizes: list[tuple[float, float]]  # points
    has_fonts: bool
    text_chars: int

    @property
    def image_only(self) -> bool:
        return not self.has_fonts or self.text_chars < max(20, 2 * self.pages)


def analyze_pdf(path: Path, tools: dict) -> PdfInfo:
    """Page count/sizes plus whether a usable text layer exists (pdffonts + pdftotext, pypdf fallback)."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise PipelineError("This PDF is password-protected. Remove the password and upload it again.")
        sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages]
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Could not read the PDF (is the file damaged?): {exc}") from exc
    if not sizes:
        raise PipelineError("The PDF has no pages.")

    has_fonts = text_chars = None
    if tools.get("pdffonts"):
        try:
            out = _run([tools["pdffonts"], str(path)], timeout=180).stdout.splitlines()
            sep = next((i for i, l in enumerate(out) if l.startswith("---")), None)
            if sep is not None:
                has_fonts = any(l.strip() for l in out[sep + 1:])
        except Exception:
            pass
    if tools.get("pdftotext"):
        try:
            out = _run([tools["pdftotext"], "-q", "-enc", "UTF-8", str(path), "-"], timeout=300).stdout
            text_chars = sum(c.isalnum() for c in out)
        except Exception:
            pass
    if has_fonts is None:
        has_fonts = any(_page_has_fonts(p) for p in reader.pages)
    if text_chars is None:
        text_chars = 0
        for p in reader.pages:
            try:
                text_chars += sum(c.isalnum() for c in (p.extract_text() or ""))
            except Exception:
                pass
    return PdfInfo(len(sizes), sizes, bool(has_fonts), text_chars)


def _page_has_fonts(page) -> bool:
    try:
        res = page.get("/Resources")
        res = res.get_object() if res is not None else None
        return bool(res and res.get("/Font"))
    except Exception:
        return False


# --------------------------------------------------------------------------------------
# Extraction: OCR (Tesseract word boxes) and text layer (pdfplumber)
# --------------------------------------------------------------------------------------

def _median(values, default=0.0):
    return statistics.median(values) if values else default


def _has_alnum(s: str) -> bool:
    return any(c.isalnum() for c in s)


_ASCENDERS = set("bdfhijklt")
_DESCENDERS = set("gjpqy")


def _glyph_size(text: str, height: float) -> float | None:
    """Estimate font size from a word's box height, correcting for which letters it contains.

    "Risks" (caps/ascenders only), "gray" (descenders only), "was" (x-height only) and "Appendix"
    (both) have very different box heights at the same font size. Dividing by the typical fraction
    of the em each shape covers makes heights comparable across words (and so across headings).
    """
    letters = [c for c in text if c.isalnum()]
    if not letters:
        return None
    asc = any(c.isupper() or c.isdigit() or c in _ASCENDERS for c in letters)
    desc = any(c in _DESCENDERS for c in letters)
    return height / {(True, True): 0.93, (True, False): 0.72, (False, True): 0.73, (False, False): 0.52}[asc, desc]


def ocr_page(pdf_path: Path, page_no: int, page_size: tuple[float, float], opts: Options) -> PageContent:
    """Render one page and OCR it into lines/paragraphs using Tesseract's block/par/line indices."""
    import pytesseract
    from pdf2image import convert_from_path

    w_pt, h_pt = page_size
    dpi = opts.dpi
    pixels = (w_pt / 72 * dpi) * (h_pt / 72 * dpi)
    if pixels > MAX_PAGE_PIXELS:
        dpi = max(72, int(dpi * (MAX_PAGE_PIXELS / pixels) ** 0.5))

    images = convert_from_path(str(pdf_path), dpi=dpi, first_page=page_no, last_page=page_no,
                               fmt="png", grayscale=True, thread_count=1)
    if not images:
        raise RuntimeError("page could not be rendered")
    img = images[0]
    scale = 72.0 / dpi  # pixels -> points, so pages rendered at different DPIs stay comparable
    data = pytesseract.image_to_data(img, lang=opts.lang, config="--psm 3",
                                     output_type=pytesseract.Output.DICT, timeout=600)
    page = PageContent(page_no, img.width * scale, img.height * scale, "ocr")
    img.close()

    lines: dict[tuple, list[dict]] = {}
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        conf = float(data["conf"][i])
        if data["level"][i] != 5 or not text or conf < 0:
            continue
        word = {
            "text": text, "conf": conf,
            "left": data["left"][i] * scale, "top": data["top"][i] * scale,
            "right": (data["left"][i] + data["width"][i]) * scale,
            "bottom": (data["top"][i] + data["height"][i]) * scale,
        }
        word["size"] = _glyph_size(text, data["height"][i] * scale)
        lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(word)
        if word["size"] is not None:
            page.word_sizes.append(word["size"])

    paras: dict[tuple, list[Line]] = {}
    confs = []
    for (block, par, _), words in lines.items():
        sized = [w["size"] for w in words if w["size"] is not None] or [w["bottom"] - w["top"] for w in words]
        line = Line(
            text=" ".join(w["text"] for w in words),
            size=_median(sized),
            conf=sum(w["conf"] for w in words) / len(words),
            left=min(w["left"] for w in words), top=min(w["top"] for w in words),
            right=max(w["right"] for w in words), bottom=max(w["bottom"] for w in words),
        )
        paras.setdefault((block, par), []).append(line)
        confs.extend(w["conf"] for w in words)
    page.paras = _refine_paragraphs(list(paras.values()), use_font_changes=False)
    page.conf = sum(confs) / len(confs) if confs else None
    return page


_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "\u00a0": " "})
_BOLD_RE = re.compile(r"bold|black|heavy|semibold|demi", re.I)


def text_layer_page(page, page_no: int) -> PageContent:
    """Extract words from a pdfplumber page and rebuild lines/paragraphs from their geometry."""
    content = PageContent(page_no, float(page.width), float(page.height), "text")
    words = []
    for w in page.extract_words(use_text_flow=True, keep_blank_chars=False, extra_attrs=["size", "fontname"]):
        w["text"] = w["text"].translate(_LIGATURES).strip()
        if not w["text"]:
            continue
        if _has_alnum(w["text"]):
            content.word_sizes.append(float(w["size"]))
        words.append(w)

    # Text flow reads straight across a narrow gutter on two-column pages, interleaving the columns.
    # So split the page into columns first and group lines within each one, reading every column
    # top to bottom before moving right. A single-column page is one chunk: behaviour is unchanged.
    for chunk in _column_chunks(words, content.width):
        lines = _words_to_lines(chunk)
        paras = _refine_paragraphs([lines] if lines else [], use_font_changes=True)
        prev = content.paras[-1] if content.paras else None
        if prev and paras and not TERMINAL_RE.search(prev.lines[-1].text) and paras[0].lines[0].text[:1].islower():
            prev.lines.extend(paras.pop(0).lines)  # a sentence that continues at the top of the next column
        content.paras.extend(paras)
    return content


def _find_gutters(words: list[dict], page_width: float) -> list[tuple[float, float]]:
    """Vertical whitespace strips that separate text columns, left to right ([] = one column).

    The text area is cut into thin horizontal slices; an x position is a gutter candidate if no word
    covers it in at least COLUMN_MIN_GUTTER_HEIGHT_FRAC of the slices that contain text, so a title
    spanning both columns doesn't hide the gutter. Candidates must be interior, wide enough, and leave
    a meaningful share of words in every column, which rules out word gaps and ragged right edges.
    """
    if len(words) < COLUMN_MIN_WORDS or COLUMN_MAX_COLUMNS < 2:
        return []
    x_min, x_max = min(w["x0"] for w in words), max(w["x1"] for w in words)
    y_min = min(w["top"] for w in words)
    slice_h = max(_median([w["bottom"] - w["top"] for w in words]) / 2, 1.0)
    n_bins = int(math.ceil(x_max - x_min)) + 1  # 1pt bins
    rows: dict[int, bytearray] = {}
    for w in words:
        b0, b1 = int(w["x0"] - x_min), int(math.ceil(w["x1"] - x_min))
        for s in range(int((w["top"] - y_min) / slice_h), int((w["bottom"] - y_min) / slice_h) + 1):
            rows.setdefault(s, bytearray(n_bins))[b0:b1] = b"\x01" * (b1 - b0)
    blocked = [sum(col) for col in zip(*rows.values())]
    max_blocked = (1 - COLUMN_MIN_GUTTER_HEIGHT_FRAC) * len(rows)

    gutters, start = [], None
    for b, n in enumerate(blocked + [len(rows) + 1]):  # sentinel closes a trailing run
        if n <= max_blocked:
            start = b if start is None else start
        elif start is not None:
            # Runs touching either edge are margins/ragged edges, not gutters.
            if start > 0 and b < n_bins and b - start >= COLUMN_MIN_GUTTER_WIDTH_FRAC * page_width:
                gutters.append((x_min + start, x_min + b))
            start = None
    gutters = sorted(sorted(gutters, key=lambda g: g[0] - g[1])[:COLUMN_MAX_COLUMNS - 1])

    # Drop gutters until every column holds enough words (guards against false splits).
    while gutters:
        mids = [(a + b) / 2 for a, b in gutters]
        counts = [0] * (len(gutters) + 1)
        for w in words:
            counts[bisect.bisect(mids, (w["x0"] + w["x1"]) / 2)] += 1
        thinnest = min(range(len(counts)), key=counts.__getitem__)
        if counts[thinnest] >= COLUMN_MIN_SIDE_WORD_FRAC * len(words):
            break
        gutters.pop(min(thinnest, len(gutters) - 1))
    return gutters


def _column_chunks(words: list[dict], page_width: float) -> list[list[dict]]:
    """Split words into reading-order chunks: each column fully, left to right.

    Rows containing a word that crosses a gutter (a title or caption spanning the page) become their
    own full-width chunk and divide the page into sections, whose columns are read separately. Words
    keep their original relative order inside each chunk.
    """
    gutters = _find_gutters(words, page_width)
    if not gutters:
        return [words]
    mids = [(a + b) / 2 for a, b in gutters]
    bands: list[list[float]] = []  # merged y-ranges of full-width rows
    for w in sorted((w for w in words if any(w["x0"] < b and w["x1"] > a for a, b in gutters)),
                    key=lambda w: w["top"]):
        if bands and w["top"] <= bands[-1][1]:
            bands[-1][1] = max(bands[-1][1], w["bottom"])
        else:
            bands.append([w["top"], w["bottom"]])

    sections = [[[] for _ in range(len(gutters) + 1)] for _ in range(len(bands) + 1)]
    band_words: list[list[dict]] = [[] for _ in bands]
    for w in words:
        cy = (w["top"] + w["bottom"]) / 2
        band = next((i for i, (top, bottom) in enumerate(bands) if top <= cy <= bottom), None)
        if band is not None:
            band_words[band].append(w)
        else:
            above = sum(1 for _, bottom in bands if bottom < cy)
            sections[above][bisect.bisect(mids, (w["x0"] + w["x1"]) / 2)].append(w)

    chunks = []
    for i, columns in enumerate(sections):
        chunks.extend(columns)
        if i < len(bands):
            chunks.append(band_words[i])
    return [c for c in chunks if c]


def _words_to_lines(words: list[dict]) -> list[Line]:
    """Group words (in reading order) into lines by vertical overlap and left-to-right progress."""
    lines: list[list[dict]] = []
    for w in words:
        if lines:
            prev = lines[-1][-1]
            h = min(prev["bottom"] - prev["top"], w["bottom"] - w["top"]) or 1.0
            overlap = min(prev["bottom"], w["bottom"]) - max(prev["top"], w["top"])
            if overlap >= 0.5 * h and w["x0"] >= prev["x1"] - 1.0:
                lines[-1].append(w)
                continue
        lines.append([w])

    return [
        Line(
            text=" ".join(w["text"] for w in ws),
            size=_median([float(w["size"]) for w in ws]),
            conf=100.0,
            left=min(w["x0"] for w in ws), top=min(w["top"] for w in ws),
            right=max(w["x1"] for w in ws), bottom=max(w["bottom"] for w in ws),
            bold=all(_BOLD_RE.search(w.get("fontname") or "") for w in ws),
        )
        for ws in lines
    ]


def _refine_paragraphs(groups: list[list[Line]], use_font_changes: bool) -> list[Para]:
    """Split line groups into paragraphs at large vertical gaps and after short sentence-final lines.

    Tesseract's own paragraphs often swallow the next paragraph when spacing is tight; a text layer
    has no paragraphs at all. Font size/weight changes are only trusted for real text layers.
    """
    all_lines = [l for g in groups for l in g]
    if not all_lines:
        return []
    steps = [b.top - a.top for g in groups for a, b in zip(g, g[1:]) if b.top > a.top]
    typical_step = _median(steps, default=_median([l.size for l in all_lines]) * 1.2)
    typical_width = _median([l.right - l.left for l in all_lines])
    out: list[Para] = []
    for lines in groups:
        paras = [[lines[0]]]
        for prev, line in zip(lines, lines[1:]):
            size = max(prev.size, line.size)
            step = line.top - prev.top
            new = (
                step <= 0  # moved up: new column or out-of-flow text
                or step > max(1.35 * typical_step, 1.25 * size)
                or (re.search(r"[.!?:]$", prev.text) and (prev.right - prev.left) < 0.7 * typical_width)
                or (use_font_changes and (abs(line.size - prev.size) > 0.15 * size or prev.bold != line.bold))
            )
            if new:
                paras.append([line])
            else:
                paras[-1].append(line)
        out.extend(Para(p) for p in paras)
    return out


# --------------------------------------------------------------------------------------
# Markdown reconstruction
# --------------------------------------------------------------------------------------

PAGE_LABEL_RE = re.compile(r"^(?:page|pg\.?|p\.)\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)
PAGE_FRACTION_RE = re.compile(r"^\d{1,4}\s*(?:of|/)\s*\d{1,4}$", re.I)
BARE_PAGE_RE = re.compile(r"^[-–—(\[]?\s*(?:\d{1,4}|[ivxlc]{1,6})\s*[-–—)\]]?$", re.I)
EDGE_PAGE_RE = re.compile(r"^page\s+(\d{1,4})(?:\s+of\s+\d{1,4})?\s+|\s+page\s+(\d{1,4})(?:\s+of\s+\d{1,4})?$", re.I)
ORDERED_RE = re.compile(r"^(\d{1,3})[.)]\s+(\S.*)$")
# Bullet glyphs, plus common OCR misreadings of "•" (¢ © ° « and a lone e/o before a capital).
BULLET_RE = re.compile(r"^(?:[•●▪◦·∙‣○■□►▶➢✓»¢©°«]\s*|[-–—*+]\s+|[eo]\s+(?=[A-Z]))(\S.*)$")
MARKER_ONLY_RE = re.compile(r"^(?:\d{1,3}[.)]|[•●▪◦·∙‣○■□►▶➢✓»¢©°«*+-])$")
TERMINAL_RE = re.compile(r"[.!?:;]['\"”’)\]]?$")
MARGIN = 0.1  # top/bottom fraction of the page treated as header/footer zone


def _in_margin(line: Line, page: PageContent) -> bool:
    return line.bottom < page.height * MARGIN or line.top > page.height * (1 - MARGIN)


def _is_page_number(line: Line, page: PageContent) -> bool:
    t = line.text.strip()
    if PAGE_LABEL_RE.match(t):
        return True
    return _in_margin(line, page) and bool(PAGE_FRACTION_RE.match(t) or BARE_PAGE_RE.match(t))


def _strip_page_label(line: Line, page: PageContent) -> None:
    """Remove a 'Page 12' footer that got glued to the start or end of a body line."""
    m = EDGE_PAGE_RE.search(line.text)
    if not m:
        return
    n = int(m.group(1) or m.group(2))
    if _in_margin(line, page) or abs(n - page.number) <= 2:
        line.text = (line.text[:m.start()] + " " + line.text[m.end():]).strip()


def _is_junk(line: Line, source: str, alone: bool) -> bool:
    """Figure labels, axis ticks and OCR noise: mostly non-alphabetic, tiny, or low confidence."""
    s = re.sub(r"\s+", "", line.text)
    if not s:
        return True
    if MARKER_ONLY_RE.match(s):
        return False  # a list number split from its text; re-attached later
    alnum = sum(c.isalnum() for c in s)
    alpha = sum(c.isalpha() for c in s)
    if alnum / len(s) < 0.5:
        return True
    if source != "ocr":
        return False
    if line.conf < 30 or (line.conf < 55 and alpha / len(s) < 0.6):
        return True
    if alone and alnum <= 2:
        return True
    tokens = line.text.split()
    if len(tokens) >= 3 and line.conf < 75 and sum(len(t) <= 2 for t in tokens) / len(tokens) > 0.7:
        return True
    return False


def _drop_running_headers(pages: list[PageContent]) -> None:
    """Remove header/footer lines that repeat (ignoring digits) in the margins of many pages."""
    if len(pages) < 3:
        return

    def key(line):
        return re.sub(r"\d+", "#", line.text.lower()).strip()

    counts = Counter()
    for page in pages:
        counts.update({key(l) for p in page.paras for l in p.lines if _in_margin(l, page) and len(key(l)) >= 4})
    threshold = max(3, 0.4 * len(pages))
    repeated = {k for k, c in counts.items() if c >= threshold}
    if not repeated:
        return
    for page in pages:
        for para in page.paras:
            para.lines = [l for l in para.lines if not (_in_margin(l, page) and key(l) in repeated)]


def _clean_page(page: PageContent) -> None:
    for para in page.paras:
        kept = []
        for line in para.lines:
            if _is_page_number(line, page):
                continue
            _strip_page_label(line, page)
            if _is_junk(line, page.source, alone=len(para.lines) == 1):
                continue
            kept.append(line)
        para.lines = kept
    page.paras = [p for p in page.paras if p.lines]

    # Re-attach list numbers that OCR split into their own paragraph ("3." then "Item text").
    merged: list[Para] = []
    for para in page.paras:
        prev = merged[-1] if merged else None
        if prev and len(prev.lines) == 1 and MARKER_ONLY_RE.match(prev.lines[0].text.strip()):
            first = para.lines[0]
            first.text = f"{prev.lines[0].text.strip()} {first.text}"
            first.left = min(first.left, prev.lines[0].left)
            merged[-1] = para
        else:
            merged.append(para)
    if merged and len(merged[-1].lines) == 1 and MARKER_ONLY_RE.match(merged[-1].lines[0].text.strip()):
        merged.pop()
    page.paras = merged


WORDLIST_PATH = "/usr/share/dict/words"

# Used only when WORDLIST_PATH is missing (Windows/macOS without it, or no `wamerican` package): common
# words that often get hyphenated at a line break. Anything not listed keeps its hyphen - the safe default.
FALLBACK_WORDS = frozenset("""
about above absolute according account achieve achievement across activity actually addition additional
address administration advantage against agreement algorithm alternative although analysis analytical
another anything application applications approach appropriate approximately architecture argument
around article assessment assistance associated association assumption attention available average
background because become before behavior behaviour believe benefit between beyond building business
calculation capability capacity category certain challenge change character characteristic chemical
children clinical collection combination commercial committee common communication community company
comparison complete component components computer concentration concept condition conditions conference
configuration consequence consider considerable consideration consistent constant construction content
context continue contribution control conventional coordination corresponding country current currently
customer database decision definition delivery demonstrate department dependent describe description
design determine development difference different difficult dimension direction discussion distribution
document during economic education effective efficiency effort element emergency employee employment
encourage energy engineering environment environmental equipment especially establish estimate
evaluation everything evidence example excellent exchange existing experience experiment experimental
explanation expression facilities facility factor following foundation framework frequency function
functional fundamental further general generally government graduate greater growth guidance hardware
health however hypothesis identification identify implementation importance important improvement
include including increase increased independent indicate individual industrial industry influence
information infrastructure initial institution instruction instrument insurance integration
intelligence interest interesting international interpretation intervention introduction investigation
investment involved knowledge laboratory language learning legislation literature location machine
maintenance management manufacturing material materials mathematical measurement mechanism medical
medicine member membership message method methodology minister minimum moreover movement multiple
national natural necessary negative network nevertheless normally nothing number objective observation
obtained occupation operation operational opinion opportunity optimization organisation organization
original outcome output overall participant participants particular particularly partnership patient
patients people percentage performance period permanent personal perspective phenomenon physical
planning political population position positive possibility possible potential practical practice
precisely preparation presence presentation president pressure prevention previous previously primary
principle priority probability problem procedure procurement process processing produce product
production productivity professional profile program programme progress project property proportion
proposal protection provide provided provision publication purpose qualitative quality quantitative
quantity question reasonable recommendation reduction reference regarding regional registration
regulation relationship relative relatively relevant replacement report representation representative
requirement requirements research resource resources response responsibility responsible restaurant
result results revenue schedule scheduled science scientific secretary section security selection
separate sequence service services significance significant significantly similar simulation situation
software solution something specific specifically standard statement statistical statistics strategy
strength structure student students subsequent substantial success successful sufficient suggest
summary supplement support surface survey system systems technical technique technology temperature
therefore thousand throughout together tradition traditional training transaction transformation
transition transport transportation treatment understanding university unfortunately variable variation
various vehicle version whatever whether without working
""".split())


@functools.lru_cache(maxsize=None)
def _load_wordlist(path: str) -> tuple[frozenset[str], str]:
    """Read a one-word-per-line list into a lowercase set (cached per path), else use the fallback."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            words = frozenset(w.strip().lower() for w in fh if w.strip())
        if words:
            return words, path
    except OSError:
        pass
    return FALLBACK_WORDS, "built-in fallback list"


def dictionary() -> tuple[frozenset[str], str]:
    """(set of known words, where it came from). Read once; later calls hit the cache."""
    return _load_wordlist(WORDLIST_PATH)


dictionary()  # load at startup rather than on the first line break

_EDGE_PUNCT_RE = re.compile(r"^\W+|\W+$")


def _is_wrapped_word(before: str, after: str) -> bool:
    """Should a line-end hyphen-minus between `before` and `after` be dropped?

    Only when the joined form is a dictionary word and the hyphenated form is not ("informa-" +
    "tion"). Real compounds and domain terms ("T-cell", "CD4-positive") are not in the dictionary,
    so they keep the hyphen: leaving a stray hyphen is better than corrupting a term.
    """
    words, _ = dictionary()
    a, b = _EDGE_PUNCT_RE.sub("", before).lower(), _EDGE_PUNCT_RE.sub("", after).lower()
    return bool(a and b) and a + b in words and f"{a}-{b}" not in words


def join_lines(texts: list[str]) -> str:
    """Undo soft wraps: join lines with spaces and resolve hyphens left at a line break."""
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
        elif out.endswith("\u00ad"):
            out = out[:-1] + t  # soft hyphen: always a line-break artefact, never part of the word
        elif out.endswith("-") and len(out) > 1 and not out[-2].isspace() and _has_alnum(out[:-1].split()[-1]):
            # Hyphen-minus after a word: a wrapped word ("informa-tion") or a real hyphen ("T-cell")?
            # Ask the dictionary; either way the two halves are joined without a space.
            before, after = out[:-1].split()[-1], t.split()[0]
            out = out[:-1] + t if _is_wrapped_word(before, after) else out + t
        else:
            out += " " + t
    return out


def _split_size_jumps(para: Para, body: float, min_heading_rel: float) -> list[list[Line]]:
    """Separate heading lines that OCR grouped into the same paragraph as body text."""
    groups = [[para.lines[0]]]
    for prev, line in zip(para.lines, para.lines[1:]):
        hi, lo = max(prev.size, line.size), max(min(prev.size, line.size), 1e-6)
        if hi / body >= min_heading_rel and hi / lo > 1.2:
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


def _para_to_blocks(lines: list[Line], page: int, body: float, opts_ratio: float, source: str) -> list[Block]:
    text = join_lines([l.text for l in lines])
    size = _median([l.size for l in lines])
    rel = size / body if body else 1.0
    n_words = len(text.split())
    geo = dict(page=page, rel=rel, top=lines[0].top, bottom=lines[-1].bottom, size=size)
    alpha = sum(c.isalpha() for c in text)

    is_heading = alpha >= 2 and (
        (rel >= opts_ratio and n_words <= 25 and len(lines) <= 4)
        # a short, standalone, noticeably larger line is a minor heading even below the main threshold
        or (rel >= 1.2 and len(lines) == 1 and n_words <= 10 and not re.search(r"[.,;]$", text))
        # text layer: a short bold line on its own
        or (source == "text" and all(l.bold for l in lines) and len(lines) == 1 and n_words <= 12
            and rel >= 0.95 and not text.endswith("."))
    )
    if is_heading:
        return [Block("h", text=text.rstrip("#").strip(), **geo)]

    blocks: list[Block] = []
    buf: list[str] = []
    lst: Block | None = None

    def flush():
        if buf:
            blocks.append(Block("p", text=join_lines(buf), **geo))
            buf.clear()

    for line in lines:
        t = line.text.strip()
        m = ORDERED_RE.match(t)
        if m:
            n = int(m.group(1))
            continues = lst is not None and lst.kind == "ol" and n == lst.items[-1][0] + 1
            starts = (lst is None and (not buf or n == 1)) or (lst is not None and lst.kind == "ul" and n == 1)
            if continues or starts:
                if starts:
                    flush()
                    lst = Block("ol", **geo)
                    blocks.append(lst)
                lst.items.append([n, [m.group(2)]])
                continue
        b = BULLET_RE.match(t)
        if b and (lst is None or lst.kind == "ul"):
            if lst is None:
                flush()
                lst = Block("ul", **geo)
                blocks.append(lst)
            lst.items.append(["-", [b.group(1)]])
            continue
        if lst is not None:
            lst.items[-1][1].append(t)  # wrapped continuation of the current item
        else:
            buf.append(t)
    flush()
    for blk in blocks:
        if blk.kind == "ol":
            blk.items = _split_merged_items(blk.items)
    return blocks


def _split_merged_items(items: list[list]) -> list[list]:
    """'1. Apples 2. Pears' (two items OCR'd onto one line) -> two items."""
    out = []
    for n, parts in items:
        text = join_lines(parts)
        while True:
            m = re.search(rf"\s{n + 1}[.)]\s+(?=[A-Z0-9\"'“‘(])", text)
            if not m:
                break
            out.append([n, [text[:m.start()].strip()]])
            n, text = n + 1, text[m.end():]
        out.append([n, [text]])
    return out


def _assign_heading_levels(blocks: list[Block]) -> None:
    """Cluster heading sizes into tiers: largest tier -> H1, next -> H2, the rest -> H3."""
    headings = [b for b in blocks if b.kind == "h"]
    if not headings:
        return
    tiers: list[float] = []  # the largest size of each tier, descending
    for rel in sorted({round(b.rel, 3) for b in headings}, reverse=True):
        if not tiers or rel < tiers[-1] * 0.88:
            tiers.append(rel)
    for b in headings:
        idx = next((i for i, t in enumerate(tiers) if b.rel >= t * 0.88 - 1e-9), len(tiers) - 1)
        b.level = min(idx + 1, 3)
    # Bold body-size headings (text layer) sit below the size-based tiers.
    size_tiers = [t for t in tiers if t >= 1.2]
    for b in headings:
        if b.rel < 1.2:
            b.level = min(max(len(size_tiers) + 1, 2), 3)


def _merge_blocks(blocks: list[Block]) -> list[Block]:
    """Merge wrapped heading fragments and paragraphs/list items that continue onto the next page."""
    out: list[Block] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev.kind == "h" and b.kind == "h" and prev.page == b.page and prev.level == b.level \
                and not TERMINAL_RE.search(prev.text) and 0 <= b.top - prev.bottom < 1.2 * max(prev.size, b.size):
            prev.text = join_lines([prev.text, b.text])
            prev.bottom = b.bottom
            continue
        if prev and b.kind == "p" and b.page == prev.page + 1 and b.text[:1].islower():
            if prev.kind == "p" and not TERMINAL_RE.search(prev.text):
                prev.text = join_lines([prev.text, b.text])
                prev.page = b.page
                continue
            if prev.kind in ("ol", "ul") and not TERMINAL_RE.search(join_lines(prev.items[-1][1])):
                prev.items[-1][1].append(b.text)
                prev.page = b.page
                continue
        out.append(b)
    return out


def _escape_start(text: str) -> str:
    """Stop plain paragraph text from being parsed as a heading/list/quote."""
    if re.match(r"^\d{1,9}[.)](\s|$)", text):
        return re.sub(r"^(\d+)([.)])", r"\1\\\2", text)
    if re.match(r"^(#{1,6}|[>+*-])(\s|$)", text):
        return "\\" + text
    return text


def render_markdown(blocks: list[Block]) -> str:
    parts: list[str] = []
    prev: Block | None = None
    for b in blocks:
        if b.kind == "h":
            s = f"{'#' * b.level} {b.text}"
        elif b.kind == "ol":
            s = "\n".join(f"{n}. {join_lines(parts_)}" for n, parts_ in b.items)
        elif b.kind == "ul":
            s = "\n".join(f"- {join_lines(parts_)}" for _, parts_ in b.items)
        else:
            s = _escape_start(b.text)
        if prev is not None and prev.kind == b.kind and b.kind in ("ol", "ul"):
            parts[-1] += "\n" + s  # keep consecutive items in one tight list
        else:
            parts.append(s)
        prev = b
    return ("\n\n".join(p for p in parts if p.strip()).strip() + "\n") if parts else ""


def build_markdown(pages: list[PageContent], heading_ratio: float = 1.5) -> str:
    """Turn extracted page geometry into structured Markdown."""
    pages = sorted(pages, key=lambda p: p.number)
    body = {}
    for src in ("ocr", "text"):
        sizes = [s for p in pages if p.source == src for s in p.word_sizes]
        if sizes:
            body[src] = statistics.median(sizes)

    _drop_running_headers(pages)
    for page in pages:
        _clean_page(page)

    blocks: list[Block] = []
    for page in pages:
        b_size = body.get(page.source, 1.0)
        for para in page.paras:
            for group in _split_size_jumps(para, b_size, min(1.2, heading_ratio)):
                blocks.extend(_para_to_blocks(group, page.number, b_size, heading_ratio, page.source))
    _assign_heading_levels(blocks)
    return render_markdown(_merge_blocks(blocks))


# --------------------------------------------------------------------------------------
# Word documents
# --------------------------------------------------------------------------------------

def soffice_convert(src: Path, out_dir: Path, fmt: str, soffice: str, timeout: int = 300) -> Path:
    """Convert with LibreOffice headless, using a throwaway profile so parallel runs don't collide."""
    profile = Path(tempfile.mkdtemp(prefix="lo_profile_", dir=out_dir))
    cmd = [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
           "--convert-to", fmt, "--outdir", str(out_dir), str(src)]
    try:
        proc = _run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PipelineError(f"LibreOffice timed out after {timeout}s converting {src.name} to {fmt}.")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    out = out_dir / f"{src.stem}.{fmt.split(':')[0]}"
    if not out.exists():
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        raise PipelineError(f"LibreOffice could not convert {src.name} to {fmt}. {detail}")
    return out


def _numbering_formats(doc) -> dict[str, dict[int, str]]:
    """numId -> {ilvl: numFmt} from word/numbering.xml (e.g. 'decimal', 'bullet')."""
    from docx.oxml.ns import qn
    try:
        numbering = doc.part.numbering_part.element
    except Exception:
        return {}
    abstract = {}
    for an in numbering.findall(qn("w:abstractNum")):
        levels = {}
        for lvl in an.findall(qn("w:lvl")):
            fmt = lvl.find(qn("w:numFmt"))
            levels[int(lvl.get(qn("w:ilvl")))] = fmt.get(qn("w:val")) if fmt is not None else "decimal"
        abstract[an.get(qn("w:abstractNumId"))] = levels
    fmts = {}
    for num in numbering.findall(qn("w:num")):
        ref = num.find(qn("w:abstractNumId"))
        if ref is not None:
            fmts[num.get(qn("w:numId"))] = abstract.get(ref.get(qn("w:val")), {})
    return fmts


def _num_props(para) -> tuple[str, int] | None:
    """(numId, ilvl) for a list paragraph, looking at the paragraph and then its style."""
    from docx.oxml.ns import qn
    for el in (para._p, getattr(para.style, "element", None)):
        ppr = el.find(qn("w:pPr")) if el is not None else None
        num_pr = ppr.find(qn("w:numPr")) if ppr is not None else None
        if num_pr is not None:
            num_id = num_pr.find(qn("w:numId"))
            ilvl = num_pr.find(qn("w:ilvl"))
            if num_id is not None and num_id.get(qn("w:val")) != "0":
                return num_id.get(qn("w:val")), int(ilvl.get(qn("w:val"))) if ilvl is not None else 0
    return None


def _runs_to_markdown(para) -> str:
    from docx.text.hyperlink import Hyperlink
    segments: list[list] = []  # [text, bold, italic, url]
    for item in para.iter_inner_content():
        if isinstance(item, Hyperlink):
            seg = [item.text, False, False, getattr(item, "url", "") or ""]
        else:
            seg = [item.text, bool(item.bold), bool(item.italic), ""]
        seg[0] = seg[0].replace("\t", " ").replace("\n", " ")
        if segments and segments[-1][1:] == seg[1:]:
            segments[-1][0] += seg[0]
        elif seg[0]:
            segments.append(seg)
    out = []
    for text, bold, italic, url in segments:
        core = text.strip()
        if not core:
            out.append(text)
            continue
        lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
        if url:
            core = f"[{core}]({url})"
        mark = "**" if bold else ""
        mark += "*" if italic else ""
        out.append(f"{lead}{mark}{core}{mark[::-1]}{trail}")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _table_to_markdown(table) -> str:
    rows = []
    for row in table.rows:
        rows.append([c.text.replace("|", "\\|").replace("\n", "<br>").strip() for c in row.cells])
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def docx_to_markdown(path: Path) -> str:
    import docx
    from docx.table import Table
    try:
        doc = docx.Document(str(path))
    except Exception as exc:
        raise PipelineError(f"Could not open the Word document: {exc}") from exc
    formats = _numbering_formats(doc)
    parts: list[str] = []
    counters: dict[tuple, int] = {}
    in_list = False
    # With a Title paragraph present, Title is the H1 and "Heading 1" moves down to H2.
    shift = int(any(p.style is not None and p.style.name == "Title" for p in doc.paragraphs))

    for item in doc.iter_inner_content():
        if isinstance(item, Table):
            parts.append(_table_to_markdown(item))
            in_list = False
            continue
        style = item.style.name if item.style is not None else ""
        plain = re.sub(r"\s+", " ", item.text).strip()
        if not plain:
            continue
        heading = re.match(r"Heading (\d)", style)
        if style == "Title":
            parts.append(f"# {plain}")
        elif heading:
            parts.append(f"{'#' * min(int(heading.group(1)) + shift, 6)} {plain}")
        else:
            num = _num_props(item)
            is_list = num is not None or style.startswith("List")
            if is_list:
                num_id, ilvl = num if num else (style, 0)
                fmt = formats.get(num_id, {}).get(ilvl, "decimal" if "Number" in style else "bullet")
                if not in_list:
                    counters.clear()
                counters = {k: v for k, v in counters.items() if not (k[0] == num_id and k[1] > ilvl)}
                counters[(num_id, ilvl)] = counters.get((num_id, ilvl), 0) + 1
                marker = f"{counters[(num_id, ilvl)]}." if fmt not in ("bullet", "none") else "-"
                line = f"{'   ' * ilvl}{marker} {_runs_to_markdown(item)}"
                if in_list:
                    parts[-1] += "\n" + line
                else:
                    parts.append(line)
                in_list = True
                continue
            parts.append(_escape_start(_runs_to_markdown(item)))
        in_list = False
    return "\n\n".join(p for p in parts if p).strip() + "\n"


# --------------------------------------------------------------------------------------
# Searchable PDF (OCRmyPDF)
# --------------------------------------------------------------------------------------

OCRMYPDF_EXIT = {
    1: "bad arguments", 2: "the input file is not a valid PDF", 3: "a required program is missing "
    "(Tesseract or Ghostscript)", 4: "the output PDF failed validation", 5: "a file could not be read or "
    "written", 7: "an OCR subprocess failed", 8: "the PDF is encrypted", 9: "invalid configuration "
    "(is the Tesseract language pack installed?)", 15: "an unexpected internal error",
}


def start_ocrmypdf(src: Path, dst: Path, opts: Options, jobs: int, log: Path) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "ocrmypdf", "--output-type", "pdf", "-l", opts.lang, "--jobs", str(jobs),
           "--force-ocr" if opts.force_ocr else "--skip-text", str(src), str(dst)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(log, "w", encoding="utf-8"))


def finish_ocrmypdf(proc: subprocess.Popen, dst: Path, log: Path, progress: ProgressFn,
                    start_frac: float) -> bytes:
    t0 = time.time()
    while proc.poll() is None:
        progress(min(0.99, start_frac + (1 - start_frac) * (1 - 1 / (1 + (time.time() - t0) / 30))),
                 f"Adding the OCR text layer with OCRmyPDF… ({int(time.time() - t0)}s)")
        time.sleep(0.5)
    if proc.stderr:
        proc.stderr.close()
    if proc.returncode in (0, 10) and dst.exists():
        return dst.read_bytes()
    if proc.returncode < 0 or proc.returncode == 137:  # SIGKILL: almost always the out-of-memory killer
        raise PipelineError("OCRmyPDF was killed by the system, most likely because it ran out of memory. "
                            "Turn on 'Force sequential mode' or lower the OCR DPI.")
    reason = OCRMYPDF_EXIT.get(proc.returncode, f"exit code {proc.returncode}")
    tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:] if log.exists() else []
    raise PipelineError(f"OCRmyPDF failed: {reason}.\n" + "\n".join(tail))


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------

def _noop(_frac: float, _msg: str) -> None:
    pass


def _worker_split() -> tuple[int, int]:
    cpus = os.cpu_count() or 2
    ocrmypdf_jobs = max(1, cpus // 2)
    return max(1, cpus - ocrmypdf_jobs), ocrmypdf_jobs


def choose_schedule(opts: Options) -> tuple[str, str]:
    """Decide whether Tesseract (Markdown) and OCRmyPDF (PDF) may run at the same time.

    Returns ("concurrent" | "sequential", reason). Concurrent only when psutil reports at least
    opts.min_concurrent_mem_mb of free RAM; anything uncertain falls back to the safe sequential mode.
    """
    if opts.force_sequential:
        return "sequential", "forced by setting"
    if psutil is None:
        return "sequential", "psutil is not installed, so free memory is unknown"
    try:
        available_mb = psutil.virtual_memory().available / 2**20
    except Exception:
        return "sequential", "free memory could not be read"
    if available_mb >= opts.min_concurrent_mem_mb:
        return "concurrent", f"{available_mb:,.0f} MB free (threshold {opts.min_concurrent_mem_mb:,} MB)"
    return "sequential", f"only {available_mb:,.0f} MB free (threshold {opts.min_concurrent_mem_mb:,} MB)"


def extract_pdf_pages(src: Path, info: PdfInfo, opts: Options, tools: dict, progress: ProgressFn,
                      warnings: list[str], span: tuple[float, float] = (0.0, 0.9),
                      workers: int | None = None) -> list[PageContent]:
    """Text layer / OCR for every page. `workers` = OCR threads (default: our share of _worker_split)."""
    lo, hi = span
    pages: dict[int, PageContent] = {}
    to_ocr: list[int] = []

    if opts.force_ocr or info.image_only:
        to_ocr = list(range(1, info.pages + 1))
    else:
        import pdfplumber
        text_hi = lo + (hi - lo) * 0.3
        with pdfplumber.open(str(src)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                progress(lo + (text_hi - lo) * i / info.pages, f"Reading text layer: page {i} of {info.pages}")
                try:
                    content = text_layer_page(page, i)
                except Exception as exc:
                    content = None
                    warnings.append(f"Page {i}: text extraction failed ({exc}); trying OCR.")
                chars = sum(c.isalnum() for p in (content.paras if content else []) for l in p.lines for c in l.text)
                if content is None or chars < MIN_TEXT_CHARS_PER_PAGE:
                    to_ocr.append(i)
                else:
                    pages[i] = content
                (getattr(page, "close", None) or page.flush_cache)()
        lo = text_hi

    if to_ocr:
        missing = [t for t in ("tesseract", "pdftoppm") if not tools.get(t)]
        if missing:
            msg = " ".join(TOOL_HINTS[t] for t in missing)
            if not pages:
                raise PipelineError(f"This PDF needs OCR but it can't run: {msg}")
            warnings.append(f"{len(to_ocr)} page(s) without a text layer were skipped: {msg}")
            to_ocr = []

    if to_ocr:
        workers = workers or _worker_split()[0]
        done = 0
        progress(lo, f"OCR: rendering at {opts.dpi} DPI and recognising page 1 of {len(to_ocr)}…")
        with ThreadPoolExecutor(max_workers=min(workers, len(to_ocr))) as pool:
            futures = {pool.submit(ocr_page, src, n, info.page_sizes[n - 1], opts): n for n in to_ocr}
            try:
                for fut in as_completed(futures):
                    n = futures[fut]
                    try:
                        pages[n] = fut.result()
                    except Exception as exc:
                        if "tesseract" in type(exc).__name__.lower() and "language" in str(exc).lower():
                            raise PipelineError(f"Tesseract language data missing for '{opts.lang}': {exc}")
                        warnings.append(f"Page {n}: OCR failed ({str(exc).strip()[:200]}).")
                    done += 1
                    progress(lo + (hi - lo) * done / len(to_ocr), f"OCR: page {done} of {len(to_ocr)} done")
            except BaseException:
                for f in futures:
                    f.cancel()
                raise
    return [pages[n] for n in sorted(pages)]


def convert(src: Path, workdir: Path, opts: Options, progress: ProgressFn = _noop,
            on_markdown: Callable[[str], None] | None = None) -> Result:
    """Convert a PDF/DOCX/DOC into Markdown + searchable PDF.

    `on_markdown` (PDF input) is called with the finished Markdown before the searchable-PDF stage is
    awaited, so a caller can keep it even if that last stage dies.
    """
    t0 = time.time()
    ext = src.suffix.lower()
    if ext == ".pdf":
        result = _convert_pdf(src, workdir, opts, progress, on_markdown)
    elif ext in (".docx", ".doc"):
        result = _convert_word(src, workdir, opts, progress)
    else:
        raise PipelineError(f"Unsupported file type '{ext}'. Upload a .pdf, .docx or .doc file.")
    result.stats["seconds"] = round(time.time() - t0, 1)
    progress(1.0, "Done")
    return result


def _make_searchable(pdf: Path, workdir: Path, opts: Options, tools: dict, warnings: list[str],
                     jobs: int | None = None):
    """Start OCRmyPDF in the background; returns (proc, output, log) or None.

    `jobs` = OCRmyPDF worker processes (default: its share of _worker_split).
    """
    if not opts.make_pdf:
        return None
    missing = [t for t in ("ocrmypdf", "tesseract", "ghostscript") if not tools.get(t)]
    if missing:
        warnings.append("Searchable PDF not created: " + " ".join(TOOL_HINTS[t] for t in missing))
        return None
    dst, log = workdir / "searchable.pdf", workdir / "ocrmypdf.log"
    try:
        return start_ocrmypdf(pdf, dst, opts, jobs or _worker_split()[1], log), dst, log
    except Exception as exc:  # e.g. OSError/MemoryError when forking: lose the PDF, never the Markdown
        warnings.append(f"Searchable PDF not created: OCRmyPDF could not be started ({exc}).")
        return None


def _collect_pdf(job, fallback: Path | None, progress: ProgressFn, start: float, warnings: list[str]):
    if job is None:
        return fallback.read_bytes() if fallback else None
    proc, dst, log = job
    try:
        return finish_ocrmypdf(proc, dst, log, progress, start)
    except Exception as exc:  # any PDF-stage failure becomes a warning; the Markdown is already done
        msg = str(exc) if isinstance(exc, PipelineError) else f"OCRmyPDF failed: {exc}"
        if fallback:
            warnings.append(f"{msg}\nFalling back to a PDF that already has a text layer.")
            return fallback.read_bytes()
        warnings.append(msg)
        return None


def _kill(job) -> None:
    if job and job[0].poll() is None:
        job[0].kill()
        job[0].wait()


def _convert_pdf(src: Path, workdir: Path, opts: Options, progress: ProgressFn,
                 on_markdown: Callable[[str], None] | None = None) -> Result:
    tools = find_tools()
    warnings: list[str] = []
    progress(0.01, "Inspecting PDF…")
    info = analyze_pdf(src, tools)
    ocr_all = opts.force_ocr or info.image_only
    if ocr_all:
        missing = [t for t in ("tesseract", "pdftoppm") if not tools.get(t)]
        if missing:
            raise PipelineError("This PDF needs OCR but it can't run: " + " ".join(TOOL_HINTS[t] for t in missing))

    # A PDF that already had text is itself searchable, so it is an acceptable fallback.
    fallback = src if opts.make_pdf and not info.image_only else None
    mode, reason = choose_schedule(opts)
    log.info("Schedule: %s (%s)", mode, reason)
    progress(0.02, f"Schedule: {mode} ({reason})")

    def markdown_stage(span: tuple[float, float], workers: int | None) -> tuple[list[PageContent], str]:
        pages = extract_pdf_pages(src, info, opts, tools, progress, warnings, span=span, workers=workers)
        progress(span[1], "Reconstructing Markdown structure…")
        md = build_markdown(pages, opts.heading_ratio)
        if on_markdown:
            on_markdown(md)
        return pages, md

    if mode == "concurrent":
        # Plenty of RAM: OCRmyPDF builds the PDF in the background while we extract; cores are shared.
        job = _make_searchable(src, workdir, opts, tools, warnings)
        try:
            pages, markdown = markdown_stage((0.0, 0.9), None)
            pdf_bytes = _collect_pdf(job, fallback, progress, 0.92, warnings)
        finally:
            _kill(job)
    else:
        # Low RAM: one heavy stage at a time, each with every core. Markdown extraction ALWAYS runs
        # first and to completion: it is the core output of the app, so if OCRmyPDF then exhausts memory
        # and is killed at the very end, the user still has the finished Markdown. Never start OCRmyPDF
        # before this point in sequential mode.
        cpus = os.cpu_count() or 1
        pages, markdown = markdown_stage((0.0, 0.5 if opts.make_pdf else 0.9), cpus)
        job = _make_searchable(src, workdir, opts, tools, warnings, jobs=cpus)
        try:
            pdf_bytes = _collect_pdf(job, fallback, progress, 0.55, warnings)
        finally:
            _kill(job)

    ocr_pages = [p for p in pages if p.source == "ocr"]
    confs = [p.conf for p in ocr_pages if p.conf is not None]
    if not markdown.strip():
        warnings.append("No text could be recognised in this document.")
    return Result(markdown, pdf_bytes, warnings, {
        "pages": info.pages,
        "text layer": "none (image-only)" if info.image_only else "present",
        "OCR pages": len(ocr_pages),
        "mean OCR confidence": round(sum(confs) / len(confs), 1) if confs else None,
        "schedule": mode,
    })


def _convert_word(src: Path, workdir: Path, opts: Options, progress: ProgressFn) -> Result:
    tools = find_tools()
    warnings: list[str] = []
    soffice = tools.get("soffice")
    docx_path = src
    if src.suffix.lower() == ".doc":
        if not soffice:
            raise PipelineError("Reading legacy .doc files requires LibreOffice. " + TOOL_HINTS["soffice"])
        progress(0.05, "Converting .doc to .docx with LibreOffice…")
        docx_path = soffice_convert(src, workdir, "docx", soffice)

    progress(0.15, "Extracting text and structure from the Word document…")
    markdown = docx_to_markdown(docx_path)

    pdf_bytes = None
    stats = {"text layer": "native (Word)"}
    if not soffice:
        warnings.append("PDF output not created: " + TOOL_HINTS["soffice"])
    elif opts.make_pdf or sum(c.isalnum() for c in markdown) < 20:
        progress(0.3, "Converting to PDF with LibreOffice…")
        pdf = soffice_convert(docx_path, workdir, "pdf", soffice)
        stats["pages"] = analyze_pdf(pdf, tools).pages

        if sum(c.isalnum() for c in markdown) < 20:
            # Word file that only contains scanned images: OCR the rendered PDF instead.
            warnings.append("The Word file contains little or no text; the Markdown was produced by OCR.")
            info = analyze_pdf(pdf, tools)
            pages = extract_pdf_pages(pdf, info, Options(**{**opts.__dict__, "force_ocr": True}), tools,
                                      progress, warnings, span=(0.35, 0.85))
            markdown = build_markdown(pages, opts.heading_ratio)
            stats["OCR pages"] = len(pages)

        if opts.make_pdf:
            progress(0.88, "Adding OCR text layer to any image-only pages…")
            job = _make_searchable(pdf, workdir, Options(**{**opts.__dict__, "force_ocr": False}), tools, [])
            try:
                pdf_bytes = _collect_pdf(job, pdf, progress, 0.88, warnings)
            finally:
                _kill(job)
    return Result(markdown, pdf_bytes, warnings, stats)


# --------------------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------------------

PREVIEW_CHARS = 20_000


def _safe_name(name: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(name))
    return (re.sub(r"[^\w.-]+", "_", stem).strip("._") or "document") + ext.lower()


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Searchable PDF Maker", page_icon="📄", layout="wide")
    st.title("📄 Searchable PDF Maker")
    st.caption("Upload a PDF (including scanned, image-only PDFs) or a Word document. "
               "Get back clean Markdown and a searchable PDF with a selectable text layer.")

    tools = find_tools()
    langs = st.cache_data(ttl=3600, show_spinner=False)(tesseract_languages)()

    with st.sidebar:
        st.header("Options")
        dpi = st.number_input("OCR DPI", min_value=100, max_value=600, value=300, step=50,
                              help="Render resolution for OCR. 300 is a good default; raise it for tiny text.")
        force_ocr = st.toggle("Force OCR even if a text layer exists", value=False,
                              help="Ignore any existing text and OCR every page. For the PDF output this "
                                   "rasterises pages (OCRmyPDF --force-ocr).")
        lang_options = langs or ["eng"]
        default_lang = ["eng"] if "eng" in lang_options else lang_options[:1]
        chosen = st.multiselect("OCR language(s)", lang_options, default=default_lang,
                                help="Tesseract language packs installed on this machine.")
        heading_ratio = st.slider("Heading size threshold (× body text)", 1.2, 2.5, 1.5, 0.05,
                                  help="Lines this much taller than the median body text become headings.")
        st.subheader("Memory")
        force_sequential = st.toggle(
            "Force sequential mode", value=FORCE_SEQUENTIAL,
            help="Build the Markdown first, then the searchable PDF, one at a time. Recommended on "
                 "low-RAM hosts such as Streamlit Community Cloud (env: SPM_FORCE_SEQUENTIAL=1).")
        min_mem = st.number_input(
            "Min free RAM for concurrent mode (MB)", min_value=256, max_value=262_144,
            value=MIN_CONCURRENT_MEM_MB, step=256,
            help="Below this much free memory the two OCR stages run one after the other "
                 "(env: SPM_MIN_CONCURRENT_MEM_MB).")
        mode, reason = choose_schedule(Options(force_sequential=force_sequential,
                                               min_concurrent_mem_mb=int(min_mem)))
        st.caption(f"Mode now: **{mode}** ({reason})")
        with st.expander("System check"):
            for name, path in tools.items():
                st.markdown(f"{'✅' if path else '❌'} **{name}**" + (f"  \n`{path}`" if path else ""))
            if not langs:
                st.caption("Could not list Tesseract languages.")
            st.markdown(f"Hyphenation dictionary: `{dictionary()[1]}`")

    opts = Options(dpi=int(dpi), force_ocr=force_ocr, lang="+".join(chosen) or "eng",
                   heading_ratio=float(heading_ratio), force_sequential=force_sequential,
                   min_concurrent_mem_mb=int(min_mem))

    uploaded = st.file_uploader("Upload a document", type=["pdf", "docx", "doc"])
    if uploaded is None:
        st.info("Supported: .pdf (text or scanned), .docx, .doc")
        return

    data = uploaded.getvalue()
    # Scheduling settings change how the work runs, not its output, so they're not part of the key.
    key = f"{hashlib.sha256(data).hexdigest()}|{(opts.dpi, opts.force_ocr, opts.lang, opts.heading_ratio)}"
    name = _safe_name(uploaded.name)
    stem = Path(name).stem
    cached = st.session_state.get("result")

    if st.button("Convert", type="primary", disabled=not data):
        workdir = Path(tempfile.mkdtemp(prefix="spm_"))
        src = workdir / name
        src.write_bytes(data)
        bar = st.progress(0.0, text="Starting…")
        last = [0.0]

        def progress(frac: float, msg: str) -> None:
            now = time.time()
            if now - last[0] > 0.2 or frac >= 1.0:
                last[0] = now
                bar.progress(max(0.0, min(frac, 1.0)), text=msg)

        def keep_markdown(md: str) -> None:
            # Checkpoint: if the searchable-PDF stage dies or the run is interrupted, the Markdown survives.
            st.session_state["result"] = (key, Result(md, None, [
                "The searchable PDF did not finish; only the Markdown is available."]))

        try:
            with st.spinner(f"Processing {uploaded.name}…"):
                result = convert(src, workdir, opts, progress, keep_markdown)
            st.session_state["result"] = (key, result)
            cached = st.session_state["result"]
        except PipelineError as exc:
            bar.empty()
            st.error(str(exc))
            return
        except Exception as exc:
            bar.empty()
            st.error(f"Unexpected error: {exc}")
            with st.expander("Details"):
                st.exception(exc)
            return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        bar.empty()

    if not cached or cached[0] != key:
        if cached:
            st.info("The file or options changed. Click **Convert** to process again.")
        return

    result: Result = cached[1]
    for w in result.warnings:
        st.warning(w)

    stats = {k: v for k, v in result.stats.items() if v is not None}
    if stats:
        cols = st.columns(len(stats))
        for col, (k, v) in zip(cols, stats.items()):
            col.metric(k[:1].upper() + k[1:], f"{v}s" if k == "seconds" else v)

    c1, c2 = st.columns(2)
    c1.download_button("⬇️ Download Markdown (.md)", result.markdown.encode("utf-8"), file_name=f"{stem}.md",
                       mime="text/markdown", use_container_width=True)
    if result.pdf_bytes:
        c2.download_button("⬇️ Download searchable PDF", result.pdf_bytes, file_name=f"{stem}_searchable.pdf",
                           mime="application/pdf", use_container_width=True)
    else:
        c2.button("Searchable PDF unavailable (see warnings)", disabled=True, use_container_width=True)

    st.subheader("Markdown preview")
    md = result.markdown
    if len(md) > PREVIEW_CHARS:
        cut = md.rfind("\n\n", 0, PREVIEW_CHARS)
        st.markdown(md[: cut if cut > 0 else PREVIEW_CHARS])
        st.caption(f"Preview truncated — showing {PREVIEW_CHARS:,} of {len(md):,} characters. "
                   "The full text is below and in the download.")
    else:
        st.markdown(md or "_(empty)_")
    with st.expander("Full Markdown text"):
        st.code(md, language="markdown")


# --------------------------------------------------------------------------------------
# Command line (headless) entry point
# --------------------------------------------------------------------------------------

def cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Convert a PDF/DOCX/DOC into Markdown + a searchable PDF.")
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("."))
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--force-ocr", action="store_true")
    ap.add_argument("--heading-ratio", type=float, default=1.5)
    ap.add_argument("--no-pdf", action="store_true", help="only produce the Markdown")
    ap.add_argument("--sequential", action="store_true", default=FORCE_SEQUENTIAL,
                    help="never run Tesseract and OCRmyPDF at the same time (low-RAM hosts)")
    ap.add_argument("--min-concurrent-mem-mb", type=int, default=MIN_CONCURRENT_MEM_MB,
                    help="free RAM needed to run both OCR stages at once (default %(default)s)")
    args = ap.parse_args(argv)

    opts = Options(args.dpi, args.force_ocr, args.lang, args.heading_ratio, not args.no_pdf,
                   args.sequential, args.min_concurrent_mem_mb)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="spm_"))
    try:
        src = workdir / _safe_name(args.input.name)
        shutil.copyfile(args.input, src)
        result = convert(src, workdir, opts, lambda f, m: print(f"[{f:4.0%}] {m}", file=sys.stderr))
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    stem = Path(_safe_name(args.input.name)).stem
    md_path = args.out_dir / f"{stem}.md"
    md_path.write_text(result.markdown, encoding="utf-8")
    print(f"wrote {md_path}")
    if result.pdf_bytes:
        pdf_path = args.out_dir / f"{stem}_searchable.pdf"
        pdf_path.write_bytes(result.pdf_bytes)
        print(f"wrote {pdf_path}")
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(result.stats)
    return 0


def _running_in_streamlit() -> bool:
    try:
        from streamlit import runtime
        return runtime.exists()
    except Exception:
        return False


if __name__ == "__main__":
    if _running_in_streamlit():
        main()
    else:
        sys.exit(cli(sys.argv[1:]))
