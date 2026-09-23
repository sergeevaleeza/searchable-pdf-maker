# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] - 2026-09-22

### Fixed

- Hyphenated terms split across a line break are no longer glued together. "T-cell", "beta-blocker" and "CD4-positive" used to become "Tcell", "betablocker" and "CD4- positive". A line-end hyphen is now removed only when the dictionary shows it was just splitting an ordinary word ("informa-tion" becomes "information"); otherwise it's kept. Soft hyphens are still always removed.
- Two-column text PDFs (for example academic papers with a narrow gutter) no longer mix the columns together line by line. Each column is read top to bottom before the next. Full-width titles stay in place, and a sentence that continues into the next column is joined up. Single-column pages come out exactly as before.
- The app no longer gets killed for running out of memory on low-RAM hosts such as Streamlit Community Cloud. When free memory is low, OCR for the Markdown and OCRmyPDF for the searchable PDF run one after the other instead of at the same time.

### Changed

- Sequential mode always finishes the Markdown before starting the searchable PDF. The UI saves the Markdown as soon as it's ready, so it survives even if OCRmyPDF later fails or is killed. Any failure in the searchable-PDF step now shows as a warning with the Markdown still available, and an out-of-memory kill gets a clear message.
- New sidebar controls **Force sequential mode** and **Min free RAM for concurrent mode (MB)**, with matching environment variables `SPM_FORCE_SEQUENTIAL` and `SPM_MIN_CONCURRENT_MEM_MB` (default 1500) and CLI flags `--sequential` and `--min-concurrent-mem-mb`. The mode used is shown in the progress bar and in a new **Schedule** metric.
- The hyphen dictionary is read from `/usr/share/dict/words`, or a built-in word list when that file is missing. The sidebar's System check shows which one is in use.
- Dependencies: added `psutil` to `requirements.txt` and `wamerican` (which provides `/usr/share/dict/words`) to `packages.txt`.

## [0.1.0] - 2026-09-22

### Added

- `app.py`: a Streamlit app that turns an uploaded PDF, `.docx` or `.doc` file into a Markdown file and a searchable PDF, with download buttons for both and a Markdown preview (plus an expander with the full text).
- Detection of image-only PDFs using `pdffonts`/`pdftotext`, with pypdf as a fallback. In text PDFs, any page with no text is OCR'd on its own.
- OCR pipeline:
  - Renders pages at 300 DPI and gets word boxes from Tesseract.
  - Estimates font size from each word's height, corrected for letter shape.
  - Groups words into lines and paragraphs and joins wrapped lines, including words hyphenated across lines.
  - Groups heading sizes into H1, H2 and H3 and merges headings that wrapped onto two lines.
  - Keeps ordered lists and splits items OCR merged onto one line; turns bullets into `-`, including common OCR misreadings of `•`.
  - Removes page numbers (`Page 12`), repeated headers and footers, and junk from figures.
  - Rejoins paragraphs that continue onto the next page.
- Same structure rebuilding for PDFs that already have text (pdfplumber, with bold short lines treated as headings).
- Word support: python-docx gives headings, numbered and bullet lists, bold/italic, links and tables. LibreOffice headless converts to PDF and `.doc` to `.docx`. Word files that contain only scanned images fall back to OCR.
- Searchable PDF made with OCRmyPDF (`--skip-text`, or `--force-ocr` when chosen). It runs in a separate process at the same time as the Markdown step, and the original appearance is kept.
- Sidebar options: OCR DPI, force OCR, Tesseract language(s), heading size threshold, and a system check of installed tools.
- A progress bar that reports `page N of total`. Results are kept between reruns so clicking a download button doesn't convert again. Clear error messages when a tool is missing.
- Command line mode (`python app.py input.pdf -o out/`).
- `requirements.txt`, `packages.txt` (apt packages for Streamlit Cloud), `README.md`.
- Tests (`tests/test_pipeline.py`) and a sample generator (`tests/make_samples.py`) that makes a scanned PDF, a text PDF and a `.docx`.
