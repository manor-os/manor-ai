---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files. This includes reading or extracting text/tables from PDFs, combining or merging multiple PDFs into one, splitting PDFs apart, rotating pages, adding watermarks, creating new PDFs with user-provided, downloaded, or generated imagery, filling PDF forms, encrypting/decrypting PDFs, extracting images, and OCR on scanned PDFs to make them searchable. If the user mentions a .pdf file or asks to produce one, use this skill.
license: Proprietary. LICENSE.txt has complete terms
---

# PDF Processing Guide

## Overview

This guide covers essential PDF processing operations using Python libraries and command-line tools. For advanced features, JavaScript libraries, and detailed examples, see REFERENCE.md. If you need to fill out a PDF form, read FORMS.md and follow its instructions.

## Required Workflow

1. Preserve the source PDF. Treat chat uploads, Knowledge files, and `/workspace/` files as read-only inputs. Never overwrite a mounted source; create a descriptively named output copy.
2. Inspect structure with `pypdf` or `pdfplumber`, but render every relevant page to PNG for layout review. Text extraction alone is not a visual correctness check.
3. Put temporary work under `tmp/pdfs/` and final files under `output/pdf/` inside the skill workspace.
4. After each meaningful edit, run `python scripts/verify_pdf.py <output.pdf> <render_dir/>`, then inspect every rendered page for alignment, spacing, clipping, glyphs, tables, images, headers, footers, and page transitions. Add `--expect-interactive --require-form-appearances` for an editable form or `--expect-flattened` for a static form copy.
5. Before returning, save the verified file with `sandbox(action="save_result", params={...})`. That creates a new managed artifact through Manor's file-action boundary; do not use `generate_file` or `patch_file` to replace a read-only attachment.

For signed PDFs, preserve the signed original. Editing invalidates its signature. Refuse by default and continue only when the user explicitly accepts signature invalidation. Never flatten a signed PDF without that explicit decision.

### Final Delivery Contract

- Deliver one canonical PDF from the absolute path `/skill/output/pdf/<descriptive-name>.pdf`. The `filename` passed to sandbox action `save_result` must match that basename.
- Call sandbox action `save_result` only after structural verification and page-by-page visual review. Save the final PDF exactly once in the successful path.
- Never save helper scripts, render PNGs, temporary copies, intermediate PDFs, or a fallback DOCX as final artifacts. Keep all of those under `/skill/tmp/pdfs/`.
- If sandbox action `save_result` fails, use its exact returned error to correct the path or artifact once. Do not guess alternate directories or repeat an unchanged save call. The runtime stops the same failed file delivery after three attempts.
- Do not generate a DOCX substitute unless the user explicitly asked for a Word version. A missing PDF dependency is a readiness error, not permission to change the requested file type.

## Rendering and Validation

Prefer Poppler for deterministic rendering and metadata checks:

```bash
mkdir -p tmp/pdfs/rendered output/pdf
pdfinfo input.pdf
pdftoppm -png input.pdf tmp/pdfs/rendered/page
```

The bundled renderer keeps Poppler's full output resolution instead of downscaling pages:

```bash
python scripts/convert_pdf_to_images.py input.pdf tmp/pdfs/rendered --dpi 150 --json
python scripts/verify_pdf.py input.pdf tmp/pdfs/verified
```

`verify_pdf.py` reopens the file with `pypdf`, checks it with `pdfinfo`, renders every page through `pdftoppm`, compares the structural and rendered page counts, and reports form/signature risks. A passing machine report is not a visual review. Do not deliver until you inspect the latest rendered pages and they have zero visible defects.

## Image-First Visual Documents

Use the semantic visual-document composer instead of a fixed template for image-led briefs, property or product listings, portfolios, catalogs, case studies, field guides, and other PDFs whose layout depends on the supplied images and copy.

Read `references/visual-document-schema.md` when the request combines multiple images with native tables, captions, callouts, contacts, repeated records, or content-dependent pagination. Build a JSON recipe from semantic blocks, then run:

```bash
python scripts/compose_visual_document.py \
  tmp/pdfs/visual-document.json \
  output/pdf/visual-document.pdf
python scripts/verify_pdf.py \
  output/pdf/visual-document.pdf \
  tmp/pdfs/visual-document-review
```

The composer supports paragraphs, key-value tables, callouts, single hero images, adaptive one-to-four-column galleries, contact lists, theme colors, focal-point cropping, native CJK text, continuation labels, page numbers, and external links. It validates the recipe and every local image before writing. Treat the JSON as a content-and-layout plan, not a locked design template.

Do not manually place every image with ad hoc coordinates when this block model fits. Do not choose a fixed template merely because the output is a PDF. Choose a bundled template only when the user asks for one or the content closely matches that template's document type.

## Reusable Templates

