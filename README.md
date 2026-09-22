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

**The OCR pipeline:**

1. Render each page to PNG at 300 DPI, one page at a time so memory stays flat.
2. Run Tesseract (`image_to_data`) to get every word's text, box and confidence.
3. Estimate each word's font size from its box height. The estimate corrects for letter shape: "was" is shorter than "Appendix" at the same font size. The median over the whole document is the body-text size.
4. Group words into lines and paragraphs using Tesseract's block, paragraph and line numbers. Also split paragraphs at large gaps and after short lines that end a sentence.
5. Tidy the text:
   - Join wrapped lines back together, including words hyphenated across a line break.
   - Rejoin paragraphs that continue onto the next page.
   - Turn lines at least about 1.5× the body size into headings (the threshold is adjustable). Group heading sizes into tiers: the largest is H1, the next H2, the rest H3. Merge headings that wrapped onto two lines.
   - Keep `1.` / `1)` lines as ordered lists and split items that OCR put on one line (`3. Foo 4. Bar`). Bullets become `-`, including common OCR misreadings of `•`.
6. Remove clutter:
   - Page numbers and footers like `Page 12`, `3 of 10` or a bare `7` in the margin.
   - Headers and footers that repeat on many pages.
   - Junk from figures and charts: mostly non-letters, very short, or low OCR confidence.

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

### Command line

The same pipeline runs without the UI:

```bash
python app.py scan.pdf -o out/            # writes out/scan.md and out/scan_searchable.pdf
python app.py report.docx --no-pdf        # Markdown only
python app.py scan.pdf --lang eng+fra --dpi 400 --force-ocr
```

## Deploy on Streamlit Community Cloud

`requirements.txt` lists the Python packages and `packages.txt` lists the apt packages. Streamlit Cloud installs both automatically. To support more OCR languages, add packages such as `tesseract-ocr-deu` to `packages.txt`.

Uploads are limited to 200 MB by default. To raise the limit, set `server.maxUploadSize` in `.streamlit/config.toml`.

## Large documents

- Pages are rendered and OCR'd one at a time, spread across CPU cores, and the progress bar shows `OCR: page N of total`.
- OCRmyPDF builds the searchable PDF in a separate process while the Markdown is being made, so the two run side by side.
- Very large pages (posters, drawings) are rendered at a lower DPI so they don't use too much memory.
- If one page fails, it's reported as a warning and the other pages still convert.

For reference, a 120-page scanned PDF took about 75 seconds on a 12-core laptop.

## Tests

```bash
pip install pytest reportlab       # reportlab only builds a text-PDF test sample
python -m pytest -q
python tests/make_samples.py samples/   # writes sample_scanned.pdf, sample_text.pdf, sample.docx
```

The end-to-end tests are skipped automatically when Tesseract, Poppler or OCRmyPDF aren't installed.
