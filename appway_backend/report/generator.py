"""
MyopicCNV+ PDF report generator — pixel-faithful overlay implementation.

Approach
════════

The designer's layout lives in ``pdf_assets/Myopic2_ver_b.ai`` (which is
internally a 6-page PDF). Instead of reconstructing the artwork from
scratch in ReportLab — where we were forever chasing gradients,
drop-shadows, and corner radii pixel-by-pixel — we:

  1. **Pre-render** each `.ai` page as a clean high-DPI PNG template
     (done once by ``appway_backend.report.templates``). For gallery pages
     we additionally blank out the baked-in sample OCT image and the
     baked filename/caption strings so only static chrome remains.

  2. **Stamp the template** as the page background in the output PDF.

  3. **Overlay** dynamic per-job text and images at the EXACT
     designer coordinates we extracted from the same `.ai`. Every
     (x, y, font, size, color) value below is a direct copy from the
     parsed text spans — no guessing, no pixel probing.

All coordinates are top-down points (the convention the designer uses).
We convert to ReportLab's bottom-up origin with ``_y(y_topdown)`` at
draw time, and to a centred baseline with ``_yb(y_topdown, size)``.

Fidelity
════════
* Logo, gradient frames, drop-shadows, rounded tabs, footer band → from
  the template PNG, identical to what the designer exports.
* All text → drawn fresh in Montserrat at the designer's exact metrics.
* Gallery images → stamped inside the designer's image-slot rect, with
  a red bbox + confidence chip drawn on top in red (same style as the
  reference).

The only things that are NOT from the `.ai` are the job-specific strings
themselves (job ID, timestamp, verdict colour, filenames, confidences,
bboxes, user-supplied OCT images) — i.e. exactly the content the
generator is supposed to supply.

Editing the layout safely
═════════════════════════
See ``docs/pdf-layout.md`` for the full edit → preview → compare
procedure, including:

  • Which dict to change for each concern (coordinates → P0/P_GAL/P5;
    verdict card → _render_verdict_card_png; gallery bbox → _render_bbox_png)
  • How to use STATIC_JOB vs MOCK_JOB for wireframe vs realistic previews
  • The _y / _yb coordinate-system gotcha and _ASCENT_RATIO
  • When (and when NOT) to rebuild the template PNGs via templates.py
  • Which files to leave untouched (pdf_report.py, epdf_generator.py)

Quick start::

    # edit this file, then:
    uv run python -m appway_backend.report.preview
    # → pdf_sandbox/outputs/previews/{verdict_page,image_page,table_page}.png
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

logger = logging.getLogger(__name__)

# ═════════════════════════════════════════════════════════════════════════
# Constants — page geometry, paths, fonts
# ═════════════════════════════════════════════════════════════════════════

PAGE_W, PAGE_H = A4                              # 595.28 × 841.89 pt

# This module lives at appway_backend/report/generator.py — go up one
# level (.parent) to reach appway_backend/report/, then into assets/.
# The assets/ sub-package is self-contained: template PNGs + TTF fonts
# are shipped alongside the Python source so the production worker needs
# no external pdf_assets/ directory.
PDF_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

TPL_SUMMARY = PDF_ASSETS_DIR / "page_template_summary.png"
TPL_GALLERY = PDF_ASSETS_DIR / "page_template_gallery.png"
TPL_TABLE   = PDF_ASSETS_DIR / "page_template_table.png"
ICON_CHECK  = PDF_ASSETS_DIR / "icon_check.png"   # green circle ✓ (user-provided asset)
ICON_CROSS  = PDF_ASSETS_DIR / "icon_cross.png"   # red circle ✗  (user-provided asset)

# Montserrat weights — registered lazily on first call to build_pdf().
FONT_EXTRABOLD = "Montserrat-ExtraBold"
FONT_BOLD      = "Montserrat-Bold"
FONT_SEMIBOLD  = "Montserrat-SemiBold"
FONT_REGULAR   = "Montserrat-Regular"
FONT_LIGHT     = "Montserrat-Light"

_FONTS_REGISTERED = False
_HELVETICA_FALLBACK = {
    FONT_EXTRABOLD: "Helvetica-Bold",
    FONT_BOLD:      "Helvetica-Bold",
    FONT_SEMIBOLD:  "Helvetica-Bold",
    FONT_REGULAR:   "Helvetica",
    FONT_LIGHT:     "Helvetica",
}


def _register_fonts() -> None:
    """Register Montserrat weights (idempotent). Falls back to Helvetica
    if a given weight's .ttf cannot be loaded."""
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    weights = [
        (FONT_EXTRABOLD, ["Montserrat-ExtraBold.ttf"]),
        (FONT_BOLD,      ["Montserrat-Bold.ttf"]),
        (FONT_SEMIBOLD,  ["Montserrat-SemiBold.ttf"]),
        (FONT_REGULAR,   ["Montserrat-Regular.ttf"]),
        (FONT_LIGHT,     ["Montserrat-Light.ttf"]),
    ]
    for logical, candidates in weights:
        ok = False
        for fname in candidates:
            p = PDF_ASSETS_DIR / fname
            if p.is_file():
                try:
                    pdfmetrics.registerFont(TTFont(logical, str(p)))
                    ok = True
                    break
                except Exception as e:
                    logger.warning("Font %s: %s", fname, e)
        if not ok:
            globals()[_const_name_for(logical)] = _HELVETICA_FALLBACK[logical]
    _FONTS_REGISTERED = True


def _const_name_for(logical: str) -> str:
    return {
        "Montserrat-ExtraBold": "FONT_EXTRABOLD",
        "Montserrat-Bold":      "FONT_BOLD",
        "Montserrat-SemiBold":  "FONT_SEMIBOLD",
        "Montserrat-Regular":   "FONT_REGULAR",
        "Montserrat-Light":     "FONT_LIGHT",
    }[logical]


# ═════════════════════════════════════════════════════════════════════════
# Data model
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class InputFileInfo:
    filename: str
    modality: str = ""
    study_description: str = ""
    series_description: str = ""
    frames: int = 1
    rows: int = 0
    columns: int = 0


@dataclass
class PerImageResult:
    filename: str
    pred: int                                  # 1 = active, 0 = inactive
    conf: Optional[float] = None
    bbox: list = field(default_factory=list)   # [x1, y1, x2, y2] in src px
    image_path: Optional[Path] = None


@dataclass
class ReportJob:
    job_id: str
    processed_at: datetime
    software_version: str
    input_files: list[InputFileInfo]
    verdict: str                                # "Positive" | "Negative"
    processing_time: float
    per_image: list[PerImageResult]
    app_description: Optional[str] = None


# ═════════════════════════════════════════════════════════════════════════
# Coord helpers — all designer coords are TOP-DOWN pt.
# ReportLab's origin is bottom-left, so we flip y.
# ═════════════════════════════════════════════════════════════════════════

def _y(y_topdown: float) -> float:
    """Flip a top-down y coordinate to ReportLab's bottom-up."""
    return PAGE_H - y_topdown


# Montserrat's cap-ascent-to-size ratio: PyMuPDF reports text bbox
# y0 at the top of the cap-height box, which sits ~0.968 × font_size
# above the baseline for Montserrat (empirically measured by re-parsing
# our own output and matching designer span bboxes to within <0.05 pt).
# Using this exact ratio in ``_yb`` makes our rendered spans land with
# the same bbox y0 as the designer's PDF — i.e. pixel-faithful
# vertical alignment with no per-size fudge factors.
_ASCENT_RATIO = 0.968


def _yb(y_topdown: float, font_size: float) -> float:
    """Convert a designer-style "text top y" in top-down coords to
    ReportLab's drawString baseline (bottom-up).

    PyMuPDF reports a text span's bbox with ``y0`` = top of the cap-
    height box. ReportLab draws from the baseline. For Montserrat the
    baseline sits ``_ASCENT_RATIO × font_size`` (~0.968 s) below the
    bbox top, so we subtract that to convert designer's y0 to a
    baseline in ReportLab's bottom-up coords.
    """
    return PAGE_H - y_topdown - font_size * _ASCENT_RATIO


def _draw_template(c: rl_canvas.Canvas, path: Path) -> None:
    """Stamp a full-page template PNG at (0, 0) covering the whole A4."""
    if not path.is_file():
        logger.warning("Template not found: %s", path)
        return
    try:
        from reportlab.lib.utils import ImageReader
        c.drawImage(
            ImageReader(str(path)), 0, 0,
            width=PAGE_W, height=PAGE_H,
            preserveAspectRatio=False, mask=None,
        )
    except Exception:
        logger.exception("Could not stamp template %s", path)


def _text(
    c: rl_canvas.Canvas,
    x_topdown: float, y_topdown: float,
    text: str, font: str, size: float,
    hex_color: str,
) -> None:
    """Draw a single text string at the designer's (x, y_top) in the
    given Montserrat weight, size, and hex color."""
    c.setFont(font, size)
    c.setFillColor(colors.HexColor(hex_color))
    c.drawString(x_topdown, _yb(y_topdown, size), text)