Use the bundled data-driven templates for polished new documents:

- `business-report`: landscape editorial dashboards for research and market insights.
- `project-proposal`: bold two-page creative proposals and launch plans.
- `invoice`: Swiss-style quotes and invoices for professional services.
- `academic-paper`: three-page, two-column research manuscripts with an abstract, findings, tables, and references.

Template specifications and sample data live under `assets/templates/`. Copy an example JSON into `tmp/pdfs/`, replace the sample content, and render it to a new output file. Do not edit the bundled example in place.

```bash
python scripts/generate_from_template.py list
python scripts/generate_from_template.py render business-report \
  tmp/pdfs/report-data.json output/pdf/business-report.pdf
python scripts/verify_pdf.py output/pdf/business-report.pdf tmp/pdfs/business-report-review
```

The generator validates required data, resolves fonts across sandbox/Linux/macOS environments, normalizes unsupported Unicode dashes, writes atomically, and reopens the PDF before returning. Set `PDF_TEMPLATE_FONT_REGULAR` and optionally `PDF_TEMPLATE_FONT_BOLD` when the runtime uses custom brand fonts.

## Images in Created PDFs

Created PDFs may use photographs, illustrations, diagrams, textures, logos, and other raster images when they improve the document. Do not default to text-only pages when the user's brief calls for visual storytelling. Use one or more of these source routes:

1. **User-provided or Knowledge image** - import the returned `fs_path` or upload path into `/skill/tmp/pdfs/assets/` with sandbox action `write_file`. Preserve the original as a read-only input.
2. **Downloaded image** - use `web_search` or `web_fetch` to identify the source page and confirm the usage rights, then download the direct HTTPS image URL into `/skill/tmp/pdfs/assets/source/`. Record the source page, direct asset URL, license, and required credit. If the license or permission is unknown, do not embed the image.
3. **Generated image** - call the system `generate_file(kind="image")` tool with the subject, visual style, composition, and aspect ratio. Use text-free imagery; keep all titles, labels, charts, and factual data as native PDF content. Import the returned workspace `fs_path` into `/skill/tmp/pdfs/assets/source/` with sandbox action `write_file` before continuing.

Never point ReportLab, fpdf2, or another renderer at a remote URL. Every image must exist as a local sandbox file before layout begins. If a download is unavailable in the sandbox, use a user-provided or generated image instead and report the limitation; do not leave a broken placeholder.

Normalize each selected image before embedding:

```bash
python scripts/prepare_image_asset.py \
  tmp/pdfs/assets/source/hero.png \
  tmp/pdfs/assets/hero.png \
  --source-type generated \
  --source-ref "workspace:generated/hero.png" \
  --prompt "Abstract competitive team arena, deep navy and orange, no text" \
  --manifest tmp/pdfs/assets/manifest.json
```

For a downloaded image, pass `--source-type downloaded`, `--source-ref <source-page-or-direct-url>`, and `--license <license-or-user-authorized>`. The helper applies EXIF orientation, caps oversized files, normalizes color/alpha, verifies the written image, and appends provenance plus dimensions and checksum to the asset manifest.

When laying out images:

- Crop intentionally and preserve the focal subject; do not stretch images or distort their aspect ratio.
- Use at least 150 effective DPI for placed raster images and prefer 200-300 DPI for print-oriented pages.
- Avoid enlarging small downloads. Generate or source a larger image instead.
- Keep generated imagery free of embedded text, fake UI, unsupported claims, or data visualizations. Draw accurate labels and charts with native PDF primitives.
- Add visible attribution in the PDF whenever the license requires it.
- After rendering the final PDF, inspect every image for blur, bad crops, transparency artifacts, color shifts, missing credits, and overlap with text.

Example with fpdf2 after preparation:

```python
pdf.image("tmp/pdfs/assets/hero.png", x=16, y=42, w=178, h=82, keep_aspect_ratio=True)
```

## Quick Start

```python
from pypdf import PdfReader, PdfWriter

# Read a PDF
reader = PdfReader("document.pdf")
print(f"Pages: {len(reader.pages)}")

# Extract text
text = ""
for page in reader.pages:
    text += page.extract_text()
```

## Python Libraries

### pypdf - Basic Operations

#### Merge PDFs
```python
from pypdf import PdfWriter, PdfReader

writer = PdfWriter()
for pdf_file in ["doc1.pdf", "doc2.pdf", "doc3.pdf"]:
    reader = PdfReader(pdf_file)
    for page in reader.pages:
        writer.add_page(page)

with open("merged.pdf", "wb") as output:
    writer.write(output)
```

#### Split PDF
```python
reader = PdfReader("input.pdf")
for i, page in enumerate(reader.pages):
    writer = PdfWriter()
    writer.add_page(page)
    with open(f"page_{i+1}.pdf", "wb") as output:
        writer.write(output)
```

