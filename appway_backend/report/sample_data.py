"""
Mock job data for the PDF generator sandbox.

Two presets are exposed:

  * ``STATIC_JOB``  — a "backbone" preset that uses obvious placeholder
    strings (``"XXX…"``, ``"0000-00-00"``, ``"X.X.X"`` etc.) so the
    generated PDF acts as a visual wireframe. Use this while iterating on
    the layout: you can judge spacing, typography, and section positions
    without any real-looking data distracting the eye.

  * ``MOCK_JOB``    — a plausible, fully-populated preset that mirrors
    the ``test-20260426_174801`` scenario from ``pdf_assets/Myopic2
    copia.pdf``. Use this for final visual comparison against the
    designer's reference output.

``preview.py`` uses ``STATIC_JOB`` by default; swap to ``MOCK_JOB`` when
you want to see the layout with realistic content. To test the image
gallery with real OCT images, drop PNGs into ``pdf_sandbox/sample_images/``
whose filenames match the ``filename`` field of each
``PerImageResult``; if a file isn't present the generator will render
a placeholder box instead.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .generator import InputFileInfo, PerImageResult, ReportJob

# Sample images are looked up in pdf_sandbox/sample_images/ so the
# sandbox can load real OCT images without polluting the main package.
# When this module is imported outside the sandbox context (e.g. in
# tests) and the directory doesn't exist, _img() will simply return None
# and the generator will render a placeholder box.
_SANDBOX_ROOT = Path(__file__).resolve().parent.parent.parent / "pdf_sandbox"
SAMPLE_IMAGES_DIR = _SANDBOX_ROOT / "sample_images"


def _img(filename: str) -> Path | None:
    """Return the sample_images path for ``filename`` if it exists,
    otherwise None (so the generator falls back to a placeholder box)."""
    p = SAMPLE_IMAGES_DIR / filename
    return p if p.is_file() else None


# ─────────────────────────────────────────────────────────────────────────
# STATIC_JOB — backbone / wireframe preset
#
# Every field that will be filled in at runtime from the real job uses
# an obviously-placeholder value so the generated PDF is a clean visual
# skeleton. Field counts are kept to the minimum that still exercises
# each layout slot:
#   • 1 input file   → INPUT FILES RECEIVED section renders 1 row
#   • 2 positives    → gallery page 2 renders BOTH slots (canonical
#                      two-up layout you iterate against)
#   • 1 negative     → table still exercises the INACTIVE row style
# ─────────────────────────────────────────────────────────────────────────

# NOTE: ReportJob.software_version is overridden at render time inside
# build_pdf() with the live value of the CLINICAL_TRIAL_PROTOCOL_VERSION
# environment variable (sourced from .env).  The value set here is used
# only as a dataclass default; it will NEVER appear in a generated PDF.
STATIC_JOB = ReportJob(
    job_id="XXXXXXXXXXXXXXXXXXX",
    # Fixed, obviously-fake timestamp (never ticks).
    processed_at=datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    software_version="X.X.X",   # overridden at render time — see note above
    # Marking the verdict as positive so the red ACTIVE card renders and
    # you can evaluate its visual weight against the rest of the page.
    verdict="Positive",
    processing_time=0.00,
    input_files=[
        InputFileInfo(
            filename="input_file.dcm",
            # All metadata fields left blank so only the filename line
            # appears — keeps the section visually minimal.
            modality="",
            study_description="",
            series_description="",
            frames=1,
        ),
    ],
    per_image=[
        # Two positives so the canonical gallery page shows BOTH slots.
        PerImageResult(
            filename="image_001.png",
            pred=1,
            conf=0.000,
            bbox=[0, 0, 0, 0],
            image_path=_img("image_001.png"),
        ),
        PerImageResult(
            filename="image_002.png",
            pred=1,
            conf=0.000,
            bbox=[0, 0, 0, 0],
            image_path=_img("image_002.png"),
        ),
        # One negative so the table still exercises the INACTIVE row style.
        PerImageResult(
            filename="image_003.png",
            pred=0,
            conf=None,
            bbox=[],
            image_path=_img("image_003.png"),
        ),
    ],
)


# ─────────────────────────────────────────────────────────────────────────
# MOCK_JOB — realistic preset mirroring Myopic2 copia.pdf
# (kept around so we can compare the final layout against the designer's
# reference PDF once the backbone is locked down)
# ─────────────────────────────────────────────────────────────────────────

MOCK_JOB = ReportJob(
    job_id="test-20260426_174801",
    processed_at=datetime(2026, 4, 26, 17, 48, 19, tzinfo=timezone.utc),
    software_version="0.1.0",
    verdict="Positive",           # triggers the red "ACTIVE" verdict card
    processing_time=12.60,
    input_files=[
        InputFileInfo(
            filename="test_443816.dcm",
            modality="OPT",
            study_description="External Patient: HRA + OCT",
            series_description="APPWAY TEST (10fr from images)",
            frames=10,
        ),
    ],
    per_image=[
        PerImageResult(
            filename="LoscialpoT006_p_p.png",
            pred=1, conf=0.715,
            bbox=[689, 128, 863, 379],
            image_path=_img("LoscialpoT006_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT007_p_p.png",
            pred=1, conf=0.752,
            bbox=[724, 142, 853, 375],
            image_path=_img("LoscialpoT007_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT007c_p_p.png",
            pred=1, conf=0.800,
            bbox=[689, 122, 848, 379],
            image_path=_img("LoscialpoT007c_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT008_p_p.png",
            pred=1, conf=0.777,
            bbox=[727, 126, 878, 382],
            image_path=_img("LoscialpoT008_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT008d_p_p.png",
            pred=1, conf=0.784,
            bbox=[681, 115, 872, 397],
            image_path=_img("LoscialpoT008d_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT009_p_p.png",
            pred=1, conf=0.727,
            bbox=[724, 127, 880, 378],
            image_path=_img("LoscialpoT009_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT009e_p_p.png",
            pred=0, conf=None, bbox=[],
            image_path=_img("LoscialpoT009e_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT010_p_p.png",
            pred=1, conf=0.609,
            bbox=[728, 130, 855, 384],
            image_path=_img("LoscialpoT010_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT010f_p_p.png",
            pred=0, conf=None, bbox=[],
            image_path=_img("LoscialpoT010f_p_p.png"),
        ),
        PerImageResult(
            filename="LoscialpoT011_p_p.png",
            pred=1, conf=0.686,
            bbox=[707, 119, 849, 367],
            image_path=_img("LoscialpoT011_p_p.png"),
        ),
    ],
)
