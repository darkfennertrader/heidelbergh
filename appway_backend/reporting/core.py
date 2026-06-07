"""
Shared report-building engine.

Both weekly_report and manual_report delegate here.  The caller controls:

  include_tests:    whether to gather + include Table C
  advance_state:    whether to persist the new period_end to state.json
                    (only weekly_report sets this to True)
  dry_run:          if True, PDF/zip saved locally; no email sent; no state write
  from_dt / to_dt:  period window (None → derive from state)
  recipients:       override REPORT_RECIPIENTS for this run
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from .audit import AuditRecord, list_audits, list_all_test_audits
from .bundle import build_and_upload_bundle
from .email import send_digest_email
from .pdf import build_report_pdf
from .state import PeriodSummary, ReportState, read_state, write_state

logger = logging.getLogger(__name__)

_PREVIEW_DIR = Path("/home/ubuntu/appway-backend/outputs/_report_preview")


def run_report(
    *,
    include_tests: bool,
    advance_state: bool,
    dry_run: bool = False,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    recipients: list[str] | None = None,
) -> None:
    """
    Core report workflow:

      1. Read state.json (S3) — get last_period_end + cumulative history
      2. Determine reporting window [period_start, period_end]
      3. List clinical audit records in that window
      4. (optional) List live test audit records
      5. Build PDF (Table A + Table B + optional Table C)
      6. Build & upload images zip → get presigned URL
      7. Send email via SES  (unless dry_run)
      8. (optional) Write updated state.json to S3  (only if advance_state)
    """
    now = datetime.now(timezone.utc)
    effective_recipients = recipients if recipients is not None else config.REPORT_RECIPIENTS

    # ── Step 1 — load state ──────────────────────────────────────────────────
    state = read_state()
    period_start = from_dt if from_dt is not None else state.last_period_end
    period_end   = to_dt   if to_dt   is not None else now

    logger.info(
        "Report window: %s  →  %s   (advance_state=%s, include_tests=%s, dry_run=%s)",
        period_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        period_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        advance_state, include_tests, dry_run,
    )

    if period_end <= period_start:
        logger.warning(
            "period_end (%s) is not after period_start (%s) — nothing to report.",
            period_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            period_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        return

    # ── Step 2 — gather clinical records ─────────────────────────────────────
    clinical_rows = list_audits(
        period_start, period_end,
        include_tests=False,
        liveness_check=False,
    )
    logger.info("Clinical analyses in period: %d", len(clinical_rows))

    # ── Step 3 — gather test records (manual only) ───────────────────────────
    test_rows: list[AuditRecord] | None = None
    if include_tests:
        test_rows = list_all_test_audits(liveness_check=True)
        logger.info("Live test analyses: %d", len(test_rows))

    # ── Step 4 — build images zip ─────────────────────────────────────────────
    _zip_key, download_url = build_and_upload_bundle(
        period_end=period_end,
        clinical_rows=clinical_rows,
        test_rows=test_rows,
        dry_run=dry_run,
    )

    # ── Step 5 — build PDF ────────────────────────────────────────────────────
    pdf_bytes = build_report_pdf(
        period_start=period_start,
        period_end=period_end,
        clinical_rows=clinical_rows,
        cumulative_history=state.history,
        test_rows=test_rows,
        generated_at=now,
        download_url=download_url,
    )
    logger.info("PDF built: %.1f KB", len(pdf_bytes) / 1024)

    # Upload report PDF to S3 (skip in dry-run)
    pdf_s3_key: str | None = None
    if not dry_run:
        import boto3
        s3 = boto3.client("s3", region_name=config.AWS_REGION)
        pdf_s3_key = f"{config.REPORT_PREFIX}{period_end.strftime('%Y-%m-%d')}/report.pdf"
        s3.put_object(
            Bucket=config.S3_BUCKET,
            Key=pdf_s3_key,
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
        logger.info("Report PDF uploaded → s3://%s/%s", config.S3_BUCKET, pdf_s3_key)
    else:
        # Dry-run: save PDF locally
        _PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        local_pdf = _PREVIEW_DIR / "report.pdf"
        local_pdf.write_bytes(pdf_bytes)
        logger.info("DRY-RUN: PDF saved locally → %s", local_pdf)

    # ── Step 6 — build subject & send email ──────────────────────────────────
    subject = (
        f"{config.REPORT_SUBJECT_PREFIX} {period_end.strftime('%Y-%m-%d')}"
    )
    pdf_filename = f"mcnv-digest-{period_end.strftime('%Y-%m-%d')}.pdf"

    n_total = len(clinical_rows)
    n_pos   = sum(1 for r in clinical_rows if r.verdict.lower() == "positive")
    n_neg   = n_total - n_pos
    avg_pt  = (sum(r.processing_time_s for r in clinical_rows) / n_total) if n_total else 0.0

    if dry_run:
        logger.info(
            "DRY-RUN: would send email to %s with subject '%s'",
            effective_recipients, subject,
        )
        _print_dry_run_summary(
            period_start=period_start,
            period_end=period_end,
            clinical_rows=clinical_rows,
            test_rows=test_rows,
            download_url=download_url,
            pdf_path=str(_PREVIEW_DIR / "report.pdf"),
            zip_path=str(_PREVIEW_DIR / "images.zip"),
        )
    else:
        send_digest_email(
            recipients=effective_recipients,
            subject=subject,
            period_start=period_start,
            period_end=period_end,
            n_analyses=n_total,
            n_positive=n_pos,
            n_negative=n_neg,
            avg_proc_s=avg_pt,
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
            download_url=download_url,
            has_test_table=(test_rows is not None),
            generated_at=now,
        )

    # ── Step 7 — advance state (weekly_report only) ──────────────────────────
    if advance_state and not dry_run:
        new_period = PeriodSummary(
            period_start=period_start,
            period_end=period_end,
            n_analyses=n_total,
            n_positive=n_pos,
            n_negative=n_neg,
            avg_proc_time_s=avg_pt,
            report_s3_key=pdf_s3_key or "",
        )
        state.history.append(new_period)
        state.last_period_end = period_end
        state.last_report_sent_at = now
        write_state(state)
        logger.info("State advanced: new last_period_end=%s", period_end.strftime("%Y-%m-%dT%H:%M:%SZ"))

    logger.info("Report workflow complete.")


def _print_dry_run_summary(
    *,
    period_start: datetime,
    period_end: datetime,
    clinical_rows: list[AuditRecord],
    test_rows: list[AuditRecord] | None,
    download_url: str | None,
    pdf_path: str,
    zip_path: str,
) -> None:
    """Print a human-readable dry-run summary to stdout."""
    print("\n" + "=" * 70)
    print("DRY-RUN SUMMARY — no email sent, no state updated")
    print("=" * 70)
    print(f"Period:  {period_start.strftime('%Y-%m-%d %H:%M UTC')} "
          f"→ {period_end.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"PDF:     {pdf_path}")
    print(f"ZIP:     {zip_path}")
    print(f"\nTable A — Clinical analyses ({len(clinical_rows)} rows):")
    if clinical_rows:
        print(f"  {'Date':22} {'ID':30} {'Img':>5} {'Pos':>4} {'Neg':>4} {'Proc':>6}  Verdict")
        print("  " + "-" * 80)
        for r in clinical_rows:
            print(
                f"  {r.completed_at.strftime('%Y-%m-%d %H:%M UTC'):22} "
                f"{r.display_id:30} {r.n_images:5} {r.n_positive:4} "
                f"{r.n_negative:4} {r.processing_time_s:6.1f}  {r.verdict}"
            )
    else:
        print("  (none)")

    if test_rows is not None:
        print(f"\nTable C — Live test analyses ({len(test_rows)} rows):")
        if test_rows:
            print(f"  {'Date':22} {'ID':30} {'Img':>5} {'Pos':>4} {'Neg':>4} {'Proc':>6}  Verdict")
            print("  " + "-" * 80)
            for r in test_rows:
                print(
                    f"  {r.completed_at.strftime('%Y-%m-%d %H:%M UTC'):22} "
                    f"{r.display_id:30} {r.n_images:5} {r.n_positive:4} "
                    f"{r.n_negative:4} {r.processing_time_s:6.1f}  {r.verdict}"
                )
        else:
            print("  (none)")

    if download_url:
        print(f"\nDownload URL: {download_url}")
    print("=" * 70 + "\n")