def _text_right(
    c: rl_canvas.Canvas,
    x_right_topdown: float, y_topdown: float,
    text: str, font: str, size: float,
    hex_color: str,
) -> None:
    """Draw text right-aligned at ``x_right_topdown``."""
    c.setFont(font, size)
    c.setFillColor(colors.HexColor(hex_color))
    w = pdfmetrics.stringWidth(text, font, size)
    c.drawString(x_right_topdown - w, _yb(y_topdown, size), text)


def _text_centered(
    c: rl_canvas.Canvas,
    x_center_topdown: float, y_topdown: float,
    text: str, font: str, size: float,
    hex_color: str,
) -> None:
    c.setFont(font, size)
    c.setFillColor(colors.HexColor(hex_color))
    c.drawCentredString(x_center_topdown, _yb(y_topdown, size), text)


# ═════════════════════════════════════════════════════════════════════════
# Designer coordinates (all top-down pt, all from Myopic2_ver_b.ai)
# ═════════════════════════════════════════════════════════════════════════
#
# ── Palette (hex) ──
C_HEADING_BLUE   = "#0088bf"   # all blue headings and highlight labels
C_BODY_GREY      = "#3a3e40"   # body text (near-black grey)
C_BODY_DARK      = "#404041"   # gallery caption "AI confidence:" / "· bbox:"
C_WHITE          = "#ffffff"
C_BLACK          = "#000000"
# Verdict card — the designer uses a HORIZONTAL gradient on the card
# body (lighter on the left, darker on the right). Pixel-sampled from
# the `.ai` at x=312 → x=552:
#   positive:  #e82724  →  #b81d20
#   negative:  #00a3d0  →  #0070a8   (approximate: blue band gradient)
# These two-stop values are used by ``_draw_verdict_card()`` via
# ``canvas.linearGradient`` for a true gradient fill rather than a flat
# colour.
C_VERDICT_POS_L  = "#e82724"
C_VERDICT_POS_R  = "#b81d20"
C_VERDICT_NEG_L  = "#4CAF50"   # material green — INACTIVE (left/lighter stop)
C_VERDICT_NEG_R  = "#2E7D32"   # material green — INACTIVE (right/darker stop)
# Legacy flat fills (kept for reference / fallback)
C_VERDICT_POS    = "#d02523"   # red for ACTIVE (mid-gradient tone)
C_VERDICT_NEG    = "#43A047"   # green for INACTIVE (mid-gradient tone)
# Designer's teal tab colour (for bleed-over overlays that need it)
C_TAB_TEAL       = "#00afbd"

# ── PAGE 0: Summary ──
# All numbers extracted verbatim from parsed text spans & filled rects.
P0 = dict(
    report_title = dict(x=37.91, y=202.61, size=20.75, color=C_HEADING_BLUE),
    # Left column — JOB INFORMATION
    job_info_heading = dict(x=49.91, y=265.17, size=10, color=C_BODY_GREY),
    job_id_label     = dict(x=49.91, y=292.50, size=10, color=C_HEADING_BLUE),
    job_id_val_x     = 93.38,   # +7 pt gap after "Job ID:"
    processed_label  = dict(x=49.91, y=307.50, size=10, color=C_HEADING_BLUE),
    processed_val_x  = 113.85,  # +7 pt gap after "Processed:"
    software_label   = dict(x=49.91, y=322.50, size=10, color=C_HEADING_BLUE),
    software_val_x   = 108.57,  # +7 pt gap after "Software:"
    # Right column — AI ANALYSIS RESULT
    ai_heading       = dict(x=297.63, y=265.17, size=10, color=C_BODY_GREY),
    patient_verdict_label = dict(x=424.84, y=265.16, size=10, color=C_HEADING_BLUE),
    # Verdict card — pymupdf reports the card's IMAGE RECT as
    # (284.43, 274.29, 577.47, 372.45) with an inner body rect of
    # (291.97, 281.83, 569.65, 364.87), but the actual VISIBLE pixels
    # of the red card occupy a tighter rect pixel-sampled from the
    # 120-DPI render: x ∈ [297.54, 557.29], y ∈ [287.30, 352.70].
    # The designer centres the title/subline not on that visible rect
    # centre (≈427.42) but on the slightly-offset OPTICAL centre at
    # x=430.28 pt (extracted verbatim from the designer's text-span
    # bbox: x0=360.41 + width/2 for the 13-pt "Myopic CNV: ACTIVE").
    verdict_title    = dict(x_center=430.28, y=307.83, size=13, color=C_WHITE),
    verdict_subline  = dict(x_center=430.28, y=324.21, size=8,  color=C_WHITE),
    verdict_rect_shadow = (284.43, 274.29, 577.47, 372.45),  # outer (shadow)
    verdict_rect_body   = (297.54, 287.30, 557.29, 352.70),  # visible body
    verdict_radius   = 6.6,                                  # sampled corner
    # Second row — INPUT FILES (left) / PROCESSING STATUS (right)
    input_heading       = dict(x=44.35, y=394.01, size=10, color=C_BODY_GREY),
    input_row_first_y   = 416.00,              # filename baseline
    input_row_details1  = 431.06,              # first details line
    input_row_details2  = 446.06,              # second details line (wrap)
    status_heading      = dict(x=297.54, y=394.01, size=10, color=C_BODY_GREY),
    # Processing status rows — left-aligned with the verdict card's left
    # border (x = 297.54 pt).  We draw our own green check circles in
    # code and white-out the baked-in circles from the template PNG.
    status_check1_y     = 416.53,
    status_check2_y     = 437.53,
    status_circle_x     = 297.54,              # left edge of green circle
    status_circle_r     = 5.0,                 # circle radius pt
    status_text_x       = 297.54 + 5.0*2 + 3, # text starts right of circle + 3 pt gap
    # Erase zone — white rect that covers the OLD baked check glyphs so
    # the shifted circles don't overlap them.
    status_erase_x0     = 340.0,
    status_erase_x1     = 378.0,
    status_erase_y0     = 408.0,               # just above first check row
    status_erase_y1     = 450.0,               # just below second check row
    # APP DESCRIPTION band (designer: 37.75,528.33 → 558.25,768.63)
    app_heading = dict(x=49.87, y=537.77, size=11, color=C_HEADING_BLUE),
    app_body_x  = 49.87,
    app_body_y0 = 565.81,                      # first body line y
    app_line_stride = 15,                      # line stride pt
    app_body_width = 500,                      # wrap width
    # Footer
    footer_y = 815.80,
)

# ── PAGE 1: Gallery slot geometry (top-down pt) ──
# Image slot rects, caption rows, tab text coords — all from .ai page 1.
P_GAL = dict(
    # Image display rects (xref 41 in the .ai), top-down
    img_slot_top = (97.12, 169.68, 499.33, 407.50),
    img_slot_bot = (97.12, 501.19, 499.33, 739.01),
    # Tab filename text anchor (top tab y=141.57, bottom y=473.08)
    tab_text_top = dict(x_center=420.0, y=141.57, size=10, color=C_WHITE),
    tab_text_bot = dict(x_center=420.0, y=473.08, size=10, color=C_WHITE),
    # Caption rows
    caption_top = dict(y=429.87, size=10),
    caption_bot = dict(y=761.38, size=10),
    # Caption text uses the 4-chunk layout from the .ai:
    #   "AI confidence:"  (Regular, #404041, x=212.00)
    #   "71.5 %"          (Bold,    #0088bf, x=285.32)
    #   "· bbox:"         (Regular, #404041, x=316.31)
    #   "x1=689 y1=128 x2=863 y2=379"  (Bold, #0088bf, x=356.26)
    cap_x_label_confidence = 212.00,
    cap_x_confidence_value = 285.32,
    cap_x_label_bbox       = 316.31,
    cap_x_bbox_value       = 356.26,
    # Footer
    footer_y = 815.80,
)

