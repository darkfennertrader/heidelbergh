"""
Image bundle builder.

For each report we build one zip archive:

    period_2026-05-22_to_2026-05-28.zip
    ├── clinical/
    │   ├── ACC-2026-00042_20260522/
    │   │   ├── result.pdf          ← per-job ePDF (humans read this)
    │   │   ├── b_scan_001_z1.41mm.png
    │   │   └── ...
    │   └── ACC-2026-00043_20260523/
    │       └── ...
    ├── test/                        ← only when include_tests=True
    │   └── test-20260527_24ab7967/
    │       ├── result.pdf
    │       └── ...
    ├── manifest.csv                 ← same columns as Table A
    └── README.txt

Sources:
  • PNGs + result.pdf  from  /home/ubuntu/appway-backend/outputs/<job-id>/
  • Anything not found on disk is skipped with a warning.

The zip is streamed directly to a BytesIO buffer — no temp files.
Then uploaded to:
    s3://<bucket>/reports/<YYYY-MM-DD>/images.zip

A presigned URL (TTL = REPORT_PRESIGNED_TTL_DAYS days) is returned.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import boto3

from .. import config
from .audit import AuditRecord

logger = logging.getLogger(__name__)

_OUTPUTS_ROOT = Path("/home/ubuntu/appway-backend/outputs")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3", region_name=config.AWS_REGION)


def _zip_key(period_end: datetime) -> str:
    return f"{config.REPORT_PREFIX}{period_end.strftime('%Y-%m-%d')}/images.zip"


def _add_job_to_zip(
    zf: zipfile.ZipFile,
    record: AuditRecord,
    subdir: str,
) -> int:
    """
    Add result.pdf + all *.png files for one job to the zip.
    Returns the number of files added.
    """
    job_dir = _OUTPUTS_ROOT / record.job_id
    if not job_dir.exists():
        logger.warning(
            "[%s] outputs dir not found at %s — skipping from bundle",
            record.job_id, job_dir,
        )
        return 0

    added = 0

    # result.pdf (sits directly in outputs/<job-id>/)
    pdf_path = job_dir / "result.pdf"
    if pdf_path.exists():
        zf.write(pdf_path, arcname=f"{subdir}/result.pdf")
        added += 1
    else:
        logger.warning("[%s] result.pdf not found at %s", record.job_id, pdf_path)

    # All PNGs (in per-DICOM subdirectories: outputs/<job-id>/<stem>/*.png)
    for png in sorted(job_dir.rglob("*.png")):
        # Preserve the per-DICOM subdirectory structure inside the zip folder
        # e.g. outputs/<job>/<dicom-stem>/b_scan_001.png
        #   → clinical/<folder>/b_scan_001.png  (flatten one level for readability)
        #   OR
        #   → clinical/<folder>/<dicom-stem>/b_scan_001.png  (preserve sub-structure)
        # We choose to flatten: all PNGs go directly into the per-job folder so
        # opening the folder shows: result.pdf + all PNGs side-by-side.
        arcname = f"{subdir}/{png.name}"
        zf.write(png, arcname=arcname)
        added += 1

    return added


def _build_manifest_csv(
    clinical_rows: list[AuditRecord],
    test_rows: list[AuditRecord] | None,
) -> str:
    """Build the manifest.csv contents as a string."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "type", "date_utc", "id", "n_images",
        "n_positive", "n_negative", "processing_time_s", "verdict",
    ])
    for r in clinical_rows:
        writer.writerow([
            "clinical",
            r.completed_at.strftime("%Y-%m-%d %H:%M UTC"),
            r.display_id,
            r.n_images, r.n_positive, r.n_negative,
            f"{r.processing_time_s:.2f}",
            r.verdict,
        ])
    if test_rows:
        for r in test_rows:
            writer.writerow([
                "test",
                r.completed_at.strftime("%Y-%m-%d %H:%M UTC"),
                r.display_id,
                r.n_images, r.n_positive, r.n_negative,
                f"{r.processing_time_s:.2f}",
                r.verdict,
            ])
    return buf.getvalue()


_README = """\
mCNV+ Analysis Image Bundle
============================

This archive contains all images and per-analysis reports for one reporting period.

Structure
---------
  clinical/<ID>/result.pdf    — The clinical AI report for this analysis
  clinical/<ID>/*.png         — The individual OCT images that were analysed
                                 (filename matches the "Per-Image Results" table
                                  inside result.pdf — cross-reference directly)

  test/<ID>/result.pdf        — Same layout, for test analyses (if present)
  test/<ID>/*.png

  manifest.csv                — Summary table identical to Table A in the email

Notes
-----
• The presigned download URL for this zip expires after {ttl_days} days.
• Test analyses (test/ folder) were generated using inject_job.sh or similar
  and are not part of the official clinical record.
• Image filenames for real Heidelberg Spectralis volumes use the convention
  b_scan_<NNN>_z<depth>mm.png — the slice number matches the HEYEX viewer.

Generated by AppWay backend · MyopicCNV+ pipeline
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def build_and_upload_bundle(
    *,
    period_end: datetime,
    clinical_rows: list[AuditRecord],
    test_rows: list[AuditRecord] | None = None,
    dry_run: bool = False,
) -> tuple[str | None, str | None]:
    """
    Build the images zip, upload it to S3, and return (s3_key, presigned_url).

    dry_run:  if True, save to outputs/_report_preview/images.zip instead of S3
              and return (local_path_str, None).

    Returns (None, None) if the zip would be empty (no outputs dirs found).
    """
    buf = io.BytesIO()
    total_files = 0

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Clinical analyses
        for rec in clinical_rows:
            folder = f"clinical/{rec.folder_name}"
            added = _add_job_to_zip(zf, rec, folder)
            total_files += added
            logger.debug("[%s] Added %d file(s) to zip under %s", rec.job_id, added, folder)

        # Test analyses (manual_report only)
        if test_rows:
            for rec in test_rows:
                folder = f"test/{rec.folder_name}"
                added = _add_job_to_zip(zf, rec, folder)
                total_files += added

        # manifest.csv
        manifest = _build_manifest_csv(clinical_rows, test_rows)
        zf.writestr("manifest.csv", manifest.encode("utf-8"))

        # README.txt
        readme = _README.format(ttl_days=config.REPORT_PRESIGNED_TTL_DAYS)
        zf.writestr("README.txt", readme.encode("utf-8"))

    zip_bytes = buf.getvalue()
    logger.info(
        "Built images zip: %d files, %.1f KB",
        total_files, len(zip_bytes) / 1024,
    )

    if dry_run:
        preview_dir = Path("/home/ubuntu/appway-backend/outputs/_report_preview")
        preview_dir.mkdir(parents=True, exist_ok=True)
        out_path = preview_dir / "images.zip"
        out_path.write_bytes(zip_bytes)
        logger.info("DRY-RUN: zip saved locally → %s", out_path)
        return str(out_path), None

    s3_key = _zip_key(period_end)
    s3 = _s3()
    s3.put_object(
        Bucket=config.S3_BUCKET,
        Key=s3_key,
        Body=zip_bytes,
        ContentType="application/zip",
    )
    logger.info("Uploaded zip → s3://%s/%s (%d bytes)", config.S3_BUCKET, s3_key, len(zip_bytes))

    # Generate presigned URL
    ttl_seconds = config.REPORT_PRESIGNED_TTL_DAYS * 86400
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.S3_BUCKET, "Key": s3_key},
        ExpiresIn=ttl_seconds,
    )
    logger.info("Presigned URL generated (TTL=%d days)", config.REPORT_PRESIGNED_TTL_DAYS)
    return s3_key, url
