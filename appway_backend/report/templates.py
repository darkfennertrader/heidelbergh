"""
Build clean pre-rendered page template PNGs from the designer's
`.ai` source (Myopic2_ver_b.ai, which is a PDF internally).

Strategy: since the `.ai` has EVERY text span baked in (including
dynamic sample values like "test-20260426_174801", "Myopic CNV:
ACTIVE", and the entire per-image results table), we REDACT all
text on each page and keep only the graphics chrome — gradients,
drop shadows, logo, teal tabs, footer band, verdict card pills,
table heading bar, etc.

We ALSO redact the baked sample OCT images on gallery pages so the
generator can draw user-supplied OCT frames in the empty slots.

The PDF generator then redraws every text element itself at the
exact (x, y, font, size, color) triples extracted straight from
the `.ai` — producing a byte-faithful visual match with zero
ghosting.

Run:
    uv run python -m appway_backend.report.templates

Outputs (pdf_assets/):
    page_template_summary.png    (page 0, text+photos stripped)
    page_template_gallery.png    (page 1, text+photos stripped)
    page_template_table.png      (page 5, text+photos stripped)

Pages 1–4 in the `.ai` are layout-identical (variants of the same
gallery chrome); we only build one gallery template — the generator
stamps it on every gallery page it emits.
"""
from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF

# This module lives at appway_backend/report/templates.py.
# The designer source .ai lives under pdf_sandbox/designer_source/ (gitignored).
# Generated template PNGs are written to appway_backend/report/assets/ so they
# are co-located with the Python source and the production worker never needs
# an external pdf_assets/ directory.
_MODULE_DIR = Path(__file__).resolve().parent          # appway_backend/report/
_REPO_ROOT  = _MODULE_DIR.parent.parent                # repo root
SRC_AI  = _REPO_ROOT / "pdf_sandbox" / "designer_source" / "Myopic2_ver_b.ai"
OUT_DIR = _MODULE_DIR / "assets"

# 300 DPI → ~2480×3508 px for A4, sharp at any zoom, template PNG
# stays ~300–500 KB each.
DPI = 300

# ── Table page: shift card up under the logo ──
# The designer's `.ai` puts the PER-IMAGE RESULTS card at y≈224 pt
# (with ~100 pt of empty space between the logo and the card). We
# override that layout: we move the whole card region upward by
# ``TABLE_SHIFT_UP_PT`` so the card sits with ~13 pt breathing room
# below the logo. The generator's P5 y-coords are reduced by the
# same amount so the overlaid text still lands on the right rows.
TABLE_SHIFT_UP_PT = 83.0
# Y-band (top-down pt) that contains the whole card incl. its drop
# shadow — we shift this entire band upward by TABLE_SHIFT_UP_PT.
# 215 pt = a few pt above the tab top (catches shadow bloom).
# 755 pt = just above the dark-blue footer ribbon (~757.65 pt).
TABLE_CARD_Y_TOP_PT = 215.0
TABLE_CARD_Y_BOT_PT = 755.0
# X-extent (pt) of the card body + shadow — the left/right gradient
# bars run at x < 18.5 pt and x > 577 pt and must NOT be shifted or
# the side-frame chrome will show a visible seam. Only pixels in this
# inner x-range are moved.  Values are derived from pixel-sampling the
# 300-DPI template: gap in non-white columns starts at px 77 / ends at
# px 2403, i.e. pt 18.5 / 576.9.
TABLE_CARD_X_LEFT_PT  = 18.5
TABLE_CARD_X_RIGHT_PT = 577.0


