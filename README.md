# Searchable PDF Maker

A Streamlit app that turns a PDF or Word document into:

- **Markdown**: clean and structured, with headings, paragraphs, and ordered and bullet lists. For Word files, tables too.
- **A searchable PDF**: your original pages with an invisible, selectable OCR text layer added by [OCRmyPDF](https://ocrmypdf.readthedocs.io/). The pages look exactly as they did before.

It works on scanned or image-only PDFs, where the pages are just pictures, as well as on normal text PDFs, `.docx` and `.doc` files.

## How it works

| Input | Markdown | Searchable PDF |
| --- | --- | --- |
| Text PDF (every page has text) | Words and font sizes read from the text layer (pdfplumber) | `ocrmypdf --skip-text` |
| Text PDF with some textless pages | Text layer; pages with no text are OCR'd | `ocrmypdf --redo-ocr` |
| Scanned / image-only PDF, including a hidden text layer | OCR pipeline (below) | `ocrmypdf --redo-ocr`, then `--force-ocr` if that fails |
| `.docx` | python-docx: styles, lists, bold/italic, links, tables | LibreOffice → PDF, then as for PDFs |
| `.doc` | LibreOffice → `.docx`, then as above | as above |

A PDF counts as image-only when `pdffonts` finds no fonts, or when `pdftotext` returns almost no text. If those tools aren't available, pypdf does the same check. Text is also counted page by page, so a page with no text is OCR'd even in an otherwise text-based PDF.

**Hidden text layers.** Some PDFs reference a font and contain text-drawing commands, yet have no extractable text. Google Docs exports of scanned pages are an example: they draw one invisible space character in ArialMT for each empty paragraph. The app treats these files as image-only, because they are. OCRmyPDF, though, counts any text command as "this page already has text":

- Its default mode stops with `PriorOcrFoundError`.
- `--skip-text` skips every page, exits successfully, and returns the file unchanged, so the "searchable" PDF has no text at all.

So for image-only files and pages the app never uses `--skip-text`:

- It runs `--redo-ocr`, which OCRs every page but keeps any real text.
- If that fails, it retries with `--force-ocr`, which also works but turns the pages into images and makes the file larger.
- A `PriorOcrFoundError` from any mode triggers the next mode instead of an error.
- After OCRmyPDF finishes, the app checks that the output really contains text. A run that "succeeded" without adding any counts as a failure.
- If every attempt fails, you still get the Markdown, plus a warning: *Searchable PDF could not be generated for this file: …*

**Multi-column text PDFs.** pdfplumber's text flow can read straight across a narrow gutter and mix two columns together line by line. Before grouping words into lines, the app looks for gutters: vertical strips that stay free of words for most of the page height. It reads each column top to bottom before moving right, and joins a sentence that continues at the top of the next column. It handles 1 and 2 columns reliably and up to 4 in general. A title or caption that spans the full page width stays in its place above or between the columns.

A page is only split when the evidence is clear:

- the gutter is wider than `COLUMN_MIN_GUTTER_WIDTH_FRAC` of the page (default 1.2%, about 7pt),
- it has no words in at least `COLUMN_MIN_GUTTER_HEIGHT_FRAC` (75%) of the text rows,
- every column holds at least `COLUMN_MIN_SIDE_WORD_FRAC` (15%) of the page's words.

Pages with fewer than `COLUMN_MIN_WORDS` (30) words are never split, and `COLUMN_MAX_COLUMNS` (4) caps the count. Single-column pages come out exactly as before. These constants are at the top of `app.py`.

**The OCR pipeline:**

1. Render each page to PNG at 300 DPI, one page at a time (`pdftoppm -f N -l N`), into a temporary file that Tesseract reads directly. No page bitmap is held in memory, and each file is deleted once its page is done.
2. Run Tesseract (`image_to_data`) to get every word's text, box and confidence.
3. Estimate each word's font size from its box height. The estimate corrects for letter shape: "was" is shorter than "Appendix" at the same font size. The median over the whole document is the body-text size.
4. Group words into lines and paragraphs using Tesseract's block, paragraph and line numbers. Also split paragraphs at large gaps and after short lines that end a sentence.
5. Tidy the text:
   - Join wrapped lines back together and deal with hyphens at line breaks (see below).
   - Rejoin paragraphs that continue onto the next page.
   - Turn lines at least about 1.5× the body size into headings (the threshold is adjustable). Group heading sizes into tiers: the largest is H1, the next H2, the rest H3. Merge headings that wrapped onto two lines.
   - Keep `1.` / `1)` lines as ordered lists and split items that OCR put on one line (`3. Foo 4. Bar`). Bullets become `-`, including common OCR misreadings of `•`.
6. Remove clutter:
   - Page numbers and footers like `Page 12`, `3 of 10` or a bare `7` in the margin.
   - Headers and footers that repeat on many pages.
   - Junk from figures and charts: mostly non-letters, very short, or low OCR confidence.

**Hyphens at line breaks.** A hyphen at the end of a line can be a word split to fit the line ("informa-" / "tion"), or part of a real term ("T-" / "cell"). Removing it from a real term corrupts the term, so the app keeps the hyphen unless the dictionary clearly says otherwise:

- A soft hyphen (U+00AD) is always removed and the halves joined, because it only ever marks a line break.
- A normal hyphen is removed only when the joined word is in the dictionary and the hyphenated form isn't. "informa-tion" becomes "information", while "T-cell", "beta-blocker", "CD4-positive" and "IL-6R" keep their hyphens.
- The lookup ignores case and surrounding punctuation, but the output keeps the original casing.

The dictionary is `/usr/share/dict/words` (the `wamerican` apt package, listed in `packages.txt`). It's read once at startup and kept in memory. If the file is missing, as on Windows, the app uses a built-in list of about 430 common words defined in `app.py`. With that smaller list, more line-break hyphens are kept, which is the safe outcome. No `pyenchant` or `nltk` is needed. The sidebar's **System check** shows which dictionary is in use.

## Run locally

Requires Python 3.10+ and these system packages:

| Package | Used for | Debian/Ubuntu | macOS (Homebrew) | Windows |
| --- | --- | --- | --- | --- |
| Tesseract | OCR | `tesseract-ocr` (+ `tesseract-ocr-<lang>`) | `tesseract tesseract-lang` | [UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) |
| Poppler | page rendering, `pdffonts`, `pdftotext` | `poppler-utils` | `poppler` | [poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases) |
| Ghostscript | used by OCRmyPDF | `ghostscript` | `ghostscript` | [ghostscript.com](https://ghostscript.com/releases/gsdnld.html) |
| LibreOffice | Word → PDF | `libreoffice` | `--cask libreoffice` | [libreoffice.org](https://www.libreoffice.org/download/) |

```bash
# Debian/Ubuntu
sudo apt-get install -y $(cat packages.txt)

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Open <http://localhost:8501>, upload a file, and click **Convert**.

On Windows the app also looks in the default install folders for Tesseract, LibreOffice, Ghostscript and Poppler, so they don't have to be on `PATH`. The sidebar's **System check** shows which tools were found.

LibreOffice is only needed for Word files. Without it, `.docx` files still produce Markdown but no PDF, and `.doc` files can't be read. Tesseract and Poppler are only needed when a document requires OCR.

### Sidebar options

- **OCR DPI** (default 300): raise it for very small print. Use 200 for speed or on low-memory hosts. If not even one 300-DPI page fits in free memory, the app drops to 200 DPI itself and says so.
- **Force OCR even if a text layer exists**: ignores any existing text and OCRs every page. For the PDF output this runs `ocrmypdf --force-ocr`, which turns the pages into images.
- **OCR language(s)**: any installed Tesseract language packs, combined as `eng+deu` and so on.
- **Heading size threshold**: how much bigger than body text a line must be to count as a heading.
- **Force sequential mode** and **Min free RAM for concurrent mode (MB)**: see [Memory and concurrency](#memory-and-concurrency). The caption under them shows which mode the next run will use and why.

### Command line

The same pipeline runs without the UI:

```bash
python app.py scan.pdf -o out/            # writes out/scan.md and out/scan_searchable.pdf
python app.py report.docx --no-pdf        # Markdown only
python app.py scan.pdf --lang eng+fra --dpi 400 --force-ocr
python app.py scan.pdf --sequential       # low-RAM machine: never run both OCR stages at once
```

## Memory and concurrency

A PDF conversion has two heavy stages:

- Tesseract OCR, which produces the Markdown,
- OCRmyPDF, which produces the searchable PDF.

Both use a lot of RAM. Running them at the same time is faster, but on a small host it can get the app killed for running out of memory. Before each PDF conversion, the app works out how much memory it may use. It reads free memory with `psutil`, and inside a Linux container it also reads the container's memory limit (cgroup v1 or v2) and uses the smaller figure, because `psutil` reports the whole host's RAM there. It then picks a mode:

| Mode | When | What happens |
| --- | --- | --- |
| **Concurrent** | Free RAM is at least the threshold (default 1500 MB) | OCRmyPDF starts in the background while the Markdown is extracted. Memory and CPU cores are split between them. |
| **Sequential** | Free RAM is below the threshold, **Force sequential mode** is on, or psutil is unavailable | The stages run one after the other, and each may use the whole budget. |

Worker counts are then limited by memory as well as by CPU cores. Inside a container the core count is the host's, so without that limit a 1 GB container on an 8-core host would start 8 Tesseract workers at once. The app plans to use at most 75% of free memory, using these per-worker costs measured on letter-size pages:

- one OCR worker (`pdftoppm` + Tesseract): about 215 MB at 300 DPI, or 150 MB at 200 DPI;
- one OCRmyPDF job: about the same, plus about 130 MB for OCRmyPDF itself.

If not even one worker fits at the chosen DPI, the OCR resolution drops to 200 DPI and a warning says so.

In sequential mode the Markdown is always finished before OCRmyPDF starts. If OCRmyPDF then runs out of memory, you still have the complete Markdown: the UI saves it as soon as it's ready, and any searchable-PDF failure (including the process being killed) becomes a warning instead of an error.

The chosen mode shows in the progress bar and in the **Schedule** metric, and is logged.

### Troubleshooting

Every PDF conversion logs a one-line pre-flight profile to stderr, which appears in Streamlit Cloud's app logs:

```text
preflight: file=Nutrition_Book_ch04.pdf size=21.4MB pages=87 fonts=True text_chars=0 pages_without_text=87 hidden_text_layer=True image_only=True | dpi=300 schedule=sequential ocr_workers=3 ocrmypdf_jobs=2 ocrmypdf_modes=--redo-ocr,--force-ocr est_peak=653MB free=1,000MB
```

The same details are in the **Diagnostics** expander below the results. Each stage also logs how long it took and its peak memory (the app plus Tesseract, Ghostscript and the other helper processes it starts). A failing stage logs its full traceback with the file and, for OCR, the page number.

Settings:

- `SPM_MIN_CONCURRENT_MEM_MB` (default `1500`): free RAM, in MB, needed for concurrent mode. Also the sidebar number box and `--min-concurrent-mem-mb`.
- `SPM_FORCE_SEQUENTIAL=1`: always use sequential mode. Also the sidebar toggle and `--sequential`.

## Deploy on Streamlit Community Cloud

`requirements.txt` lists the Python packages and `packages.txt` lists the apt packages. Streamlit Cloud installs both automatically. To support more OCR languages, add packages such as `tesseract-ocr-deu` to `packages.txt`.

The free tier has about 1 GB of RAM. Because the app reads the container's memory limit, the default threshold picks sequential mode there, and worker counts are limited to what fits. To make that explicit, add a secret or environment variable `SPM_FORCE_SEQUENTIAL = "1"`. Keep the OCR DPI at 300 or lower on that tier, and use 200 for very long documents, because memory per page grows with the square of the DPI.

Uploads are limited to 200 MB by default. To raise the limit, set `server.maxUploadSize` in `.streamlit/config.toml`.

## Large documents

- Pages are rendered and OCR'd one at a time, and a page's image is deleted as soon as it's done. Work is spread across as many CPU cores as fit in memory, and the progress bar shows `OCR: page N of total`. Memory therefore depends on the number of workers, not the number of pages. For an 87-page, 22 MB scanned book, peak memory was about 2.0 GB with 6 OCR workers and 6 OCRmyPDF jobs running at the same time. Within a 1 GB container limit (sequential, 3 workers then 2 jobs) it was 0.58 GB.
- When there's enough memory, OCRmyPDF builds the searchable PDF in a separate process while the Markdown is being made. See [Memory and concurrency](#memory-and-concurrency).
- Very large pages (posters, drawings) are rendered at a lower DPI so they don't use too much memory.
- If one page fails, it's reported as a warning and the other pages still convert.

For reference, a 120-page scanned PDF took about 75 seconds on a 12-core laptop.

## Tests

```bash
pip install pytest reportlab       # reportlab only builds the text-PDF test samples
python -m pytest -q
python tests/make_samples.py samples/   # writes sample_scanned.pdf, sample_text.pdf, sample_two_column.pdf, sample.docx
```

The end-to-end tests are skipped automatically when Tesseract, Poppler or OCRmyPDF aren't installed.

The hidden-text-layer regression tests use two local test documents in `tests/fixtures/`: `nutrition_ch04_p1-3.pdf` (the first three pages of a Google Docs export of a scanned book chapter) and the full 87-page `Nutrition_Book_ch04.pdf`. PDFs in that folder are git-ignored and aren't in the repository, so these tests skip when the files are absent. With the full file present, the suite takes about 1.5 minutes longer. The unit tests for the same logic always run.
