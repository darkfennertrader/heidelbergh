"""
DICOM processor. Per-job local layout:

    outputs/<job-id>/
        <dicom-stem-1>/
            metadata.json          DICOM header (no pixel data)
            <dicom-stem-1>.png     single-frame, OR
            frame000.png …         multi-frame volume, one PNG per slice
        <dicom-stem-2>/
            metadata.json
            <dicom-stem-2>.png
        result.pdf                 single combined report (also embedded in result.dcm)

    ~/appway-workdir/<job-id>/output/result.dcm   ← only uploaded to S3 (1 per job)
"""

import json
import logging
import shutil
from pathlib import Path

import numpy as np
import pydicom
from PIL import Image

from . import config
from .epdf_generator import generate_epdf_dcm
from .inference import run_inference

logger = logging.getLogger(__name__)

# Per-job outputs root — everything an operator might want to look at for
# a given job lives under outputs/<job-id>/:
#   - <stem>.json   DICOM header metadata
#   - <stem>.png    pixel frames (the PNGs fed to YOLO)
#   - result.pdf    the human-readable report PDF (copy of what's embedded
#                   in result.dcm, which itself only lives transiently in
#                   ~/appway-workdir/<job-id>/output/ before being uploaded
#                   to S3 and the workdir is wiped)
# Never uploaded to S3. Never cleaned up automatically — operators decide.
OUTPUTS_ROOT = Path("/home/ubuntu/appway-backend/outputs")




def _dicom_metadata_to_dict(ds: pydicom.Dataset) -> dict:
    """
    Convert a pydicom Dataset to a plain dict suitable for JSON serialisation.
    Skips pixel data and any element that cannot be serialised.
    """
    result = {}
    for elem in ds:
        if elem.keyword == "PixelData":
            continue
        try:
            tag_name = elem.keyword or str(elem.tag)
            value = elem.value
            # Recursively handle sequences
            if isinstance(value, pydicom.sequence.Sequence):
                value = [_dicom_metadata_to_dict(item) for item in value]
            elif isinstance(value, bytes):
                value = value.hex()
            elif hasattr(value, "__iter__") and not isinstance(value, str):
                value = list(value)
            else:
                value = str(value)
            result[tag_name] = value
        except Exception:
            pass
    return result


def _normalise_frame(frame: np.ndarray, invert: bool = False) -> np.ndarray:
    """Normalise a 2D frame to uint8 [0-255]."""
    f = frame.astype(float)
    if f.max() != f.min():
        f = (f - f.min()) / (f.max() - f.min()) * 255.0
    else:
        f = np.zeros_like(f)
    f = f.astype(np.uint8)
    if invert:
        f = 255 - f
    return f


# Characters that are always safe in a PNG filename on Linux + in DICOM
# SeriesDescription etc. Anything else gets replaced with '_'.
_SAFE_FNAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+")


def _sanitize_for_filename(name: str) -> str:
    """Strip path separators / odd chars so a string is safe as a filename."""
    out = []
    for ch in (name or "").strip():
        out.append(ch if ch in _SAFE_FNAME_CHARS else "_")
    cleaned = "".join(out).strip("._")
    return cleaned or "frame"


