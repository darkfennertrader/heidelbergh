#!/usr/bin/env bash
#
# build_test_dcm.sh — build a synthetic multi-frame Ophthalmic Tomography
# (OPT) DICOM file from a folder of images (JPEG / PNG), matching the
# structure of a real AppWay DICOM so it can be fed straight into
# scripts/inject_job.sh for end-to-end pipeline testing.
#
# Usage:
#   scripts/build_test_dcm.sh --input <dir-of-images> --output <output-dir>
#
# Produces:
#   <output-dir>/test_<6-random-digits>.dcm
#
# How it works (internal Python via the project venv):
#   1. Reads every .jpg/.jpeg/.png from --input (sorted alphabetically)
#   2. Converts each to grayscale (MONOCHROME2, 8-bit) at its NATIVE
#      resolution. The first image's dimensions become the canonical
#      Rows × Columns for the whole DICOM. Any subsequent image whose
#      size does not match is resized with LANCZOS to fit, and a
#      per-image warning is printed showing which files were touched.
#   3. Stacks them as a single multi-frame DICOM (uncompressed, Explicit VR
#      Little Endian, NumberOfFrames = N)
#   4. Copies DICOM-level metadata + the credential block from a hardcoded
#      reference DICOM (docs/gold_samples/20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm)
#   5. Stores the original input filenames inside the DICOM so they survive
#      the round-trip (ImageComments tag + appended to SeriesDescription).
#   6. Assigns fresh StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID
#      so the synthetic file never collides with the reference file.
#
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────
REFERENCE_DCM="${REFERENCE_DCM:-/home/ubuntu/appway-backend/docs/gold_samples/20220509185826_d7a99bf81ff94ecd820bd72f37e11cfc.dcm}"
VENV_PY="${VENV_PY:-/home/ubuntu/appway-backend/.venv/bin/python3}"

INPUT_DIR=""
OUTPUT_DIR=""

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
die() { echo "error: $*" >&2; exit 1; }

usage() {
    sed -n '2,17p' "$0" | sed 's/^#\s\{0,1\}//'
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse args
# ─────────────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --input)     INPUT_DIR="$2"; shift 2 ;;
        --input=*)   INPUT_DIR="${1#*=}"; shift ;;
        --output)    OUTPUT_DIR="$2"; shift 2 ;;
        --output=*)  OUTPUT_DIR="${1#*=}"; shift ;;
        -h|--help)   usage ;;
        *)           die "unknown argument: $1 (use --help)" ;;
    esac
done

[ -n "$INPUT_DIR" ]  || die "missing required --input <dir-of-images>"
[ -n "$OUTPUT_DIR" ] || die "missing required --output <output-dir>"
[ -d "$INPUT_DIR" ]  || die "--input is not a directory: $INPUT_DIR"

mkdir -p "$OUTPUT_DIR"

[ -f "$REFERENCE_DCM" ] || die "reference DICOM not found: $REFERENCE_DCM"
[ -x "$VENV_PY" ]       || die "project venv python not found at $VENV_PY (run 'uv sync' first)"

# Generate 6-digit filename. Use awk+rand to avoid SIGPIPE from tr|head.
RAND6="$(awk 'BEGIN{srand(); printf "%06d", int(rand()*1000000)}')"
OUTPUT_DCM="${OUTPUT_DIR%/}/test_${RAND6}.dcm"

echo "════════════════════════════════════════════════════════════════════════"
echo "  Build synthetic multi-frame OPT DICOM"
echo "  Input images dir : ${INPUT_DIR}"
echo "  Output file      : ${OUTPUT_DCM}"
echo "  Reference DICOM  : ${REFERENCE_DCM}"
echo "════════════════════════════════════════════════════════════════════════"

# ─────────────────────────────────────────────────────────────────────────────
# Run the DICOM builder (embedded Python via project venv)
# ─────────────────────────────────────────────────────────────────────────────
INPUT_DIR="$INPUT_DIR" OUTPUT_DCM="$OUTPUT_DCM" REFERENCE_DCM="$REFERENCE_DCM" \
"$VENV_PY" - <<'PYEOF'
import os, sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}

input_dir     = Path(os.environ["INPUT_DIR"])
output_dcm    = Path(os.environ["OUTPUT_DCM"])
reference_dcm = Path(os.environ["REFERENCE_DCM"])

# 1. Collect images (sorted → deterministic frame order)
image_paths = sorted(
    p for p in input_dir.iterdir()
    if p.is_file() and p.suffix.lower() in IMAGE_EXTS
)
if not image_paths:
    sys.exit(f"error: no image files (jpg/png/…) found in {input_dir}")

# 2. Read reference DICOM (for metadata copy only — NOT for target size;
#    we now use the first input image's native resolution instead).
ref = pydicom.dcmread(str(reference_dcm))

# 3. Determine canonical (rows, cols) from the FIRST image, load every
#    image, resize only mismatches, and build a per-image inventory table.
first_im = Image.open(image_paths[0]).convert("L")
cols, rows = first_im.size  # PIL size is (W, H) = (cols, rows)
print(f"  Canonical frame size (from first image): {rows} rows × {cols} cols")
print(f"  Image inventory ({len(image_paths)} file(s), target: {cols} × {rows}):")

