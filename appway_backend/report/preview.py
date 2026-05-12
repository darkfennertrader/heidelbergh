"""
Sandbox preview runner: generate a PDF from one of the presets in
``sample_data.py`` and render each page to a PNG for quick visual
inspection.

Usage:
    cd /home/ubuntu/appway-backend
    uv run python -m appway_backend.report.preview

Outputs:
    pdf_sandbox/outputs/preview.pdf          — the full PDF
    pdf_sandbox/outputs/previews/verdict_page.png
    pdf_sandbox/outputs/previews/image_page.png
    pdf_sandbox/outputs/previews/table_page.png
    (additional gallery pages: image_page_2.png, image_page_3.png …)

The runner uses ``STATIC_JOB`` by default — a wireframe preset whose
placeholder strings (``"XXX…"``, ``"0000-00-00"``, ``"X.X.X"``) make it
easy to iterate on the page backbone without real-looking data in the
way. Swap the import below to ``MOCK_JOB`` when you want a realistic
preview for side-by-side comparison with ``pdf_assets/Myopic2 copia.pdf``.

The ``previews/`` folder is wiped at the start of every run so the
filename list always reflects exactly the page count of the current
job (i.e. after switching from MOCK_JOB to STATIC_JOB you won't see
stale ``image_page_4.png``…``image_page_6.png`` files from a previous run).
"""
from __future__ import annotations

from pathlib import Path

from .generator import build_pdf
from .sample_data import MOCK_JOB as JOB  # swap to MOCK_JOB for realistic preview

# Sandbox output root: pdf_sandbox/outputs/
_REPO_ROOT    = Path(__file__).resolve().parent.parent.parent
OUT_DIR       = _REPO_ROOT / "pdf_sandbox" / "outputs"
OUT_PDF       = OUT_DIR / "preview.pdf"
PREVIEW_DIR   = OUT_DIR / "previews"
PREVIEW_DPI   = 120     # good screen-reading quality, quick to render


def _wipe_previews() -> None:
    """Remove stale preview PNGs so the folder always reflects exactly
    the pages in the current run.
    """
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    for pattern in ("page-*.png", "verdict_page.png", "image_page*.png",
                    "table_page.png"):
        for p in PREVIEW_DIR.glob(pattern):
            try:
                p.unlink()
            except Exception:
                pass


def _render_previews() -> int:
    """Render each PDF page to a named PNG in ``previews/``.

    Page mapping (1-based):
      1          → verdict_page.png
      2 … n-1   → image_page.png  (first gallery page)
                   image_page_2.png, image_page_3.png … (subsequent)
      n          → table_page.png   (always the last page)
    """
    import fitz  # lazy import: only the sandbox needs this
    doc = fitz.open(str(OUT_PDF))
    n = len(doc)
    gallery_index = 0
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=PREVIEW_DPI)
        if i == 1:
            name = "verdict_page.png"
        elif i == n:
            name = "table_page.png"
        else:
            gallery_index += 1
            name = "image_page.png" if gallery_index == 1 else f"image_page_{gallery_index}.png"
        pix.save(str(PREVIEW_DIR / name))
    doc.close()
    return n


def main() -> int:
    # 1) Build the PDF from the chosen preset.
    path = build_pdf(JOB, OUT_PDF)
    print(f"Wrote {path}  ({path.stat().st_size} bytes)")

    # 2) Wipe any stale preview PNGs, then regenerate a fresh set.
    _wipe_previews()
    n = _render_previews()
    print(f"Rendered {n} preview PNG(s) → {PREVIEW_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