def _derive_frame_labels(ds: pydicom.Dataset, n_frames: int) -> list[str]:
    """
    Produce a list of N human-readable labels, one per frame of a multi-frame
    DICOM, that will be used to name the extracted PNG files.

    Strategy, in priority order:

      1. **Test DICOMs built by scripts/build_test_dcm.sh**
         We stamp the original source filenames (comma-separated) into the
         ``ImageComments`` tag. If it parses cleanly into exactly N names, we
         use them (stem only, ``.png`` will be appended by the caller). This
         makes the ophthalmologist's "Per-Image Results" table in the PDF
         show the SAME filenames they uploaded, so they can cross-reference
         directly to their source images.

      2. **Real Spectralis / Heidelberg OPT volumes**
         Source images don't have filenames — they are axial B-scans of the
         same eye. We pull ``InStackPositionNumber`` (slice index as shown in
         HEYEX) from ``PerFrameFunctionalGroupsSequence[i].FrameContentSequence``
         and ``ImagePositionPatient[2]`` (Z-depth in mm) from
         ``PerFrameFunctionalGroupsSequence[i].PlanePositionSequence``. Labels
         look like ``b_scan_001_z1.41mm``. The clinician scrolls to that slice
         number in HEYEX and immediately sees what the AI flagged.

      3. **Fallback**
         If neither source is usable, fall back to generic ``frame000``…
         ``frameNNN`` — matches the pre-refactor behaviour so nothing breaks.
    """
    # ── (1) Test DICOM path — ImageComments CSV of original filenames ──
    try:
        ic = str(getattr(ds, "ImageComments", "") or "").strip()
        if ic:
            names = [n.strip() for n in ic.split(",") if n.strip()]
            if len(names) == n_frames and all("." in n for n in names):
                # Use just the stem (drop the .jpeg / .png suffix — the caller
                # will write '.png' anyway).
                return [_sanitize_for_filename(Path(n).stem) for n in names]
    except Exception:
        # Anything odd → fall through to the real-DICOM path.
        pass

    # ── (2) Real OPT volume — per-frame B-scan metadata ──
    try:
        pfgs = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
        if pfgs is not None and len(pfgs) == n_frames:
            labels = []
            for frame_item in pfgs:
                # B-scan slice number (1-based, matches HEYEX UI)
                slice_no = None
                fcs = getattr(frame_item, "FrameContentSequence", None)
                if fcs and len(fcs) > 0:
                    slice_no = getattr(fcs[0], "InStackPositionNumber", None)

                # Z-depth in mm (ImagePositionPatient = [x, y, z])
                z_mm = None
                pps = getattr(frame_item, "PlanePositionSequence", None)
                if pps and len(pps) > 0:
                    ipp = getattr(pps[0], "ImagePositionPatient", None)
                    if ipp is not None and len(ipp) >= 3:
                        try:
                            z_mm = float(ipp[2])
                        except (TypeError, ValueError):
                            z_mm = None

                if slice_no is not None and z_mm is not None:
                    labels.append(f"b_scan_{int(slice_no):03d}_z{z_mm:.2f}mm")
                elif slice_no is not None:
                    labels.append(f"b_scan_{int(slice_no):03d}")
                else:
                    labels.append(None)  # marker → fall through for this one

            if all(l is not None for l in labels):
                return labels  # type: ignore[return-value]
    except Exception:
        pass

    # ── (3) Generic fallback (pre-existing behaviour) ──
    return [f"frame{i:03d}" for i in range(n_frames)]


def _prepare_for_yolo(img: Image.Image) -> Image.Image:
    """
    Align a DICOM-extracted frame with the training-time input distribution.

    The MyopicCNV+ YOLO weight was fine-tuned on HEYEX TIFF exports at
    ``1008 × 596`` (landscape, RGB) — see
    ``/home/ubuntu/mcnv/src/web_pages/helpers.py`` → ``list_of_images()``,
    whose only preprocessing is ``PIL.Image.convert("RGB")`` + save as JPEG
    (no resize, no CLAHE, no contrast enhancement). Heidelberg Spectralis
    volumes, however, arrive natively as ``496 × 512`` greyscale B-scans,
    so feeding them directly to the model creates a train/inference domain
    shift: different aspect ratio → different letterbox padding during
    YOLO's internal 640×640 resize → degraded detection accuracy.

    To eliminate that shift we resize every extracted frame to the exact
    training resolution (``TRAIN_IMAGE_WIDTH × TRAIN_IMAGE_HEIGHT``, via
    Lanczos for high-quality downscaling of dense OCT textures) and
    convert to RGB so the pixel layout matches the training tensors. The
    resulting PNG is what both (a) the worker feeds to
    ``inference.run_inference()`` and (b) the operator inspects in
    ``outputs/<job-id>/<stem>/``.
    """
    img = img.resize(
        (config.TRAIN_IMAGE_WIDTH, config.TRAIN_IMAGE_HEIGHT),
        Image.LANCZOS,
    )
    return img.convert("RGB")