frames    = []
resized   = []   # list of (name, orig_w, orig_h) for the final warning line
for idx, p in enumerate(image_paths):
    im   = first_im if idx == 0 else Image.open(p).convert("L")
    w, h = im.size
    if (w, h) != (cols, rows):
        im = im.resize((cols, rows), Image.LANCZOS)
        resized.append((p.name, w, h))
        print(f"    ⚠  {p.name}   {w} × {h}  → resized to {cols} × {rows}")
    else:
        print(f"    ✓  {p.name}   {w} × {h}")
    frames.append(np.asarray(im, dtype=np.uint8))

pixel_array = np.stack(frames, axis=0)            # shape (N, rows, cols)
n_frames = pixel_array.shape[0]
print(f"  Built pixel array: {pixel_array.shape} dtype={pixel_array.dtype}")
if resized:
    print(f"  ⚠  {len(resized)}/{n_frames} image(s) were resized to match the canonical size:")
    for name, w, h in resized:
        print(f"       - {name}  ({w} × {h} → {cols} × {rows})")
else:
    print(f"  ✓  All {n_frames} images already match the canonical size — no resizing needed.")

# 4. New dataset — fresh UIDs so we never collide with the reference file
now  = datetime.now()
date = now.strftime("%Y%m%d")
time = now.strftime("%H%M%S")

# File meta — uncompressed Explicit VR Little Endian
file_meta = pydicom.dataset.FileMetaDataset()
file_meta.MediaStorageSOPClassUID    = ref.SOPClassUID         # OPT
file_meta.MediaStorageSOPInstanceUID = generate_uid()
file_meta.TransferSyntaxUID          = ExplicitVRLittleEndian
file_meta.ImplementationClassUID     = generate_uid()
file_meta.ImplementationVersionName  = "APPWAY_TEST_BLD"

ds = FileDataset(str(output_dcm), {}, file_meta=file_meta, preamble=b"\x00" * 128)

# 5. Copy DICOM-level metadata from the reference so AppWay Link sees a
#    structurally identical file — EXCEPT identity-bearing UIDs + pixel data.
SKIP_TAGS = {
    "PixelData",
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "NumberOfFrames",
    "Rows",
    "Columns",
    "BitsAllocated",
    "BitsStored",
    "HighBit",
    "PixelRepresentation",
    "SamplesPerPixel",
    "PhotometricInterpretation",
    "PlanarConfiguration",
}
for elem in ref.iterall():
    name = pydicom.datadict.keyword_for_tag(elem.tag)
    if name and name in SKIP_TAGS:
        continue
    try:
        ds.add(elem)
    except Exception:
        pass  # some private tags may fail — non-fatal

# 6. Image-specific fields
ds.SOPClassUID       = ref.SOPClassUID
ds.SOPInstanceUID    = file_meta.MediaStorageSOPInstanceUID
ds.StudyInstanceUID  = generate_uid()
ds.SeriesInstanceUID = generate_uid()

ds.Modality         = getattr(ref, "Modality", "OPT")
ds.Rows             = rows
ds.Columns          = cols
ds.NumberOfFrames   = n_frames
ds.BitsAllocated    = 8
ds.BitsStored       = 8
ds.HighBit          = 7
ds.PixelRepresentation       = 0
ds.SamplesPerPixel           = 1
ds.PhotometricInterpretation = "MONOCHROME2"

ds.ContentDate = date
ds.ContentTime = time
ds.StudyDate   = getattr(ref, "StudyDate", date)
ds.StudyTime   = getattr(ref, "StudyTime", time)
ds.SeriesDate  = date
ds.SeriesTime  = time

# Mark it clearly as a synthetic / test file, AND retain the original input
# filenames inside the DICOM so they survive the round-trip.
#   - SeriesDescription: short-form header (LO, 64-char limit) visible in
#                        any PACS listing. Kept intentionally compact.
#   - ImageComments    : full comma-separated list of source filenames
#                        (LT, 10240-char limit — safe for dozens of frames).
names_csv = ", ".join(p.name for p in image_paths)
ds.SeriesDescription = f"APPWAY TEST ({n_frames}fr from {input_dir.name})"[:64]
ds.ImageComments     = names_csv

# 7. Pixel data — uncompressed, little-endian, row-major
ds.PixelData = pixel_array.tobytes()

# 8. Save
ds.is_little_endian = True
ds.is_implicit_VR   = False
ds.save_as(str(output_dcm), write_like_original=False)
print(f"  ✓ Saved {output_dcm} ({output_dcm.stat().st_size} bytes)")
PYEOF

# ─────────────────────────────────────────────────────────────────────────────
# Final summary
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════════════"
echo "  ✓ DICOM ready: ${OUTPUT_DCM}"
echo
echo "  Feed it into the pipeline with:"
echo "    scripts/inject_job.sh --files ${OUTPUT_DCM}"
echo "════════════════════════════════════════════════════════════════════════"
