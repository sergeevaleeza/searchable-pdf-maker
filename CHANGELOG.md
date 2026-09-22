# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