#### Extract Metadata
```python
reader = PdfReader("document.pdf")
meta = reader.metadata
print(f"Title: {meta.title}")
print(f"Author: {meta.author}")
print(f"Subject: {meta.subject}")
print(f"Creator: {meta.creator}")
```

#### Rotate Pages
```python
reader = PdfReader("input.pdf")
writer = PdfWriter()

page = reader.pages[0]
page.rotate(90)  # Rotate 90 degrees clockwise
writer.add_page(page)

with open("rotated.pdf", "wb") as output:
    writer.write(output)
```

### pdfplumber - Text and Table Extraction

#### Extract Text with Layout
```python
import pdfplumber

with pdfplumber.open("document.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        print(text)
```

#### Extract Tables
```python
with pdfplumber.open("document.pdf") as pdf:
    for i, page in enumerate(pdf.pages):
        tables = page.extract_tables()
        for j, table in enumerate(tables):
            print(f"Table {j+1} on page {i+1}:")
            for row in table:
                print(row)
```

#### Advanced Table Extraction
```python
import pandas as pd

with pdfplumber.open("document.pdf") as pdf:
    all_tables = []
    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if table:  # Check if table is not empty
                df = pd.DataFrame(table[1:], columns=table[0])
                all_tables.append(df)

# Combine all tables
if all_tables:
    combined_df = pd.concat(all_tables, ignore_index=True)
    combined_df.to_excel("extracted_tables.xlsx", index=False)
```

### reportlab - Create PDFs

#### Creating PDFs with Chinese/CJK Text

For Chinese, Japanese, Korean, or other non-Latin text, use this deterministic path first. Do not start with ReportLab.

Use the preinstalled `fpdf2` runtime and resolve fonts in this order: Source Han Sans SC OTF at `/tmp/fonts/OTF/SimplifiedChinese/`, then the bundled Noto Sans CJK collection at `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`. For Noto's collection, pass `collection_font_number=2` to select the Simplified Chinese face. Do not install PDF packages or download fonts during a generation run; if the preinstalled runtime is missing, report a sandbox readiness error.

Do not use ReportLab with SourceHanSansSC OTF files. ReportLab's `TTFont` does not support PostScript/CFF outlines and will fail with `postscript outlines are not supported`. Do not try to convert SourceHanSansSC OTF to TTF with fonttools for ReportLab; renaming or converting the container does not make the outlines compatible with ReportLab.

Only use ReportLab for CJK text if a verified TrueType-outline `.ttf` font already exists and `pdfmetrics.registerFont(TTFont(...))` succeeds on the first try. Otherwise, use `fpdf2` with Source Han or the bundled Noto collection, or an HTML-to-PDF/browser rendering fallback. Do not repeatedly download fonts from the internet.

Minimal `fpdf2` template for Chinese/CJK text:

```python
from pathlib import Path
from fpdf import FPDF
from fpdf.enums import XPos, YPos

FONT_REG = Path("/tmp/fonts/OTF/SimplifiedChinese/SourceHanSansSC-Regular.otf")
FONT_BOLD = Path("/tmp/fonts/OTF/SimplifiedChinese/SourceHanSansSC-Bold.otf")

if not FONT_REG.exists():
    raise FileNotFoundError(f"Missing CJK font: {FONT_REG}")

pdf = FPDF()
pdf.set_auto_page_break(auto=True, margin=15)
pdf.add_page()

pdf.add_font("SourceHan", "", str(FONT_REG))
if FONT_BOLD.exists():
    pdf.add_font("SourceHan", "B", str(FONT_BOLD))

pdf.set_font("SourceHan", "B" if FONT_BOLD.exists() else "", 22)
pdf.cell(0, 12, "什么是 AI", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

pdf.ln(6)
pdf.set_font("SourceHan", "", 12)
pdf.multi_cell(0, 8, "人工智能是让计算机执行通常需要人类智能才能完成的任务的技术。")

pdf.output("output.pdf")
```

#### Basic PDF Creation
```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("hello.pdf", pagesize=letter)
width, height = letter

# Add text
c.drawString(100, height - 100, "Hello World!")
c.drawString(100, height - 120, "This is a PDF created with reportlab")

# Add a line
c.line(100, height - 140, 400, height - 140)

# Save
c.save()
```

#### Create PDF with Multiple Pages
```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet

doc = SimpleDocTemplate("report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = []

# Add content
title = Paragraph("Report Title", styles['Title'])
story.append(title)
story.append(Spacer(1, 12))

body = Paragraph("This is the body of the report. " * 20, styles['Normal'])
story.append(body)
story.append(PageBreak())

# Page 2
story.append(Paragraph("Page 2", styles['Heading1']))
story.append(Paragraph("Content for page 2", styles['Normal']))

# Build PDF
doc.build(story)
```