def _shift_card_up(png_path: Path) -> None:
    """Post-process the table-page template PNG to move the whole
    "PER-IMAGE RESULTS" card (teal tab + white body + drop shadow)
    upward so it sits right below the logo instead of floating in the
    middle of the page.

    We identify a Y-band in top-down point space
    ``[TABLE_CARD_Y_TOP_PT, TABLE_CARD_Y_BOT_PT]`` that fully contains
    the card (including shadow bloom) and shift every pixel row in
    that band upward by ``TABLE_SHIFT_UP_PT`` pt. The strip at the
    bottom of the band that becomes vacant after the shift is filled
    with pure white so the ribbon-free area beneath the card stays
    clean.

    The generator then draws the dynamic table text at P5 y-coords
    that have been reduced by the same ``TABLE_SHIFT_UP_PT`` so the
    text lands on the shifted card's rows.

    NOTE: The dark-blue footer ribbon at the very bottom of the page
    is OUTSIDE this band (ribbon top ≈ 757.65 pt > 755 pt) so it is
    untouched — the footer stays pinned to its original position.
    """
    try:
        from PIL import Image as PILImage
    except Exception:
        return

    img = PILImage.open(str(png_path)).convert("RGB")
    W, H = img.size
    # px per pt along the Y axis (A4 height is 841.89 pt).
    ppp = H / 841.89

    band_top_px   = int(round(TABLE_CARD_Y_TOP_PT * ppp))
    band_bot_px   = int(round(TABLE_CARD_Y_BOT_PT * ppp))
    shift_px      = int(round(TABLE_SHIFT_UP_PT * ppp))

    # X-range in pixels: only shift the card's inner area, NOT the
    # left/right gradient bars which form the page's side frame. Shifting
    # the full width would create a visible seam in those bars.
    x_left_px  = int(round(TABLE_CARD_X_LEFT_PT  * ppp))
    x_right_px = int(round(TABLE_CARD_X_RIGHT_PT * ppp))
    x_left_px  = max(0, x_left_px)
    x_right_px = min(W, x_right_px)
    inner_w    = x_right_px - x_left_px

    band_top_px = max(0, band_top_px)
    band_bot_px = min(H, band_bot_px)
    if shift_px <= 0 or band_bot_px - band_top_px <= shift_px or inner_w <= 0:
        return

    # Crop only the inner-x slice of the Y-band, paste it back shifted
    # upward, then fill the vacated strip at the bottom with white.
    # The left/right gradient bars (outside x_left..x_right) are never
    # touched so the side-frame chrome stays seamless.
    band = img.crop((x_left_px, band_top_px, x_right_px, band_bot_px))
    new_top = band_top_px - shift_px
    img.paste(band, (x_left_px, new_top))

    # Vacated strip at the bottom of the shifted zone.
    white = PILImage.new("RGB", (inner_w, shift_px), (255, 255, 255))
    img.paste(white, (x_left_px, band_bot_px - shift_px))

    img.save(str(png_path))


def _redact_all_text(page: fitz.Page) -> None:
    """Add a redact annotation over every visible text span, then
    apply — this removes the text from the content stream entirely
    (not just overpaints). Graphics and images remain."""
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type", 0) != 0:
            continue  # skip image blocks
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if not span.get("text", "").strip():
                    continue
                # Slightly expand the bbox so descenders/antialias
                # edges are fully captured.
                x0, y0, x1, y1 = span["bbox"]
                r = fitz.Rect(x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)
                # fill=None keeps underlying graphics visible. We
                # only want to strip the glyphs themselves.
                page.add_redact_annot(r, fill=None)
    # cross_out=False so no strike-through line is drawn on apply
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)


def _redact_gallery_photos(page: fitz.Page) -> None:
    """Strip the baked sample OCT image(s) from a gallery page by
    redacting their rectangles with 'images=remove'. We target the
    two slot rectangles precisely."""
    OCT_RECTS_PT = [
        (97.12, 169.68, 499.33, 407.50),
        (97.12, 501.19, 499.33, 739.01),
    ]
    for rect_tuple in OCT_RECTS_PT:
        r = fitz.Rect(*rect_tuple)
        # Fill with white so the slot reads as empty paper.
        page.add_redact_annot(r, fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)


def _redact_summary_verdict_card(page: fitz.Page) -> None:
    """Strip the baked verdict-card raster (red rounded rect + shadow)
    from the summary page. The `.ai` layers two stacked images:
      * xref=255 — outer shadow `Rect(284.43, 274.29, 577.47, 372.45)`
      * xref=258 — inner red body `Rect(291.97, 281.83, 569.65, 364.87)`
    The outer shadow rect encloses both, so redacting it with
    ``PDF_REDACT_IMAGE_REMOVE`` deletes the entire card. The
    generator then draws its own rounded rect + text fresh so the
    card colour (red for positive, blue for negative) follows the
    job's verdict."""
    r = fitz.Rect(284.43, 274.29, 577.47, 372.45)
    page.add_redact_annot(r, fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)



