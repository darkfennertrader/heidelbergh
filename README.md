# appway-backend

Backend service for **MyopicCNV+** — an AI-powered Myopic Choroidal
Neovascularisation (mCNV) detection pipeline that processes OCT DICOM
files and produces a branded, multi-page PDF report.

---

## PDF Report Generation

### Overview

The report is generated with a **template-overlay** strategy that gives
pixel-faithful reproduction of the designer's artwork without having to
re-implement gradients, drop-shadows, rounded-corner tabs, and logos
from scratch in a drawing library.

```
pdf_sandbox/designer_source/Myopic2_ver_b.ai  (designer source — internally a 6-page PDF, gitignored)
        │
        │  appway_backend/report/templates.py  (run once, or on .ai changes)
        │  → redact all text spans with PyMuPDF
        │  → render each page to a 300-DPI PNG via PyMuPDF
        │  → post-process table page in PIL (shift card up under logo)
        ▼
appway_backend/report/assets/
  page_template_summary.png   ← page 1 chrome (logo, gradients, footer band)
  page_template_gallery.png   ← page 2 chrome (image slots, teal tabs, footer)
  page_template_table.png     ← page 3 chrome (table card, teal tab, footer)
        │
        │  appway_backend/report/generator.py  build_pdf(job, out_path)
        │  → stamp template PNG as background (ReportLab)
        │  → overlay all dynamic text at designer coordinates
        │    (job ID, verdict card, filenames, confidence, bboxes …)
        │  → rasterise verdict card as PIL gradient PNG, stamp in PDF
        ▼
output PDF  (A4, Montserrat font, pixel-faithful layout)
```

### Pages

| Preview file | Content |
|---|---|
| `verdict_page.png` | Summary: job info, AI verdict card, input files, processing status, app description |
| `image_page.png` | Gallery: up to 2 OCT images with red bbox overlay + confidence/bbox captions |
| `table_page.png` | Per-image results table: filename, ACTIVE/INACTIVE, confidence, bbox coordinates |

### Key design decisions

**Text redaction + re-draw**
The designer's `.ai` has every text span baked in (including sample job
IDs, filenames, and table values). We redact all text from each source
page with `page.add_redact_annot` / `page.apply_redactions` in PyMuPDF,
leaving only the graphical chrome. The generator then redraws every text
element at the exact `(x, y, font, size, color)` triples extracted from
the `.ai` via `page.get_text("dict")` — so the output is visually
identical to what the designer exported.

**Coordinate system**
The designer (and PyMuPDF) uses a top-down origin; ReportLab uses a
bottom-up origin. The helpers `_y(y_topdown)` and `_yb(y_topdown, size)`
handle the flip. `_yb` additionally applies the Montserrat cap-ascent
ratio (`_ASCENT_RATIO = 0.968`) so text baselines land at the designer's
bbox top exactly.

**Verdict card**
ReportLab cannot render horizontal gradients or Gaussian-blurred drop
shadows. We rasterise the card at 4× oversample in PIL (rounded-rect
mask → gradient fill → blurred shadow → composite → downsample with
LANCZOS) and stamp the transparent PNG into the PDF. Red gradient for
ACTIVE (`#e82724 → #b81d20`), blue gradient for INACTIVE
(`#00a3d0 → #0070a8`).

**Table page card shift**
The designer centres the PER-IMAGE RESULTS card vertically (~100 pt gap
below the logo). The product spec pins the card right under the logo.
`build_page_templates.py` post-processes the rendered PNG with PIL:
it crops the inner x-band of the card (skipping the side gradient bars)
within the Y-range `[215, 755] pt` and pastes it back shifted 83 pt
upward, filling the vacated strip with white. `pdf_generator.py` subtracts
the same 83 pt from all card-area y-coords (`_P5_SHIFT = 83.0`).

---

## Packages Used

| Package | Import name | Purpose |
|---|---|---|
| **ReportLab** | `reportlab` | Draw text, stamp images, produce the output PDF |
| **PyMuPDF** | `fitz` | Open the `.ai` source, redact text spans, render 300-DPI template PNGs, render preview PNGs from the output PDF |
| **Pillow** | `PIL` | Rasterise the verdict card (gradient + shadow); annotate OCT images with red bbox + confidence chip; post-process template PNG (table card shift) |
| **Montserrat TTF** | `appway_backend/report/assets/*.ttf` | Designer typeface (ExtraBold, Bold, SemiBold, Regular, Light); registered with ReportLab at runtime |

All Python dependencies are declared in `pyproject.toml` and locked in
`uv.lock`. Install with `uv sync`.

---

## Developer Workflow

### 1 — Rebuild template PNGs (only needed when `.ai` changes)

```bash
uv run python -m appway_backend.report.templates
```

Outputs `appway_backend/report/assets/page_template_{summary,gallery,table}.png`.

### 2 — Iterate on the layout

Edit `appway_backend/report/generator.py`. Coordinate constants live in
the `P0`, `P_GAL`, and `P5` dicts. Font helpers are at the top of the
file. The module is fully self-contained — changes here are immediately
reflected when the production worker calls `generate_epdf_dcm()`.

> **Full procedure** — coordinate system, what to edit for each concern,
> what not to touch, and the visual-comparison workflow — is documented
> in [`docs/pdf-layout.md`](docs/pdf-layout.md).

### 3 — Generate a preview

```bash
uv run python -m appway_backend.report.preview
```

Outputs:
- `pdf_sandbox/outputs/preview.pdf`
- `pdf_sandbox/outputs/previews/verdict_page.png`
- `pdf_sandbox/outputs/previews/image_page.png`
- `pdf_sandbox/outputs/previews/table_page.png`

Switch between `STATIC_JOB` (wireframe placeholders) and `MOCK_JOB`
(realistic data) by editing the import in
`appway_backend/report/preview.py` for different preview modes.

### 4 — Production entry point

```python
# High-level: use the DICOM wrapper (normal production path)
from appway_backend.epdf_generator import generate_epdf_dcm

# Low-level: call the PDF body generator directly (e.g. for tests)
from appway_backend.pdf_report import build_pdf, ReportJob, InputFileInfo, PerImageResult
```

`build_pdf(job: ReportJob, out_path: Path) -> Path`

---

## Project Layout

```
appway_backend/
  pdf_report.py             Public API shim — import build_pdf / dataclasses from here
  epdf_generator.py         DICOM ePDF wrapper (calls pdf_report.build_pdf internally)
  report/
    __init__.py             Re-exports from generator.py
    generator.py            PDF layout engine (build_pdf + P0/P_GAL/P5 coords + helpers)
    templates.py            Rebuild template PNGs from .ai source (reads pdf_sandbox/designer_source/)
    sample_data.py          STATIC_JOB / MOCK_JOB sandbox presets
    preview.py              Sandbox preview runner (python -m appway_backend.report.preview)
    assets/                 Runtime assets — template PNGs + Montserrat TTFs (committed alongside source)
  processor.py              DICOM → PNG extraction + inference orchestration
  worker.py                 Infinite SQS poll loop
  inference.py              YOLO singleton + run_inference()
  config.py                 .env / env-var loader
  s3_utils.py / sqs_utils.py / sns_utils.py
pdf_sandbox/                Layout sandbox (gitignored — kept on disk for reference)
  designer_source/          Designer's original .ai files, .otf font variants, reference PDFs
  outputs/previews/         Last generated preview PNGs (verdict_page, image_page, table_page)
scripts/                    AWS / deployment helpers
```