def _dicom_to_png(ds: pydicom.Dataset, dest: Path) -> None:
    """
    Extract pixel data from a DICOM dataset and save as PNG file(s).

    - Single 2D frame (H, W)         → dest.png
    - Multi-frame volume (N, H, W)   → one PNG per slice, named from
      per-frame metadata — see ``_derive_frame_labels()`` for the priority
      rules (ImageComments CSV → B-scan slice+depth → generic frameNNN).
    - RGB 2D (H, W, 3)               → dest.png

    Every saved PNG is first passed through ``_prepare_for_yolo()`` so it
    matches the MyopicCNV+ training input distribution (1008×596 RGB).
    """
    arr = ds.pixel_array
    photo = getattr(ds, "PhotometricInterpretation", "MONOCHROME2")
    invert = (photo == "MONOCHROME1")

    dest.parent.mkdir(parents=True, exist_ok=True)

    if arr.ndim == 2:
        # Single monochrome frame
        img = Image.fromarray(_normalise_frame(arr, invert), mode="L")
        img = _prepare_for_yolo(img)
        img.save(dest, format="PNG")

    elif arr.ndim == 3:
        samples = getattr(ds, "SamplesPerPixel", 1)
        if samples == 3:
            # RGB single frame (H, W, 3)
            img = Image.fromarray(arr.astype(np.uint8), mode="RGB")
            img = _prepare_for_yolo(img)
            img.save(dest, format="PNG")
        else:
            # Multi-frame volume (N, H, W) — save each slice separately
            suffix = dest.suffix
            parent = dest.parent
            labels = _derive_frame_labels(ds, arr.shape[0])
            for i, frame in enumerate(arr):
                frame_dest = parent / f"{labels[i]}{suffix}"
                img = Image.fromarray(_normalise_frame(frame, invert), mode="L")
                img = _prepare_for_yolo(img)
                img.save(frame_dest, format="PNG")

    elif arr.ndim == 4:
        # Multi-frame RGB (N, H, W, 3)
        suffix = dest.suffix
        parent = dest.parent
        labels = _derive_frame_labels(ds, arr.shape[0])
        for i, frame in enumerate(arr):
            frame_dest = parent / f"{labels[i]}{suffix}"
            img = Image.fromarray(frame.astype(np.uint8), mode="RGB")
            img = _prepare_for_yolo(img)
            img.save(frame_dest, format="PNG")

    else:
        raise ValueError(f"Unexpected pixel array shape: {arr.shape}")