#### Subscripts and Superscripts

**IMPORTANT**: Never use Unicode subscript/superscript characters (₀₁₂₃₄₅₆₇₈₉, ⁰¹²³⁴⁵⁶⁷⁸⁹) in ReportLab PDFs. The built-in fonts do not include these glyphs, causing them to render as solid black boxes.

Instead, use ReportLab's XML markup tags in Paragraph objects:
```python
from reportlab.platypus import Paragraph
from reportlab.lib.styles import getSampleStyleSheet

styles = getSampleStyleSheet()

# Subscripts: use <sub> tag
chemical = Paragraph("H<sub>2</sub>O", styles['Normal'])

# Superscripts: use <super> tag
squared = Paragraph("x<super>2</super> + y<super>2</super>", styles['Normal'])
```

For canvas-drawn text (not Paragraph objects), manually adjust font the size and position rather than using Unicode subscripts/superscripts.

## Command-Line Tools

### pdftotext (poppler-utils)
```bash
# Extract text
pdftotext input.pdf output.txt

# Extract text preserving layout
pdftotext -layout input.pdf output.txt

# Extract specific pages
pdftotext -f 1 -l 5 input.pdf output.txt  # Pages 1-5
```

### qpdf
```bash
# Merge PDFs
qpdf --empty --pages file1.pdf file2.pdf -- merged.pdf

# Split pages
qpdf input.pdf --pages . 1-5 -- pages1-5.pdf
qpdf input.pdf --pages . 6-10 -- pages6-10.pdf

# Rotate pages
qpdf input.pdf output.pdf --rotate=+90:1  # Rotate page 1 by 90 degrees

# Remove password
qpdf --password=mypassword --decrypt encrypted.pdf decrypted.pdf
```

### pdftk (if available)
```bash
# Merge
pdftk file1.pdf file2.pdf cat output merged.pdf

# Split
pdftk input.pdf burst

# Rotate
pdftk input.pdf rotate 1east output rotated.pdf
```

## Common Tasks

### Extract Text from Scanned PDFs
```python
# Requires: pip install pytesseract pdf2image
import pytesseract
from pdf2image import convert_from_path

# Convert PDF to images
images = convert_from_path('scanned.pdf')

# OCR each page
text = ""
for i, image in enumerate(images):
    text += f"Page {i+1}:\n"
    text += pytesseract.image_to_string(image)
    text += "\n\n"

print(text)
```

### Add Watermark
```python
from pypdf import PdfReader, PdfWriter

# Create watermark (or load existing)
watermark = PdfReader("watermark.pdf").pages[0]

# Apply to all pages
reader = PdfReader("document.pdf")
writer = PdfWriter()

for page in reader.pages:
    page.merge_page(watermark)
    writer.add_page(page)

with open("watermarked.pdf", "wb") as output:
    writer.write(output)
```

### Extract Images
```bash
# Using pdfimages (poppler-utils)
pdfimages -j input.pdf output_prefix

# This extracts all images as output_prefix-000.jpg, output_prefix-001.jpg, etc.
```

### Password Protection
```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
writer = PdfWriter()

for page in reader.pages:
    writer.add_page(page)

# Add password
writer.encrypt("userpassword", "ownerpassword")

with open("encrypted.pdf", "wb") as output:
    writer.write(output)
```

## Quick Reference

| Task | Best Tool | Command/Code |
|------|-----------|--------------|
| Merge PDFs | pypdf | `writer.add_page(page)` |
| Split PDFs | pypdf | One page per file |
| Extract text | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Create Latin-text PDFs | reportlab | Canvas or Platypus |
| Create Chinese/CJK PDFs | fpdf2 with Source Han or Noto Sans CJK | Prefer Source Han OTF; use bundled Noto CJK TTC face index 2 as the deterministic fallback |
| Command line merge | qpdf | `qpdf --empty --pages ...` |
| OCR scanned PDFs | pytesseract | Convert to image first |
| Fill PDF forms | pdf-lib or pypdf (see FORMS.md) | See FORMS.md |

## Quality Expectations

- Keep typography, margins, spacing, hierarchy, and page numbering consistent.
- Reject clipped or overlapping text, broken tables, black-square glyphs, blurry images, stale form appearances, and unreadable annotations.
- Keep citations and references human-readable; never leave internal tool tokens or placeholders in the PDF.
- Use ASCII hyphens in generated content when a visually similar Unicode dash could be unsupported by the selected font.

## Next Steps

- For advanced pypdfium2 usage, see REFERENCE.md
- For JavaScript libraries (pdf-lib), see REFERENCE.md
- If you need to fill out a PDF form, follow the instructions in FORMS.md
- For troubleshooting guides, see REFERENCE.md