# ── PAGE 5: Table ──
# Columns, rows, heading all from .ai page 5.
#
# All (x, y, size) values below are lifted verbatim from parsed text
# spans on the designer's ``Myopic2 copia.pdf`` table page (which is
# identical in geometry to page 5 of ``Myopic2_ver_b.ai``). We use
# those values with NO offset so the generator's output lines up
# pixel-for-pixel with the designer.
# NOTE: We deliberately diverge from the designer's layout on this page.
# The `.ai` centres the PER-IMAGE RESULTS card vertically (with ~100 pt
# of empty space between the logo and the card's teal tab), but the
# product spec wants the card pinned right underneath the logo.
# ``appway_backend.report.templates`` shifts the card band upward by
# ``TABLE_SHIFT_UP_PT`` (= 83 pt) in the rendered template PNG, so we
# subtract the same 83 pt from every y-coord that lands ON the card
# (tab heading, column headers, data rows). The footer is OUTSIDE the
# shifted band, so ``footer_y`` keeps its original value.
_P5_SHIFT = 83.0
P5 = dict(
    # Teal tab heading "PER-IMAGE RESULTS" — the teal tab shape is
    # baked into the template PNG but the TEXT is redacted, so we
    # overlay it fresh. Designer y=234.07 pt minus the 83 pt card-up
    # shift = 151.07 pt.
    tab_heading_x = 64.05,
    tab_heading_y = 234.07 - _P5_SHIFT,
    tab_heading_size = 11.87,
    # Column header row (designer y=280.12 pt, shifted up by 83 pt)
    col_heading_y = 280.12 - _P5_SHIFT,
    col_heading_size = 9,
    col_x_image  = 74.47,
    col_x_result = 228.28,
    col_x_conf   = 309.72,
    col_x_bbox   = 400.12,
    # First data row (designer y=303.15 for filename, shifted up by 83
    # pt), row stride 25.82 pt
    row_y0 = 303.15 - _P5_SHIFT,
    row_stride = 25.815,
    row_size = 8,
    # Bounding-box two-line offsets — designer layout puts "x1=… y1=…"
    # slightly above row centre and "x2=… y2=…" slightly below. Shifted
    # up by ~2pt so the two-line block sits vertically centred in the row.
    bbox_line1_y_offset = -8.50,
    bbox_line2_y_offset =  1.10,
    # Em-dash glyph used for INACTIVE rows in the BOUNDING BOX column
    # is set at 7.5 pt in the designer (slightly smaller than the 8 pt
    # row text). Other em-dashes in CONFIDENCE stay at 8 pt.
    inactive_dash_size_bbox = 7.5,
    # ── Bounded-table geometry (pixel-sampled from page_template_table.png) ──
    # The card chrome (rounded white body + drop shadow) is baked into the
    # template PNG. We sample its INNER white rectangle here so row-tint
    # fills are clipped precisely to the card's visible body — nothing
    # escapes the card frame regardless of row count or stride math.
    #
    # Sampled at 300 DPI (2481×3508 px) from the post-shifted template:
    #   left:   px≈267  → pt≈64.1   right: px≈2286 → pt≈548.5
    #   top:    py≈785  → pt≈188.4  (first white row below teal tab)
    #   bottom: py≈2038 → pt≈489.1  (last white row before footer grad.)
    card_inner_left   = 64.0,
    card_inner_right  = 522.0,   # inner edge of right card frame (pt=523.06 from probe)
    card_inner_top    = 188.0,    # top of white card body (below teal tab)
    card_inner_bottom = 489.0,    # bottom of white card body (above footer)
    # Column-header band height inside the card (pt). This band sits at
    # the top of the inner area and contains the COLUMN HEADER row with a
    # light-grey background tint so it reads as a distinct header.
    tbl_header_h = 22.0,
    # Footer — the designer *right-aligns* the string so its RIGHT edge
    # sits at x=563.28 pt (extracted verbatim from the table page's
    # footer span bbox x1). The left edge varies with page-number digit
    # width while the right edge stays fixed at ~563.28 pt, a little
    # inside the dark-blue ribbon's right end. Uses a single-space
    # " · " separator (not two spaces).
    footer_x_right = 563.28,
    footer_y = 815.80,
)


# ═════════════════════════════════════════════════════════════════════════
# Text-wrapping helper (for APP DESCRIPTION body)
# ═════════════════════════════════════════════════════════════════════════

def _wrap_lines(text: str, font: str, size: float, width: float) -> list[str]:
    """Simple word-wrap → list of lines fitting within ``width`` pt."""
    words = text.split()
    out, line = [], ""
    for w in words:
        test = (line + " " + w).strip()
        if pdfmetrics.stringWidth(test, font, size) <= width:
            line = test
        else:
            if line:
                out.append(line)
            line = w
    if line:
        out.append(line)
    return out


def _wrap_text_at_chars(
    text: str, font: str, size: float, max_width: float,
    break_chars: str = "-_.",
) -> list[str]:
    """Wrap a single token (e.g. a UUID or filename) at preferred break
    characters so lines stay within ``max_width`` pt.

    Prefers splitting just *after* a character in ``break_chars`` so the
    natural delimiters (hyphens in UUIDs, underscores in filenames) sit at
    the end of the first line rather than the start of the second.  Falls
    back to a hard mid-char split if no preferred break point fits.

    Returns a list of at most 2 lines (any excess is silently dropped since
    the layout only has room for 2 lines in these slots).
    """
    if pdfmetrics.stringWidth(text, font, size) <= max_width:
        return [text]

    # Try to split after a preferred break character, scanning right-to-left
    # so we take the longest first line that still fits.
    best_split = -1
    for i, ch in enumerate(text):
        if ch in break_chars:
            candidate = text[: i + 1]
            if pdfmetrics.stringWidth(candidate, font, size) <= max_width:
                best_split = i + 1   # split *after* the break char

    if best_split > 0:
        line1 = text[:best_split]
        line2 = text[best_split:]
    else:
        # Hard-split: binary-search for the longest prefix that fits.
        lo, hi = 1, len(text) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if pdfmetrics.stringWidth(text[:mid], font, size) <= max_width:
                lo = mid
            else:
                hi = mid - 1
        line1 = text[:lo]
        line2 = text[lo:]

    return [line1, line2] if line2 else [line1]


# ═════════════════════════════════════════════════════════════════════════
# Gallery image rendering — bbox + confidence chip
# ═════════════════════════════════════════════════════════════════════════

