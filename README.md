# Searchable PDF Maker

A Streamlit app that turns a PDF or Word document into:

- **Markdown**: clean and structured, with headings, paragraphs, and ordered and bullet lists. For Word files, tables too.
- **A searchable PDF**: your original pages with an invisible, selectable OCR text layer added by [OCRmyPDF](https://ocrmypdf.readthedocs.io/). The pages look exactly as they did before.

It works on scanned or image-only PDFs, where the pages are just pictures, as well as on normal text PDFs, `.docx` and `.doc` files.

## How it works

| Input | Markdown | Searchable PDF |
| --- | --- | --- |
| Text PDF | Words and font sizes read from the text layer (pdfplumber). Pages with no text are OCR'd. | `ocrmypdf --skip-text` |
| Scanned / image-only PDF | OCR pipeline (below) | `ocrmypdf` adds a text layer under the page images |
| `.docx` | python-docx: styles, lists, bold/italic, links, tables | LibreOffice → PDF, then `ocrmypdf --skip-text` |
| `.doc` | LibreOffice → `.docx`, then as above | as above |

A PDF counts as image-only when `pdffonts` finds no fonts, or when `pdftotext` returns almost no text. If those tools aren't available, pypdf does the same check.

**Multi-column text PDFs.** pdfplumber's text flow can read straight across a narrow gutter and mix two columns together line by line. Before grouping words into lines, the app looks for gutters: vertical strips that stay free of words for most of the page height. It reads each column top to bottom before moving right, and joins a sentence that continues at the top of the next column. It handles 1 and 2 columns reliably and up to 4 in general. A title or caption that spans the full page width stays in its place above or between the columns.

A page is only split when the evidence is clear:

- the gutter is wider than `COLUMN_MIN_GUTTER_WIDTH_FRAC` of the page (default 1.2%, about 7pt),
- it has no words in at least `COLUMN_MIN_GUTTER_HEIGHT_FRAC` (75%) of the text rows,
- every column holds at least `COLUMN_MIN_SIDE_WORD_FRAC` (15%) of the page's words.

Pages with fewer than `COLUMN_MIN_WORDS` (30) words are never split, and `COLUMN_MAX_COLUMNS` (4) caps the count. Single-column pages come out exactly as before. These constants are at the top of `app.py`.

**The OCR pipeline:**

1. Render each page to PNG at 300 DPI, one page at a time so memory stays flat.
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

Open http://localhost:8501, upload a file, and click **Convert**.

On Windows the app also looks in the default install folders for Tesseract, LibreOffice, Ghostscript and Poppler, so they don't have to be on `PATH`. The sidebar's **System check** shows which tools were found.

LibreOffice is only needed for Word files. Without it, `.docx` files still produce Markdown but no PDF, and `.doc` files can't be read. Tesseract and Poppler are only needed when a document requires OCR.

### Sidebar options

- **OCR DPI** (default 300): raise it for very small print, lower it for speed.
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

Both use a lot of RAM. Running them at the same time is faster, but on a small host it can get the app killed for running out of memory. Before each PDF conversion, the app checks free memory with `psutil` and picks a mode:

| Mode | When | What happens |
| --- | --- | --- |
| **Concurrent** | Free RAM is at least the threshold (default 1500 MB) | OCRmyPDF starts in the background while the Markdown is extracted. The CPU cores are split between them. |
| **Sequential** | Free RAM is below the threshold, **Force sequential mode** is on, or psutil is unavailable | The stages run one after the other, and each gets every CPU core. |

In sequential mode the Markdown is always finished before OCRmyPDF starts. If OCRmyPDF then runs out of memory, you still have the complete Markdown: the UI saves it as soon as it's ready, and any searchable-PDF failure (including the process being killed) becomes a warning instead of an error.

The chosen mode shows in the progress bar and in the **Schedule** metric, and is logged.

Settings:

- `SPM_MIN_CONCURRENT_MEM_MB` (default `1500`): free RAM, in MB, needed for concurrent mode. Also the sidebar number box and `--min-concurrent-mem-mb`.
- `SPM_FORCE_SEQUENTIAL=1`: always use sequential mode. Also the sidebar toggle and `--sequential`.

## Deploy on Streamlit Community Cloud

`requirements.txt` lists the Python packages and `packages.txt` lists the apt packages. Streamlit Cloud installs both automatically. To support more OCR languages, add packages such as `tesseract-ocr-deu` to `packages.txt`.

The free tier has about 1 GB of RAM. The default threshold already picks sequential mode there. To make that explicit, add a secret or environment variable `SPM_FORCE_SEQUENTIAL = "1"`. Also keep the OCR DPI at 300 or lower on that tier, because memory per page grows with the square of the DPI.

Uploads are limited to 200 MB by default. To raise the limit, set `server.maxUploadSize` in `.streamlit/config.toml`.

## Large documents

- Pages are rendered and OCR'd one at a time, spread across CPU cores, and the progress bar shows `OCR: page N of total`.
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
