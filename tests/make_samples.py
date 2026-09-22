"""Generate sample inputs: an image-only (scanned-style) PDF and a .docx.

    python tests/make_samples.py [out_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DPI = 300
PAGE_W, PAGE_H = int(8.5 * DPI), int(11 * DPI)
MARGIN = int(1 * DPI)

FONT_CANDIDATES = {
    "regular": ["arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Helvetica.ttc"],
    "bold": ["arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Helvetica.ttc"],
}
FONT_DIRS = ["C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
             "/usr/share/fonts/TTF", "/System/Library/Fonts", "/Library/Fonts"]


def font(kind: str, pt: float) -> ImageFont.FreeTypeFont:
    px = int(pt * DPI / 72)
    for name in FONT_CANDIDATES[kind]:
        for d in FONT_DIRS:
            p = Path(d) / name
            if p.exists():
                return ImageFont.truetype(str(p), px)
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            pass
    raise RuntimeError("No TrueType font found for generating the sample")


class Page:
    def __init__(self, number: int):
        self.number = number
        self.img = Image.new("L", (PAGE_W, PAGE_H), 255)
        self.draw = ImageDraw.Draw(self.img)
        self.y = MARGIN
        small = font("regular", 9)
        self.draw.text((MARGIN, int(0.5 * DPI)), "ACME Quarterly Report", font=small, fill=0)
        self.draw.text((PAGE_W // 2 - 60, PAGE_H - int(0.6 * DPI)), f"Page {number}", font=small, fill=0)

    def wrap(self, text: str, f, width: int) -> list[str]:
        lines, cur = [], ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if self.draw.textlength(trial, font=f) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        return lines + [cur] if cur else lines

    def text(self, text: str, pt=11, kind="regular", indent=0, space_after=0.9, width=None):
        f = font(kind, pt)
        width = width or PAGE_W - 2 * MARGIN - indent
        line_h = int(pt * DPI / 72 * 1.3)
        for line in self.wrap(text, f, width):
            self.draw.text((MARGIN + indent, self.y), line, font=f, fill=0)
            self.y += line_h
        self.y += int(pt * DPI / 72 * space_after)

    def figure(self):
        """A simple chart with axis ticks and stray symbols: the kind of thing OCR turns into junk."""
        x0, y0, w, h = MARGIN + 200, self.y + 20, 1200, 500
        d = self.draw
        d.line([(x0, y0), (x0, y0 + h), (x0 + w, y0 + h)], fill=0, width=6)
        for i, bh in enumerate([200, 320, 150, 420, 280]):
            d.rectangle([x0 + 60 + i * 220, y0 + h - bh, x0 + 200 + i * 220, y0 + h], fill=90)
        tick = font("regular", 7)
        for i in range(5):
            d.text((x0 - 70, y0 + h - i * 120 - 20), str(i * 10), font=tick, fill=0)
        d.text((x0 + w + 20, y0 + 40), "~ | = -- %", font=tick, fill=0)
        self.y = y0 + h + 120


def make_image_pdf(path: Path) -> Path:
    p1 = Page(1)
    p1.text("Annual Operations Review", pt=26, kind="bold", space_after=0.8)
    p1.text("This report summarises how the operations team performed over the past year. It covers "
            "staffing, logistics and the main risks we expect to face in the coming quarters, and it "
            "is intended for the board and for department leads.")
    p1.text("Overall results were strong. Delivery times fell by a fifth while costs stayed flat, "
            "which is a well-earned result for a team that also moved warehouses in the spring.")
    p1.text("Logistics Performance and Warehouse Consolidation Across All Northern Regions", pt=18,
            kind="bold", space_after=0.6)
    p1.text("We focused on four priorities during the year:")
    p1.text("1. Reduce average delivery time for standard orders.", indent=40, space_after=0.2)
    p1.text("2. Consolidate the three northern warehouses into one site.", indent=40, space_after=0.2)
    p1.text("3. Renegotiate carrier contracts. 4. Improve stock accuracy.", indent=40, space_after=0.9)
    p1.figure()
    p1.text("The consolidation project required close coordination between procurement, facilities "
            "and the regional managers, and the team kept customers informed at every stage while the",
            space_after=0)

    p2 = Page(2)
    p2.text("move was under way, so that no scheduled delivery was missed during the transition period.")
    p2.text("Risks and Outlook", pt=18, kind="bold", space_after=0.6)
    p2.text("Staffing Risk", pt=14, kind="bold", space_after=0.5)
    p2.text("Two senior planners retire next year. We will hire replacements early and pair them with "
            "the current staff for a full quarter of handover.")
    p2.text("Key actions for next year:", space_after=0.4)
    p2.text("• Open the new consolidated northern warehouse.", indent=40, space_after=0.2)
    p2.text("• Pilot electric vans on two urban routes.", indent=40, space_after=0.2)
    p2.text("• Publish a quarterly service-level dashboard.", indent=40, space_after=0.9)

    p3 = Page(3)
    p3.text("Appendix", pt=18, kind="bold", space_after=0.6)
    p3.text("All figures in this report are unaudited and rounded to the nearest whole percent.")

    path.parent.mkdir(parents=True, exist_ok=True)
    first, *rest = [p.img for p in (p1, p2, p3)]
    first.save(path, "PDF", resolution=DPI, save_all=True, append_images=rest)
    return path


def make_text_pdf(path: Path) -> Path:
    """A born-digital PDF with a real text layer (requires reportlab, a test-only dependency)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter

    def write(y, text, size=11, bold=False):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(72, y, text)
        return y - size * 1.3

    for page in (1, 2):
        c.setFont("Helvetica", 9)
        c.drawString(width / 2 - 20, 30, f"Page {page}")
        y = height - 72
        if page == 1:
            y = write(y, "Project Charter", 24, True) - 10
            y = write(y, "This charter defines the scope and goals of the migration project. It is")
            y = write(y, "owned by the platform team and reviewed every quarter.") - 8
            y = write(y, "Goals", 16, True) - 4
            y = write(y, "1. Move all services to the new cluster.")
            y = write(y, "2. Retire the legacy database.") - 8
            y = write(y, "Constraints", 11, True) - 2
            y = write(y, "Downtime must stay under one hour per service.")
        else:
            y = write(y, "Timeline", 16, True) - 4
            write(y, "The migration runs from March to September.")
        c.showPage()
    c.save()
    return path


def make_docx(path: Path) -> Path:
    import docx
    doc = docx.Document()
    doc.add_heading("Team Handbook", level=0)
    doc.add_paragraph("Welcome to the team. This handbook explains how we work.")
    doc.add_heading("Getting Started", level=1)
    p = doc.add_paragraph("Read this ")
    p.add_run("carefully").bold = True
    p.add_run(" before your ")
    p.add_run("first day").italic = True
    p.add_run(".")
    doc.add_paragraph("Collect your laptop", style="List Number")
    doc.add_paragraph("Set up your accounts", style="List Number")
    doc.add_heading("Tools", level=2)
    doc.add_paragraph("Chat for quick questions", style="List Bullet")
    doc.add_paragraph("Email for decisions", style="List Bullet")
    table = doc.add_table(rows=3, cols=2)
    for r, (a, b) in enumerate([("Tool", "Owner"), ("Chat", "IT"), ("Wiki", "Ops")]):
        table.cell(r, 0).text, table.cell(r, 1).text = a, b
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    print(make_image_pdf(out / "sample_scanned.pdf"))
    print(make_docx(out / "sample.docx"))
    try:
        print(make_text_pdf(out / "sample_text.pdf"))
    except ImportError:
        print("reportlab not installed; skipped sample_text.pdf")