# Erase bounds for the whole card chrome (teal frame + shadow + interior).
# Top-down pt, derived from the shifted template:
#   • X: 34..565 — generous margin outside the outer shadow edge
#   • Y: 178..520 — from just below the teal tab bottom to well below any row
#     content (the card can grow at most to card_inner_top + max_rows*stride
#     ≈ 188 + 10*25.815 + 8 ≈ 454 pt, so 520 gives >60 pt of shadow headroom)
_ERASE_X_LEFT_PT  = 34.0
_ERASE_X_RIGHT_PT = 565.0
_ERASE_Y_TOP_PT   = 178.0
_ERASE_Y_BOT_PT   = 520.0


def _whiten_card_body(png_path: Path) -> None:
    """Post-process the table-page template PNG to erase the ENTIRE baked
    card chrome (teal frame + drop shadow + white body + sample data rows)
    so the generator can stamp a dynamically-sized PIL-rendered card frame
    at runtime.

    After this step the template PNG contains ONLY:
      • MyopicCNV+ logo
      • Teal "PER-IMAGE RESULTS" tab shape (baked, kept as-is)
      • Dark-blue footer ribbon

    The generator then renders a fresh rounded-rect card (teal frame +
    gaussian shadow + white body) at the exact height needed for the actual
    number of data rows, stamps it, then draws the header band + rows inside.
    """
    try:
        from PIL import Image as PILImage, ImageDraw
    except Exception:
        return
    img = PILImage.open(str(png_path)).convert("RGB")
    W, H = img.size
    ppp_x = W / 595.28
    ppp_y = H / 841.89

    # Convert erase-bounds from pt → px and clamp to image dimensions.
    x0 = max(0, int(round(_ERASE_X_LEFT_PT  * ppp_x)))
    x1 = min(W, int(round(_ERASE_X_RIGHT_PT * ppp_x)))
    y0 = max(0, int(round(_ERASE_Y_TOP_PT   * ppp_y)))
    y1 = min(H, int(round(_ERASE_Y_BOT_PT   * ppp_y)))

    draw = ImageDraw.Draw(img)
    draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255))
    img.save(str(png_path))


def _recolor_job_info_card(png_path: Path) -> None:
    """Post-process the summary-page template PNG to recolour the
    JOB INFORMATION card background from its teal tint (~RGB 217,239,242)
    to the APP DESCRIPTION card blue tint (~RGB 216,229,243)."""
    try:
        import numpy as np
        from PIL import Image as PILImage
    except Exception:
        return
    img = PILImage.open(str(png_path)).convert("RGB")
    arr = np.array(img, dtype=np.int32)
    src_r, src_g, src_b = 217, 239, 242
    tgt_r, tgt_g, tgt_b = 216, 229, 243
    dist = np.sqrt(
        (arr[:, :, 0] - src_r) ** 2 +
        (arr[:, :, 1] - src_g) ** 2 +
        (arr[:, :, 2] - src_b) ** 2
    )
    mask = dist < 20
    arr[mask, 0] = tgt_r
    arr[mask, 1] = tgt_g
    arr[mask, 2] = tgt_b
    PILImage.fromarray(arr.astype(np.uint8)).save(str(png_path))


def main() -> None:
    if not SRC_AI.is_file():
        raise FileNotFoundError(f"Source .ai not found at {SRC_AI}")

    scale = DPI / 72.0
    mat = fitz.Matrix(scale, scale)

    doc = fitz.open(SRC_AI)
    plans = {0: "page_template_summary.png",
             1: "page_template_gallery.png",
             5: "page_template_table.png"}

    for i, out_name in plans.items():
        page = doc[i]
        # Summary: drop the baked verdict-card raster before text
        # redaction. Gallery: drop the baked OCT photos first too.
        if i == 0:
            _redact_summary_verdict_card(page)
        elif i == 1:
            _redact_gallery_photos(page)
        _redact_all_text(page)

        pix = page.get_pixmap(matrix=mat, alpha=False)
        out = OUT_DIR / out_name
        pix.save(str(out))
        # Table page only: post-process the rendered PNG to shift the
        # card upward so it sits right below the logo (see the
        # TABLE_SHIFT_UP_PT doc-comment for context), then wipe the
        # baked sample-data content so the card interior is pure white
        # and the generator can draw the dynamic table from scratch.
        if i == 5:
            _shift_card_up(out)
            _whiten_card_body(out)
        # Summary page only: recolour the JOB INFO card background to
        # match the APP DESCRIPTION card's blue tint.
        if i == 0:
            _recolor_job_info_card(out)
        print(f"Wrote {out.name:32s}  ({pix.width}×{pix.height} px, {out.stat().st_size // 1024} KB)")
    doc.close()


if __name__ == "__main__":
    main()