def _render_bbox_png(
    src_path: Path, bbox: list, conf: Optional[float],
) -> Optional[bytes]:
    """Load source PNG, draw red bbox + red confidence chip, return PNG."""
    try:
        from PIL import Image as PILImage, ImageDraw, ImageFont
    except Exception:
        return None
    try:
        img = PILImage.open(str(src_path)).convert("RGB")
    except Exception:
        return None
    if bbox and len(bbox) == 4 and not all(v == 0 for v in bbox):
        draw = ImageDraw.Draw(img)
        x1, y1, x2, y2 = [int(round(v)) for v in bbox]
        W, H = img.size
        x1, x2 = max(0, min(x1, W - 1)), max(0, min(x2, W - 1))
        y1, y2 = max(0, min(y1, H - 1)), max(0, min(y2, H - 1))
        if x2 > x1 and y2 > y1:
            draw.rectangle([(x1, y1), (x2, y2)], outline=(220, 53, 69), width=6)
            if isinstance(conf, (int, float)):
                label = f"{conf * 100:.1f}%"
                font_sz = max(14, min(40, W // 40))
                font = None
                for cand in ("Montserrat-Bold.ttf",):
                    fp = PDF_ASSETS_DIR / cand
                    if fp.is_file():
                        try:
                            font = ImageFont.truetype(str(fp), font_sz)
                            break
                        except Exception:
                            pass
                if font is None:
                    font = ImageFont.load_default()
                try:
                    tb = draw.textbbox((0, 0), label, font=font)
                    tw, th = tb[2] - tb[0], tb[3] - tb[1]
                except Exception:
                    tw, th = font_sz * 3, font_sz
                pad = max(4, font_sz // 4)
                cx1, cy1 = x1, y1 - th - 2 * pad - 2
                if cy1 < 0:
                    cy1 = y1 + 2
                cx2, cy2 = cx1 + tw + 2 * pad, cy1 + th + 2 * pad
                draw.rectangle([(cx1, cy1), (cx2, cy2)], fill=(220, 53, 69))
                draw.text((cx1 + pad, cy1 + pad - 1), label,
                          fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_image_slot(
    c: rl_canvas.Canvas, rect_topdown: tuple, item: PerImageResult,
) -> None:
    """Stamp an annotated OCT image inside the designer's image slot rect.
    Falls back to a grey placeholder if no file is on disk."""
    x0, y0, x1, y1 = rect_topdown
    slot_w = x1 - x0
    slot_h = y1 - y0
    rl_y_bot = _y(y1)   # bottom-up y of slot's bottom edge

    if item.image_path and Path(item.image_path).is_file():
        png = _render_bbox_png(Path(item.image_path), item.bbox, item.conf)
        if png:
            from reportlab.lib.utils import ImageReader
            c.drawImage(
                ImageReader(io.BytesIO(png)),
                x0, rl_y_bot, width=slot_w, height=slot_h,
                preserveAspectRatio=False, mask="auto",
            )
            return

    # Placeholder
    c.setFillColor(colors.HexColor("#f4f6f9"))
    c.setStrokeColor(colors.HexColor("#cccccc"))
    c.setLineWidth(0.5)
    c.rect(x0, rl_y_bot, slot_w, slot_h, stroke=1, fill=1)
    _text_centered(
        c, x0 + slot_w / 2, y0 + slot_h / 2 + 3,
        f"[sample image not on disk: {item.filename}]",
        FONT_REGULAR, 9, "#888888",
    )


# ═════════════════════════════════════════════════════════════════════════
# Verdict card — high-DPI PIL rasteriser
# ═════════════════════════════════════════════════════════════════════════
#
# ReportLab can't do linear gradients or gaussian-blurred drop shadows,
# and the designer's `.ai` uses both for the verdict card. So we raster-
# render the card at high DPI in PIL (rounded rect + horizontal gradient
# + soft shadow) and stamp that raster into the PDF. The output is a
# transparent PNG sized `(body_w + 2*pad)` × `(body_h + 2*pad)` points,
# where `pad` = ``_VERDICT_SHADOW_PAD``: enough margin around the body
# for the shadow to bloom without clipping.

_VERDICT_SHADOW_PAD = 12.0   # pt: margin for shadow bloom on each side


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    s = hex_str.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _render_verdict_card_png(
    width_pt: float,
    height_pt: float,
    radius_pt: float,
    hex_left: str,
    hex_right: str,
) -> Optional[bytes]:
    """Render the verdict card as a transparent PNG.

    The output is sized to fit the body plus a uniform margin of
    ``_VERDICT_SHADOW_PAD`` pt on every side so the gaussian shadow has
    room to bloom. Caller must stamp it offset by ``-pad`` in both axes
    so the body ends up at the intended body rect.

    Pipeline:
      1. Build a rounded-rect alpha mask at 4× oversample.
      2. Fill a horizontal-gradient layer (left→right hex interpolation).
      3. Build a shadow layer = same alpha mask, solid near-black, offset
         slightly down+right, then gaussian-blurred.
      4. Composite shadow under the gradient body.
      5. Downsample to the final size for crisp antialiasing.
    """
    try:
        from PIL import Image as PILImage, ImageDraw, ImageFilter
    except Exception:
        return None

    pad_pt = _VERDICT_SHADOW_PAD
    # 4× oversample relative to 120 DPI preview, crisp at any zoom.
    OVER = 4
    px_per_pt = (120.0 / 72.0) * OVER   # ~6.667 px/pt

    def _pt(v: float) -> int:
        return int(round(v * px_per_pt))

    W = _pt(width_pt + 2 * pad_pt)
    H = _pt(height_pt + 2 * pad_pt)
    body_x = _pt(pad_pt)
    body_y = _pt(pad_pt)
    body_w = _pt(width_pt)
    body_h = _pt(height_pt)
    body_r = _pt(radius_pt)

    # ── 1. Rounded-rect alpha mask for the body
    mask = PILImage.new("L", (W, H), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle(
        (body_x, body_y, body_x + body_w, body_y + body_h),
        radius=body_r, fill=255,
    )

    # ── 2. Horizontal gradient fill (left → right)
    lr, lg, lb = _hex_to_rgb(hex_left)
    rr, rg, rb = _hex_to_rgb(hex_right)
    grad = PILImage.new("RGB", (body_w, 1), 0)
    gpix = grad.load()
    if body_w > 0:
        for x in range(body_w):
            t = x / max(1, body_w - 1)
            gpix[x, 0] = (
                int(round(lr + (rr - lr) * t)),
                int(round(lg + (rg - lg) * t)),
                int(round(lb + (rb - lb) * t)),
            )
        grad = grad.resize((body_w, body_h), PILImage.NEAREST)
    # Paste gradient into a full-canvas RGBA layer using the body mask.
    body_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    body_rgba = PILImage.new("RGBA", (body_w, body_h), (0, 0, 0, 0))
    body_rgba.paste(grad, (0, 0))
    body_mask_crop = mask.crop((body_x, body_y, body_x + body_w, body_y + body_h))
    body_rgba.putalpha(body_mask_crop)
    body_layer.paste(body_rgba, (body_x, body_y), body_rgba)

    # ── 3. Shadow layer: same mask, solid dark, offset + blurred.
    shadow_offset_x = _pt(1.5)   # 1.5pt right
    shadow_offset_y = _pt(2.5)   # 2.5pt down (stronger downward falloff)
    blur_radius_px  = _pt(4.0)   # 4pt blur

    shadow = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        (body_x + shadow_offset_x,
         body_y + shadow_offset_y,
         body_x + shadow_offset_x + body_w,
         body_y + shadow_offset_y + body_h),
        radius=body_r,
        fill=(0, 0, 0, 90),   # ~35% black
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=blur_radius_px))

    # ── 4. Composite: shadow first, then body on top.
    out = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    out = PILImage.alpha_composite(out, shadow)
    out = PILImage.alpha_composite(out, body_layer)

    # ── 5. Downsample from 4× oversample to 1× (still at 120 DPI)
    final_W = max(1, W // OVER)
    final_H = max(1, H // OVER)
    out = out.resize((final_W, final_H), PILImage.LANCZOS)

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# Shadow padding for the table card PNG: extra transparent margin around
# the card body so the gaussian shadow has room to bloom without clipping.
_TABLE_CARD_SHADOW_PAD = 16.0   # pt

# Designer's card gradient — pixel-sampled from the baked template PNG:
#   top = RGB(0, 176, 189) = #00B0BD  — exact baked tab/frame color (seamless join)
#   bottom = from the .ai photo bottom edge of the card frame ≈ dark blue-teal
# The page-side gradient runs from #00B0BD (top) → #0089C0 (bottom) over the full
# page height. Over the card's ~300pt span (168→470pt on 841pt page), the expected
# gradient shift is approximately: #00B0BD → #0099C2 (mid-card teal→blue).
# We use a more dramatic version to make it visually clear (matching .ai photo).
_CARD_GRAD_TOP    = "#00B0BD"   # exact baked frame top color → zero gap seam
_CARD_GRAD_BOTTOM = "#006FA0"   # visible dark teal-blue at bottom ring

# The outer frame TOP is pinned to the card frame top as measured in the
# baked template: 168.23pt (pixel-sampled at x=300pt in page_template_table.png).
# This MUST be kept in sync with _CARD_OUTER_TOP so the rendered frame
# sits flush against the baked teal tab with no visible gap.
_CARD_OUTER_TOP = 168.0   # pt: top of outer frame (below tab bottom, at x=300pt)


def _render_table_card_png(
    outer_w_pt: float,
    outer_h_pt: float,
    white_l_pt: float,   # distance from outer left  → inner white left
    white_r_pt: float,   # distance from outer right → inner white right
    white_t_pt: float,   # distance from outer top   → inner white top
    white_b_pt: float,   # distance from outer bottom→ inner white bottom
    radius_pt: float,
) -> Optional[bytes]:
    """Render the table card as a transparent PNG at 4× oversample.

    The outer rect is the full teal gradient rounded frame.
    The inner white rect is positioned at the exact offsets given by
    white_l/r/t/b_pt — these can be asymmetric (e.g. the left frame ring
    is thinner than the right because the teal tab overhangs the top-left).

    The PNG is padded by ``_TABLE_CARD_SHADOW_PAD`` pt on every side so
    the gaussian shadow blooms freely. Caller must stamp it at
    ``(outer_left - pad, outer_bottom_rl - pad)`` with total size
    ``(outer_w + 2·pad, outer_h + 2·pad)``.

    Pipeline:
      1. Top-to-bottom gradient outer rounded rect.
      2. Gaussian drop shadow (behind the gradient).
      3. White inner rounded rect at exact offsets.
      4. Downsample LANCZOS 4→1.
    """
    try:
        from PIL import Image as PILImage, ImageDraw, ImageFilter
    except Exception:
        return None

    pad_pt = _TABLE_CARD_SHADOW_PAD
    OVER = 4
    ppp = (120.0 / 72.0) * OVER   # ~6.667 px/pt

    def _px(v: float) -> int:
        return int(round(v * ppp))

    W  = _px(outer_w_pt + 2 * pad_pt)
    H  = _px(outer_h_pt + 2 * pad_pt)
    ox = _px(pad_pt)          # outer rect origin x in canvas
    oy = _px(pad_pt)          # outer rect origin y in canvas
    ow = _px(outer_w_pt)
    oh = _px(outer_h_pt)
    br = _px(radius_pt)

    # inner white rect (in canvas coords)
    ix0 = ox + _px(white_l_pt)
    iy0 = oy + _px(white_t_pt)
    ix1 = ox + ow - _px(white_r_pt)
    iy1 = oy + oh - _px(white_b_pt)
    # inner corner radius — shrink by average inset
    avg_inset = _px((white_l_pt + white_r_pt + white_t_pt + white_b_pt) / 4)
    inner_r = max(0, br - avg_inset)

    # ── 1. Outer rounded-rect alpha mask
    mask = PILImage.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (ox, oy, ox + ow, oy + oh), radius=br, fill=255,
    )

    # ── 2. Drop shadow
    sdx   = _px(_CARD_SHADOW_DX)
    sdy   = _px(_CARD_SHADOW_DY)
    sblur = _px(_CARD_SHADOW_BLUR)
    shadow = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (ox + sdx, oy + sdy, ox + sdx + ow, oy + sdy + oh),
        radius=br,
        fill=(0, 0, 0, int(255 * _CARD_SHADOW_ALPHA)),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=sblur))

    # ── 3. Top-to-bottom gradient fill, masked to rounded outer rect
    tr, tg, tb_   = _hex_to_rgb(_CARD_GRAD_TOP)
    dr, dg, db    = _hex_to_rgb(_CARD_GRAD_BOTTOM)
    grad_strip = PILImage.new("RGB", (1, oh), 0)
    gp = grad_strip.load()
    for row in range(oh):
        t = row / max(1, oh - 1)
        gp[0, row] = (
            int(tr + (dr - tr) * t),
            int(tg + (dg - tg) * t),
            int(tb_ + (db - tb_) * t),
        )
    grad = grad_strip.resize((ow, oh), PILImage.NEAREST)

    grad_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    grad_rgba  = PILImage.new("RGBA", (ow, oh), (0, 0, 0, 0))
    grad_rgba.paste(grad, (0, 0))
    grad_rgba.putalpha(mask.crop((ox, oy, ox + ow, oy + oh)))
    grad_layer.paste(grad_rgba, (ox, oy), grad_rgba)

    # ── 4. White inner rect
    white_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(white_layer).rounded_rectangle(
        (ix0, iy0, ix1, iy1),
        radius=inner_r,
        fill=(255, 255, 255, 255),
    )

    # ── 5. Composite: shadow → gradient → white
    out = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    out = PILImage.alpha_composite(out, shadow)
    out = PILImage.alpha_composite(out, grad_layer)
    out = PILImage.alpha_composite(out, white_layer)

    # ── 6. Downsample 4→1 (LANCZOS)
    out = out.resize((max(1, W // OVER), max(1, H // OVER)), PILImage.LANCZOS)

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# ═════════════════════════════════════════════════════════════════════════
# Page 0 — Summary
# ═════════════════════════════════════════════════════════════════════════

def _draw_page0_summary(c: rl_canvas.Canvas, job: ReportJob, total: int) -> None:
    _draw_template(c, TPL_SUMMARY)

    # ── Report title ──
    _text(c, P0["report_title"]["x"], P0["report_title"]["y"],
          "ANALYSIS RESULT REPORT",
          FONT_EXTRABOLD, P0["report_title"]["size"],
          P0["report_title"]["color"])

    # ── Left column: JOB INFORMATION ──
    _text(c, P0["job_info_heading"]["x"], P0["job_info_heading"]["y"],
          "JOB INFORMATION", FONT_SEMIBOLD, 10, C_BODY_GREY)
    _text(c, P0["job_id_label"]["x"], P0["job_id_label"]["y"],
          "Job ID:", FONT_BOLD, 10, C_HEADING_BLUE)

    # Job ID: wrap long UUIDs so they stay inside the card.
    # Available width = right card edge (~240 pt) minus val_x start.
    _JOB_ID_MAX_W  = 155.0   # pt: max width for Job ID value
    _JOB_ID_STRIDE = 13.0    # pt: line stride when Job ID wraps
    job_id_lines = _wrap_text_at_chars(
        job.job_id, FONT_REGULAR, 10, _JOB_ID_MAX_W, break_chars="-_."
    )
    for _li, _jline in enumerate(job_id_lines[:2]):
        _text(c, P0["job_id_val_x"],
              P0["job_id_label"]["y"] + 0.06 + _li * _JOB_ID_STRIDE,
              _jline, FONT_REGULAR, 10, C_BODY_GREY)
    # Shift subsequent labels down only if Job ID wrapped to 2 lines.
    _job_id_extra = (_JOB_ID_STRIDE if len(job_id_lines) > 1 else 0)

    _text(c, P0["processed_label"]["x"],
          P0["processed_label"]["y"] + _job_id_extra,
          "Processed:", FONT_BOLD, 10, C_HEADING_BLUE)
    _text(c, P0["processed_val_x"],
          P0["processed_label"]["y"] + 0.06 + _job_id_extra,
          job.processed_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
          FONT_REGULAR, 10, C_BODY_GREY)
    _text(c, P0["software_label"]["x"],
          P0["software_label"]["y"] + _job_id_extra,
          "Software:", FONT_BOLD, 10, C_HEADING_BLUE)
    _text(c, P0["software_val_x"],
          P0["software_label"]["y"] + 0.06 + _job_id_extra,
          f"MyopicCNV+ v{job.software_version}",
          FONT_REGULAR, 10, C_BODY_GREY)

    # ── Right column: AI ANALYSIS RESULT ──
    _text(c, P0["ai_heading"]["x"], P0["ai_heading"]["y"],
          "AI ANALYSIS RESULT", FONT_SEMIBOLD, 10, C_BODY_GREY)
    # Verdict card — we render it as a high-DPI PIL raster (rounded
    # rect + horizontal gradient + gaussian-blurred drop-shadow) and
    # stamp that raster into the PDF. ReportLab can't gradient-fill or
    # blur shadows, and the designer's `.ai` embeds an equivalent
    # raster, so this is the faithful way to reproduce it. Red for
    # positive, blue for negative (both match the designer palette).
    is_pos = job.verdict.strip().lower() == "positive"
    bx0, by0, bx1, by1 = P0["verdict_rect_body"]
    bw, bh = bx1 - bx0, by1 - by0
    card_png = _render_verdict_card_png(
        bw, bh, P0["verdict_radius"],
        C_VERDICT_POS_L if is_pos else C_VERDICT_NEG_L,
        C_VERDICT_POS_R if is_pos else C_VERDICT_NEG_R,
    )
    if card_png is not None:
        from reportlab.lib.utils import ImageReader
        # The rasterised card includes a 12 pt margin around the body
        # to fit the shadow; we stamp it shifted so the body aligns.
        pad = _VERDICT_SHADOW_PAD
        c.drawImage(
            ImageReader(io.BytesIO(card_png)),
            bx0 - pad, _y(by1) - pad,
            width=bw + 2 * pad, height=bh + 2 * pad,
            preserveAspectRatio=False, mask="auto",
        )
    else:
        # Fallback: flat fill if PIL unavailable.
        c.setFillColor(colors.HexColor(C_VERDICT_POS if is_pos else C_VERDICT_NEG))
        c.setStrokeColor(colors.HexColor(C_VERDICT_POS if is_pos else C_VERDICT_NEG))
        c.roundRect(bx0, _y(by1), bw, bh,
                    P0["verdict_radius"], stroke=0, fill=1)

    vtitle = f"Myopic CNV: {'ACTIVE' if is_pos else 'INACTIVE'}"
    _text_centered(c, P0["verdict_title"]["x_center"],
                   P0["verdict_title"]["y"],
                   vtitle, FONT_BOLD, 13, C_WHITE)
    n_pos = sum(1 for x in job.per_image if x.pred == 1)
    n_tot = len(job.per_image)
    sub = (
        f"{n_pos} of {n_tot} images flagged "
        f"{'Active' if is_pos else 'Inactive'}  ·  "
        f"Processing time: {job.processing_time:.2f}s"
    )
    _text_centered(c, P0["verdict_subline"]["x_center"],
                   P0["verdict_subline"]["y"],
                   sub, FONT_REGULAR, 8, C_WHITE)

    # ── Second row: INPUT FILES / PROCESSING STATUS ──
    _text(c, P0["input_heading"]["x"], P0["input_heading"]["y"],
          "INPUT FILES RECEIVED", FONT_SEMIBOLD, 10, C_BODY_GREY)
    _text(c, P0["status_heading"]["x"], P0["status_heading"]["y"],
          "PROCESSING STATUS", FONT_SEMIBOLD, 10, C_BODY_GREY)

    # Input files — up to 2 rows of filename + 2 wrap lines of details.
    # Max filename width = distance from left edge (x≈44) to PROCESSING
    # STATUS column (x≈297), minus a small safety margin → ~230 pt.
    _FNAME_MAX_W = 230.0   # pt
    _FNAME_STRIDE = 13.0   # pt: extra vertical stride for a wrapped filename
    if job.input_files:
        info = job.input_files[0]
        fname_lines = _wrap_text_at_chars(
            info.filename, FONT_BOLD, 10, _FNAME_MAX_W, break_chars="_-."
        )
        for _fi, _fl in enumerate(fname_lines[:2]):
            _text(c, P0["input_heading"]["x"],
                  P0["input_row_first_y"] + _fi * _FNAME_STRIDE,
                  _fl, FONT_BOLD, 10, C_HEADING_BLUE)
        # If filename wrapped, push details rows down by one stride.
        _fname_extra = (_FNAME_STRIDE if len(fname_lines) > 1 else 0)

        details_parts = []
        if info.modality:          details_parts.append(f"Modality: {info.modality}")
        if info.study_description: details_parts.append(f"Study: {info.study_description}")
        if info.series_description:details_parts.append(f"Series: {info.series_description}")
        if info.frames and info.frames > 1: details_parts.append(f"Frames: {info.frames}")
        elif info.rows:            details_parts.append(f"Size: {info.rows}×{info.columns}")
        if details_parts:
            details = " | ".join(details_parts)
            # Wrap at 230 pt so details text never crosses into the right
            # PROCESSING STATUS column (which starts at x≈297 pt).
            lines = _wrap_lines(details, FONT_REGULAR, 10, 230)
            line_ys = [P0["input_row_details1"] + _fname_extra,
                       P0["input_row_details2"] + _fname_extra,
                       P0["input_row_details2"] + _fname_extra + 15.0]
            for li, ly in zip(lines[:3], line_ys):
                _text(c, P0["input_heading"]["x"], ly, li,
                      FONT_REGULAR, 10, C_BODY_GREY)

    # Processing status — the designer's check-circle glyphs are baked
    # into the template PNG at the OLD x position (~352 pt). We have
    # shifted the whole section left to x=297.54 (verdict card left
    # border). So we:
    #   1. Paint a white rectangle over the area where the old baked
    #      circles live, erasing them cleanly.
    #   2. Draw fresh green filled circles at the new x.
    #   3. Draw a white "✓" inside each circle.
    #   4. Draw the text label to the right of the circle.

    # 1. Erase old baked circles (white fill, no stroke).
    ex0  = P0["status_erase_x0"]
    ex1  = P0["status_erase_x1"]
    ey0  = P0["status_erase_y0"]   # top-down
    ey1  = P0["status_erase_y1"]
    c.setFillColor(colors.HexColor("#ffffff"))
    c.setStrokeColor(colors.HexColor("#ffffff"))
    c.rect(ex0, _y(ey1), ex1 - ex0, ey1 - ey0, stroke=0, fill=1)

    # 2. Stamp the provided icon_check.png asset for each status row.
    # The icon is a 500×500 px green circle with a white checkmark.
    # We render it at diameter = 2*r pt so it matches the circle slot.
    r   = P0["status_circle_r"]
    cx  = P0["status_circle_x"]   # left edge of icon stamp
    icon_size = r * 2             # diameter in pt
    if ICON_CHECK.is_file():
        from reportlab.lib.utils import ImageReader as _IR
        _check_reader = _IR(str(ICON_CHECK))
    else:
        _check_reader = None

    for check_y, label in [
        (P0["status_check1_y"], "Files received and validated"),
        (P0["status_check2_y"], "AI analysis complete"),
    ]:
        # Icon top-down centre aligned with text cap-height midpoint.
        cy_td = check_y + 3.5   # top-down pt of icon centre
        icon_top_rl = _y(cy_td) - icon_size / 2  # ReportLab bottom-up y

        if _check_reader is not None:
            c.drawImage(
                _check_reader,
                cx, icon_top_rl,
                width=icon_size, height=icon_size,
                preserveAspectRatio=True, mask="auto",
            )
        else:
            # Fallback: plain green circle if asset missing
            c.setFillColor(colors.HexColor("#5db85d"))
            c.circle(cx + r, _y(cy_td), r, stroke=0, fill=1)

        # 4. Text label
        _text(c, P0["status_text_x"], check_y, label,
              FONT_REGULAR, 10, C_BODY_GREY)

    # ── APP DESCRIPTION ──
    _text(c, P0["app_heading"]["x"], P0["app_heading"]["y"],
          "APP DESCRIPTION", FONT_BOLD, 11, C_HEADING_BLUE)

    # Default body is the designer's EXACT pre-broken 14 lines, lifted
    # verbatim from the .ai so the column widths, soft hyphens, and
    # ligature breaks match pixel-for-pixel. If the job supplies its
    # own ``app_description`` string we fall back to word-wrapping.
    default_lines = [
        "Our cutting-edge AI tool revolutionizes the early detection of Myopic Choroidal Neovascularization",
        "(mCNV) in a non-invasive, fast, and highly accurate manner using OCT scans. By integrating",
        "state-of-the-art machine learning and deep learning technologies tailored specifically for medical",
        "imaging, this tool establishes itself as a crucial asset for ophthalmologists aiming to enhance",
        "patient outcomes through precision and timely diagnostics. Key performance highlights include:",
        "A robust F1 score of 0.89, demonstrating our model's balanced proficiency in precision (0.84) and",
        "recall (0.95). This translates to superior reliability in detecting genuine mCNV cases while minimi-",
        "zing false positives. An exceptional recall rate of 0.95, signifying our model's capability to identify",
        "nearly all true positives, thus drastically lowering the risk of clinical oversight in mCNV detection.",
        "Notably, while these metrics are impressive at the image level, our diagnostic tool achieves an even",
        "more remarkable accuracy rate of 98% at the patient level, offering unparalleled diagnostic confi-",
        "dence in a clinical setting. Leverage our AI-driven solution for top-tier mCNV detection, empowe-",
        "ring your practice with accuracy, efficiency, and improved patient care outcomes.",
    ]
    if job.app_description:
        body_lines = _wrap_lines(job.app_description, FONT_REGULAR, 10,
                                 P0["app_body_width"])
    else:
        body_lines = default_lines
    y = P0["app_body_y0"]
    for line in body_lines:
        if y > 760:
            break
        _text(c, P0["app_body_x"], y, line, FONT_REGULAR, 10, C_BODY_GREY)
        y += P0["app_line_stride"]

    # ── Footer ──
    _text_right(c, P5["footer_x_right"], P0["footer_y"],
                f"For research purposes only · 1/{total}",
                FONT_BOLD, 10, C_WHITE)


# ═════════════════════════════════════════════════════════════════════════
# Gallery page — 2 positive images per page
# ═════════════════════════════════════════════════════════════════════════

def _fmt_bbox(bbox: list) -> str:
    if not bbox or len(bbox) != 4:
        return "—"
    x1, y1, x2, y2 = bbox
    return f"x1={int(x1)} y1={int(y1)} x2={int(x2)} y2={int(y2)}"


def _fmt_conf(c: Optional[float]) -> str:
    if c is None:
        return "—"
    return f"{c * 100:.1f} %"


def _draw_gallery_slot_overlay(
    c: rl_canvas.Canvas,
    item: PerImageResult,
    slot_rect: tuple,
    tab_tag: dict,           # {"x_center":.., "y":.., "size":.., "color":..}
    cap_y: float,            # caption baseline (top-down)
) -> None:
    """Overlay dynamic filename (tab), annotated image, and caption row."""
    # Filename in the teal tab — centred at the designer's tab center.
    _text_centered(c, tab_tag["x_center"], tab_tag["y"],
                   item.filename, FONT_SEMIBOLD, tab_tag["size"],
                   tab_tag["color"])

    # Annotated OCT image inside the slot rect.
    _draw_image_slot(c, slot_rect, item)

    # Caption row — 4 chunks at exact designer x positions.
    conf = _fmt_conf(item.conf)
    bbx  = _fmt_bbox(item.bbox)
    _text(c, P_GAL["cap_x_label_confidence"], cap_y,
          "AI confidence:", FONT_REGULAR, 10, C_BODY_DARK)
    _text(c, P_GAL["cap_x_confidence_value"], cap_y,
          conf, FONT_BOLD, 10, C_HEADING_BLUE)
    _text(c, P_GAL["cap_x_label_bbox"], cap_y,
          "· bbox:", FONT_REGULAR, 10, C_BODY_DARK)
    _text(c, P_GAL["cap_x_bbox_value"], cap_y,
          bbx, FONT_BOLD, 10, C_HEADING_BLUE)


def _draw_gallery_page(
    c: rl_canvas.Canvas,
    pair: list[PerImageResult],
    page_num: int, total: int,
) -> None:
    _draw_template(c, TPL_GALLERY)

    if len(pair) >= 1:
        _draw_gallery_slot_overlay(
            c, pair[0],
            P_GAL["img_slot_top"],
            P_GAL["tab_text_top"],
            P_GAL["caption_top"]["y"],
        )
    if len(pair) >= 2:
        _draw_gallery_slot_overlay(
            c, pair[1],
            P_GAL["img_slot_bot"],
            P_GAL["tab_text_bot"],
            P_GAL["caption_bot"]["y"],
        )

    _text_right(c, P5["footer_x_right"], P_GAL["footer_y"],
                f"For research purposes only · {page_num}/{total}",
                FONT_BOLD, 10, C_WHITE)


# ═════════════════════════════════════════════════════════════════════════
# Table page helpers
# ═════════════════════════════════════════════════════════════════════════

# Row tint colours — kept as module-level constants so _table_rows_per_page
# and _draw_table_page_body share them without re-defining.
_C_ROW_ACTIVE   = "#FDEDED"   # very light red   — ACTIVE rows
_C_ROW_INACTIVE = "#EAF5EA"   # very light green — INACTIVE rows
_C_ROW_HEADER   = "#E8F4F8"   # light blue-grey  — column-header band


def _table_rows_per_page() -> int:
    """How many data rows fit inside one table-page card body.

    Geometry (from P5):
      • card inner area height  = card_inner_bottom - card_inner_top  ≈ 301 pt
      • column-header band      = tbl_header_h                        ≈  22 pt
      • remaining for data rows ≈ 279 pt
      • row stride              = row_stride                          ≈  25.815 pt
      → floor(279 / 25.815) = 10 rows

    We compute this dynamically so changing any of those constants
    automatically recalculates capacity.
    """
    body_h   = P5["card_inner_bottom"] - P5["card_inner_top"]
    usable_h = body_h - P5["tbl_header_h"]
    return max(1, int(usable_h // P5["row_stride"]))


# ── Card frame geometry constants (derived from P5 + template probe) ──
# These describe the OUTER card frame (including the teal tab's shadow
# extension, border radius and drop-shadow clearance), not the inner
# data area.
# Outer frame geometry — pixel-sampled at 300DPI from the .ai template:
#   teal frame x: px 187..2261 → pt 44.9..542.5 (symmetric 20pt ring on L and R)
#   inner white x at y=220pt:  pt 64..522
# Left inset = 64 - 45 = 19pt ≈ 20pt (symmetric with right: 542 - 522 = 20pt)
_CARD_OUTER_LEFT    = 45.0    # pt: outer left  edge of teal frame
_CARD_OUTER_RIGHT   = 543.0   # pt: outer right edge of teal frame  (symmetric to left)
_CARD_BORDER_RADIUS = 10.0    # pt: rounded-corner radius (matches .ai photo)
_CARD_SHADOW_DX     = 2.0     # pt: shadow offset right
_CARD_SHADOW_DY     = 3.0     # pt: shadow offset down
_CARD_SHADOW_BLUR   = 6.0     # pt: shadow blur radius
_CARD_SHADOW_ALPHA  = 0.22    # shadow opacity (0–1)
_CARD_INNER_PAD     = 0.0     # pt: no extra padding — frame wraps exactly around rows


def _card_inner_bounds(n_rows: int) -> tuple[float, float, float, float]:
    """Return (inner_left, inner_right, inner_top, inner_bottom) for a
    card that holds exactly ``n_rows`` data rows plus the header band.

    All values are top-down pt. The card's TOP is pinned to
    P5["card_inner_top"] (just below the baked teal tab).
    """
    il = P5["card_inner_left"]
    ir = P5["card_inner_right"]
    it = P5["card_inner_top"]
    hdr_h = P5["tbl_header_h"]
    stride = P5["row_stride"]
    # Minimum 1 row so the table never disappears entirely.
    n = max(1, n_rows)
    height = hdr_h + n * stride + _CARD_INNER_PAD
    ib = it + height
    return il, ir, it, ib


# Teal frame thickness around the white card body (pt). Chosen to
# match the visual weight of the original .ai card border.
_CARD_FRAME_THICK = 3.5


def _draw_card_frame(
    c: rl_canvas.Canvas,
    inner_left: float, inner_right: float,
    inner_top: float,  inner_bottom: float,
) -> None:
    """Draw the table card: teal-blue outer frame → soft drop shadow → white body.

    Layer order (bottom to top):
      1. Soft drop-shadow (approximated with 4 increasingly-transparent
         expanded rects — ReportLab has no Gaussian blur).
      2. Teal-blue rounded rect (the visible frame that matches the .ai).
      3. White rounded rect (the card body, covers the centre of the frame
         so only frame_thick pt of teal remain visible as a border ring).
    """
    pad = _CARD_INNER_PAD
    # White body rect (top-down pt)
    bx0 = inner_left  - pad
    bx1 = inner_right + pad
    by0 = inner_top   - pad
    by1 = inner_bottom + pad
    bw  = bx1 - bx0
    bh  = by1 - by0
    r   = _CARD_BORDER_RADIUS
    ft  = _CARD_FRAME_THICK   # teal frame thickness

    # ── 1. Drop shadow ──
    for i in range(1, 5):
        expand = _CARD_SHADOW_BLUR * i / 4
        alpha  = _CARD_SHADOW_ALPHA * (1 - i / 5)
        sx0 = bx0 + _CARD_SHADOW_DX - expand
        sy0 = by0 + _CARD_SHADOW_DY - expand
        sw  = bw + 2 * expand
        sh  = bh + 2 * expand
        sr  = r + expand
        shadow_grey = int(255 * (1 - alpha))
        c.setFillColor(colors.Color(
            shadow_grey / 255, shadow_grey / 255, shadow_grey / 255, 1
        ))
        c.roundRect(sx0, _y(sy0 + sh), sw, sh, sr, stroke=0, fill=1)

    # ── 2. Teal-blue outer frame ──
    c.setFillColor(colors.HexColor(C_TAB_TEAL))
    c.setStrokeColor(colors.HexColor(C_TAB_TEAL))
    c.roundRect(bx0 - ft, _y(by1 + ft), bw + 2*ft, bh + 2*ft, r + ft,
                stroke=0, fill=1)

    # ── 3. White card body (painted on top — only the teal border ring shows) ──
    c.setFillColor(colors.HexColor("#ffffff"))
    c.setStrokeColor(colors.HexColor("#ffffff"))
    c.roundRect(bx0, _y(by1), bw, bh, r, stroke=0, fill=1)


def _draw_table_chrome(
    c: rl_canvas.Canvas,
    page_num: int, total: int,
    n_rows: int,
) -> None:
    """Stamp the table template + dynamic PIL card frame + teal-tab heading + footer.

    Pipeline:
      1. Stamp TPL_TABLE (contains logo + teal tab + footer ribbon; the old
         baked card chrome has been wiped by templates._whiten_card_body).
      2. Render a teal-frame + gaussian-shadow + white-body card at exactly
         the height needed for ``n_rows`` via _render_table_card_png (same
         PIL pipeline as the verdict card).  Stamp the transparent PNG.
      3. Overlay the "PER-IMAGE RESULTS" tab text (white, baked tab shape
         stays in the template PNG).
      4. Overlay the footer page-number string.
    """
    _draw_template(c, TPL_TABLE)

    # ── Dynamic card frame ──
    # The OUTER gradient frame spans the full designer width:
    #   left = _CARD_OUTER_LEFT (44pt), right = _CARD_OUTER_RIGHT (556pt)
    # The dynamic height is determined by the number of data rows.
    # The INNER white rect sits at il/ir/it/ib (from _card_inner_bounds).
    il, ir, it, ib = _card_inner_bounds(n_rows)

    outer_l = _CARD_OUTER_LEFT    # 45.0 pt (pixel-probed)
    outer_r = _CARD_OUTER_RIGHT   # 543.0 pt (pixel-probed, symmetric)

    # Pin outer_t to the exact frame-top measured from the template PNG
    # (168.0pt at x=300pt). This makes the rendered card sit flush
    # against the baked teal tab with zero gap between them.
    outer_t = _CARD_OUTER_TOP     # 168.0 pt — flush with baked tab bottom

    # Symmetric top/bottom inset: wt = it - outer_t ≈ 20pt
    # Bottom: same 20pt ring → outer_b = ib + wt
    wt      = it - outer_t        # ≈ 20 pt (inner_top - frame_top)
    outer_b = ib + wt             # symmetric bottom ring thickness

    outer_w = outer_r - outer_l
    outer_h = outer_b - outer_t

    # Offsets from outer edges → inner white rect edges (all equal = wt ≈ 20pt)
    wl = il      - outer_l     # outer_left  → inner_left
    wr = outer_r - ir          # inner_right → outer_right
    wb = outer_b - ib          # inner_bottom→ outer_bottom  (= wt, symmetric)

    card_png = _render_table_card_png(
        outer_w, outer_h,
        wl, wr, wt, wb,
        _CARD_BORDER_RADIUS,
    )
    if card_png is not None:
        from reportlab.lib.utils import ImageReader
        sp = _TABLE_CARD_SHADOW_PAD
        # The PNG includes sp pt of transparent padding on every side.
        # Stamp it so the outer body rect aligns with outer_l / outer_t.
        stamp_x  = outer_l - sp
        stamp_rl = _y(outer_b) - sp   # ReportLab bottom-up y of PNG bottom
        c.drawImage(
            ImageReader(io.BytesIO(card_png)),
            stamp_x, stamp_rl,
            width  = outer_w + 2 * sp,
            height = outer_h + 2 * sp,
            preserveAspectRatio=False,
            mask="auto",
        )

    _text(c, P5["tab_heading_x"], P5["tab_heading_y"],
          "PER-IMAGE RESULTS",
          FONT_BOLD, P5["tab_heading_size"], C_WHITE)

    _text_right(c, P5["footer_x_right"], P5["footer_y"],
                f"For research purposes only · {page_num}/{total}",
                FONT_BOLD, 10, C_WHITE)


def _draw_table_body(
    c: rl_canvas.Canvas,
    rows: list[PerImageResult],
) -> None:
    """Draw the bounded column-header band + data rows inside the dynamic card.

    The card height is computed from len(rows) so it snugly wraps
    the data with no empty space below the last row.

    Layout (top-down pt):
      card_inner_top
        ├─ [tbl_header_h]  column-header band (#E8F4F8 tint + bold labels)
        ├─ row 0           alternating ACTIVE/INACTIVE tint + text
        ├─ row 1
        …
        └─ card_inner_top + tbl_header_h + N*row_stride
    """
    n   = len(rows)
    il, ir, it, ib = _card_inner_bounds(n)
    tbl_w = ir - il
    hdr_h = P5["tbl_header_h"]
    stride = P5["row_stride"]
    rsz    = P5["row_size"]

    # ── Hard clip to card inner rect ──
    c.saveState()
    clip_path = c.beginPath()
    clip_path.rect(il, _y(ib), tbl_w, ib - it)
    c.clipPath(clip_path, stroke=0, fill=0)

    # ── Column-header band ──
    hdr_top = it
    hdr_bot = it + hdr_h
    c.setFillColor(colors.HexColor(_C_ROW_HEADER))
    c.rect(il, _y(hdr_bot), tbl_w, hdr_h, stroke=0, fill=1)

    hdr_text_y = hdr_top + (hdr_h - rsz * _ASCENT_RATIO) / 2 + 1.0
    _text(c, P5["col_x_image"],  hdr_text_y, "IMAGE",        FONT_BOLD, 9, C_HEADING_BLUE)
    _text(c, P5["col_x_result"], hdr_text_y, "RESULT",       FONT_BOLD, 9, C_HEADING_BLUE)
    _text(c, P5["col_x_conf"],   hdr_text_y, "CONFIDENCE",   FONT_BOLD, 9, C_HEADING_BLUE)
    _text(c, P5["col_x_bbox"],   hdr_text_y, "BOUNDING BOX", FONT_BOLD, 9, C_HEADING_BLUE)

    # ── Data rows ──
    row_top = hdr_bot
    for item in rows:
        row_bot = row_top + stride
        is_pos  = item.pred == 1

        c.setFillColor(colors.HexColor(_C_ROW_ACTIVE if is_pos else _C_ROW_INACTIVE))
        c.rect(il, _y(row_bot), tbl_w, stride, stroke=0, fill=1)

        text_y = row_top + (stride - rsz * _ASCENT_RATIO) / 2 + 1.0

        _text(c, P5["col_x_image"], text_y, item.filename,
              FONT_REGULAR, rsz, C_BLACK)
        _text(c, P5["col_x_result"], text_y,
              "ACTIVE" if is_pos else "INACTIVE",
              FONT_BOLD if is_pos else FONT_REGULAR, rsz, C_BLACK)
        _text(c, P5["col_x_conf"], text_y,
              _fmt_conf(item.conf) if is_pos else "—",
              FONT_REGULAR, rsz, C_BLACK)

        if is_pos and item.bbox and len(item.bbox) == 4:
            bx1, by1, bx2, by2 = item.bbox
            half = stride / 2
            line1_y = row_top + half + P5["bbox_line1_y_offset"]
            line2_y = row_top + half + P5["bbox_line2_y_offset"]
            _text(c, P5["col_x_bbox"], line1_y,
                  f"x1={int(bx1)} y1={int(by1)}",
                  FONT_REGULAR, rsz, C_BLACK)
            _text(c, P5["col_x_bbox"], line2_y,
                  f"x2={int(bx2)} y2={int(by2)}",
                  FONT_REGULAR, rsz, C_BLACK)
        else:
            _text(c, P5["col_x_bbox"], text_y, "—",
                  FONT_REGULAR, P5["inactive_dash_size_bbox"], C_BLACK)

        row_top = row_bot

    c.restoreState()


# ═════════════════════════════════════════════════════════════════════════
# Table page (one or more pages — header repeated on every page)
# ═════════════════════════════════════════════════════════════════════════

def _draw_table_pages(
    c: rl_canvas.Canvas,
    job: ReportJob,
    first_page_num: int,
    total: int,
) -> int:
    """Render all table pages, repeating the column-header band on each.

    Each page shows at most ``_table_rows_per_page()`` data rows inside
    the card's inner white rectangle, hard-clipped so fills never escape
    the card frame.  If ``job.per_image`` exceeds that capacity the rows
    are paginated across as many table pages as needed.

    Returns the number of table pages emitted.
    """
    rows_per_page = _table_rows_per_page()
    all_rows = job.per_image
    # Chunk into pages
    chunks: list[list[PerImageResult]] = []
    for i in range(0, max(1, len(all_rows)), rows_per_page):
        chunks.append(all_rows[i:i + rows_per_page])
    if not chunks:
        chunks = [[]]

    for idx, chunk in enumerate(chunks):
        page_num = first_page_num + idx
        _draw_table_chrome(c, page_num, total, n_rows=len(chunk))
        _draw_table_body(c, chunk)
        if idx < len(chunks) - 1:
            c.showPage()   # intermediate pages — caller emits the last showPage

    return len(chunks)


# ═════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════

def _table_page_count(job: ReportJob) -> int:
    """How many table pages are needed for this job's per-image rows."""
    rpp = _table_rows_per_page()
    n = max(1, len(job.per_image))
    return (n + rpp - 1) // rpp


def _total_pages(job: ReportJob) -> int:
    """Summary + ceil(positives/2) gallery pages + table page(s)."""
    positives = [x for x in job.per_image if x.pred == 1]
    gallery = (len(positives) + 1) // 2
    return 1 + gallery + _table_page_count(job)


def build_pdf(job: ReportJob, out_path: Path) -> Path:
    """Generate the full MyopicCNV+ report PDF for ``job``.

    Returns ``out_path`` for chaining.

    ``job.software_version`` is overridden here — at render time — with the
    current value of the ``CLINICAL_TRIAL_PROTOCOL_VERSION`` environment
    variable (sourced from ``.env`` via ``config.load_dotenv``).  This makes
    ``.env`` the single source of truth for the "Software: MyopicCNV+ v…"
    line on the verdict page: changing the env-var and restarting the worker
    is all that is needed; no code change is required.

    Any ``software_version`` value passed in by the caller is ignored.
    """
    from dataclasses import replace as _replace
    # Import config here (not at module top) so the generator stays
    # usable as a standalone library; config.py calls load_dotenv()
    # which ensures .env is read before we inspect the env-var.
    from appway_backend.config import CLINICAL_TRIAL_PROTOCOL_VERSION
    job = _replace(
        job,
        software_version=CLINICAL_TRIAL_PROTOCOL_VERSION,
    )

    _register_fonts()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    c = rl_canvas.Canvas(str(out_path), pagesize=A4)
    c.setTitle(f"MyopicCNV+ Report - {job.job_id}")
    c.setAuthor("MyopicCNV+ backend")
    c.setCreator("appway-backend")

    total = _total_pages(job)
    page = 1

    _draw_page0_summary(c, job, total)
    c.showPage(); page += 1

    # ── Fix 4: top-clearance transform for pages 2+ ──────────────────────────
    # Heidelberg's viewer injects a ~25-30 pt header band at the very top of
    # every page AFTER page 1 (page 1 has a thick dark-blue title band that
    # absorbs the overlap).  We shift every non-summary page down by
    # TOP_CLEARANCE pt using a uniform canvas scale + vertical translate so
    # the injected header no longer overlaps our content.
    #
    # The transform is applied inside saveState()/restoreState() around each
    # page's draw calls so it does not bleed into showPage() or the next page.
    #
    # Maths:
    #   SCALE = (PAGE_H - TOP_CLEARANCE) / PAGE_H          ≈ 0.9525
    #   side_margin = (1 - SCALE) * PAGE_W / 2             ≈ 14.1 pt
    #   c.translate(side_margin, 0)  — centres the scaled content horizontally
    #   c.scale(SCALE, SCALE)        — uniform: no aspect-ratio distortion
    #
    # Result: ~40 pt clear band at top, ~14 pt symmetric left/right margins,
    # content stays on-page vertically (footer just clips but is unimportant
    # since it is inside Heidelberg's own footer area).
    # ─────────────────────────────────────────────────────────────────────────
    _TOP_CLEARANCE = 40.0                              # pt clear at top
    _SCALE  = (PAGE_H - _TOP_CLEARANCE) / PAGE_H      # ≈ 0.9525
    _SIDE_M = (1.0 - _SCALE) * PAGE_W / 2.0           # ≈ 14.1 pt

    positives = [x for x in job.per_image if x.pred == 1]
    for i in range(0, len(positives), 2):
        pair = positives[i:i + 2]
        c.saveState()
        c.translate(_SIDE_M, 0)
        c.scale(_SCALE, _SCALE)
        _draw_gallery_page(c, pair, page, total)
        c.restoreState()
        c.showPage(); page += 1

    # Table pages — same save/restore-per-page pattern as the gallery loop.
    #
    # ⚠️  We do NOT call _draw_table_pages() here any more. That helper
    # wrapped the entire set of table pages in a single saveState /
    # restoreState, then called c.showPage() between internal chunks.
    # showPage() flushes the graphics-state stack, so when > 10 per_image
    # rows caused two or more table pages the restoreState() after the last
    # showPage() would find an EMPTY stack and raise:
    #   IndexError: pop from empty list
    #
    # Fix: inline the pagination here so every page gets its own
    # saveState / [draw chrome + body] / restoreState / showPage cycle —
    # identical to how the gallery loop handles multiple gallery pages.
    rpp    = _table_rows_per_page()
    all_rows = job.per_image
    # Build chunks — at least one chunk (empty) so the table always renders.
    tbl_chunks: list[list[PerImageResult]] = [
        all_rows[i:i + rpp]
        for i in range(0, max(1, len(all_rows)), rpp)
    ] or [[]]
    for t_chunk in tbl_chunks:
        c.saveState()
        c.translate(_SIDE_M, 0)
        c.scale(_SCALE, _SCALE)
        _draw_table_chrome(c, page, total, n_rows=len(t_chunk))
        _draw_table_body(c, t_chunk)
        c.restoreState()
        c.showPage()
        page += 1

    c.save()
    return out_path