def process(job_id: str, input_dir: Path, output_dir: Path) -> None:
    """
    For every .dcm file in input_dir:
      1. Save metadata as JSON  → outputs/<job-id>/<stem>.json
      2. Save pixel image as PNG → outputs/<job-id>/<stem>.png

    Then generate a single DICOM ePDF result report:
      3. output_dir/result.dcm   ← AppWay-compliant encapsulated PDF (to S3)
      4. outputs/<job-id>/result.pdf ← raw PDF copy for human review
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    job_outputs_dir = OUTPUTS_ROOT / job_id
    job_outputs_dir.mkdir(parents=True, exist_ok=True)

    # Collect the per-DICOM PNG subdirectories so the ePDF generator can
    # locate each positive image later for the Appendix (red bbox overlay).
    png_dirs: list[Path] = []

    files = sorted(input_dir.rglob("*"))
    for src in files:
        if not src.is_file():
            continue

        if src.suffix.lower() != ".dcm":
            logger.info("[%s] Skipping non-DICOM file: %s", job_id, src.name)
            continue

        stem = src.stem
        logger.info("[%s] Processing DICOM: %s", job_id, src.name)

        try:
            ds = pydicom.dcmread(str(src))
        except Exception as e:
            logger.error("[%s] Failed to read DICOM %s: %s", job_id, src.name, e)
            continue

        # Per-DICOM subdirectory keeps each .dcm file's artefacts together:
        #   outputs/<job-id>/<stem>/metadata.json
        #   outputs/<job-id>/<stem>/<stem>.png               (single-frame DICOM)
        #   outputs/<job-id>/<stem>/frame000.png, frame001.png, …  (multi-frame volume)
        dicom_dir = job_outputs_dir / stem
        dicom_dir.mkdir(parents=True, exist_ok=True)
        png_dirs.append(dicom_dir)

        # --- Metadata → JSON ---
        try:
            metadata = _dicom_metadata_to_dict(ds)
            json_path = dicom_dir / "metadata.json"
            json_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))
            logger.info("[%s]   Metadata saved → %s", job_id, json_path)
        except Exception as e:
            logger.error("[%s]   Metadata extraction failed for %s: %s", job_id, src.name, e)

        # --- Pixel data → PNG ---
        # Use the DICOM stem as the PNG filename so the report's
        # "Per-Image Results" table shows the original input filename
        # (e.g. "20260518132053.rfzyz2kj.oer.png") rather than a
        # generic "image.png".  For multi-frame volumes _dicom_to_png()
        # overrides the dest stem anyway (using ImageComments CSV labels
        # or per-frame B-scan metadata), so this only matters for
        # single-frame DICOMs.
        try:
            png_path = dicom_dir / f"{stem}.png"
            _dicom_to_png(ds, png_path)
            logger.info("[%s]   PNG saved → %s", job_id, png_path)
        except Exception as e:
            logger.error("[%s]   PNG extraction failed for %s: %s", job_id, src.name, e)

    # --- Run YOLO inference on extracted PNG images ---
    # rglob so we pick up PNGs in every per-DICOM subdirectory.
    png_paths = sorted(job_outputs_dir.rglob("*.png"))


    logger.info("[%s] Running inference on %d PNG(s)…", job_id, len(png_paths))
    inference_result = None
    try:
        inference_result = run_inference(png_paths)
        logger.info(
            "[%s] Inference result: verdict=%s, processing_time=%.2fs, images=%d",
            job_id,
            inference_result.get("verdict"),
            inference_result.get("processing_time", 0.0),
            len(inference_result.get("per_image", [])),
        )
    except Exception as e:
        logger.error("[%s] Inference failed: %s — falling back to report without AI result", job_id, e)

    # --- Generate ePDF result DICOM ---
    logger.info("[%s] Generating ePDF result DICOM…", job_id)
    try:
        result_dcm_path = output_dir / "result.dcm"
        generate_epdf_dcm(
            job_id,
            input_dir,
            result_dcm_path,
            inference_result=inference_result,
            png_dirs=png_dirs,
        )
        logger.info("[%s] ePDF result DICOM → %s", job_id, result_dcm_path)
    except Exception as e:
        logger.error("[%s] ePDF generation failed: %s", job_id, e)
        raise

    # --- Save human-readable PDF copy (outputs/<job-id>/result.pdf) ---
    # This is purely for operator review on the backend machine. It is NEVER
    # uploaded to S3 and is NOT cleaned up by the worker — operators decide
    # when to prune /home/ubuntu/appway-backend/outputs/. A failure here must
    # never block the real result flow, so we catch and warn.
    try:
        ds_out = pydicom.dcmread(str(result_dcm_path))
        pdf_bytes = bytes(ds_out.EncapsulatedDocument)
        pdf_path = job_outputs_dir / "result.pdf"
        pdf_path.write_bytes(pdf_bytes)
        logger.info("[%s] Human-readable PDF → %s (%d bytes)", job_id, pdf_path, len(pdf_bytes))
    except Exception as e:
        logger.warning("[%s] Could not save human-readable PDF: %s", job_id, e)

    logger.info("[%s] Processor complete. Job outputs: %s", job_id, job_outputs_dir)

