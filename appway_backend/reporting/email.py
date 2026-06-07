"""
SES email sender for the digest report.

Sends a single multipart/mixed email:
  - HTML body with period summary + presigned download link
  - Inline PDF attachment (the digest report)

Uses boto3 SES send_raw_email so we have full control over headers
(From:, Reply-To:, Subject:, attachment filename, etc.)
"""
from __future__ import annotations

import email as stdlib_email
import email.mime.application
import email.mime.multipart
import email.mime.text
import logging
from datetime import datetime, timezone

import boto3

from .. import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HTML body template
# ─────────────────────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{subject}</title>
</head>
<body style="font-family: Arial, Helvetica, sans-serif; color: #1A1A2E;
             max-width: 700px; margin: 0 auto; padding: 24px;">

  <div style="background:#0E86D4; border-radius:8px 8px 0 0; padding:20px 24px;">
    <h1 style="margin:0; color:#fff; font-size:22px;">MyopicCNV+</h1>
    <p  style="margin:4px 0 0; color:#D1ECF1; font-size:14px;">Clinical Analysis Digest</p>
  </div>

  <div style="border:1px solid #B0BEC5; border-top:none; border-radius:0 0 8px 8px;
              padding:24px;">

    <p style="font-size:13px; color:#555;">
      Report generated: <strong>{generated_at}</strong>
    </p>

    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse; margin-bottom:20px;">
      <tr style="background:#0E86D4; color:#fff; font-size:13px; text-align:center;">
        <th style="padding:8px;">Period</th>
        <th style="padding:8px;">Analyses</th>
        <th style="padding:8px;">Positive</th>
        <th style="padding:8px;">Negative</th>
        <th style="padding:8px;">Avg proc (s)</th>
      </tr>
      <tr style="text-align:center; font-size:20px; font-weight:bold;">
        <td style="padding:12px; font-size:12px; color:#555;">{period_label}</td>
        <td style="padding:12px; color:#1A1A2E;">{n_analyses}</td>
        <td style="padding:12px; color:#E53935;">{n_positive}</td>
        <td style="padding:12px; color:#43A047;">{n_negative}</td>
        <td style="padding:12px; color:#1A1A2E; font-size:16px;">{avg_proc_s}</td>
      </tr>
    </table>

    {download_block}

    <hr style="border:none; border-top:1px solid #B0BEC5; margin:20px 0;">

    <p style="font-size:12px; color:#555;">
      The full digest report (PDF) is attached to this email.<br>
      It contains Table A (per-analysis), Table B (cumulative)
      {test_table_note}
    </p>

    <p style="font-size:11px; color:#B0BEC5;">
      AppWay backend &middot; MyopicCNV+ pipeline &middot; Confidential
    </p>

  </div>
</body>
</html>
"""

_DOWNLOAD_BLOCK = """\
    <div style="background:#E3F2FD; border-radius:6px; padding:16px; margin-bottom:16px;">
      <p style="margin:0 0 8px; font-weight:bold; font-size:13px;">
        📥 Download all images for this period
      </p>
      <p style="margin:0 0 8px; font-size:11px; color:#555;">
        This link expires in {ttl_days} days. The zip contains per-analysis
        subfolders, each with the AI report PDF and all OCT images.
      </p>
      <a href="{url}"
         style="display:inline-block; background:#0E86D4; color:#fff;
                padding:10px 20px; border-radius:4px; text-decoration:none;
                font-size:13px; font-weight:bold;">
        Download images.zip
      </a>
      <p style="margin:8px 0 0; font-size:10px; color:#B0BEC5; word-break:break-all;">
        {url}
      </p>
    </div>
"""

_NO_DOWNLOAD_BLOCK = """\
    <p style="font-size:12px; color:#888; font-style:italic;">
      (No image download link for this report.)
    </p>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def send_digest_email(
    *,
    recipients: list[str],
    subject: str,
    period_start: datetime,
    period_end: datetime,
    n_analyses: int,
    n_positive: int,
    n_negative: int,
    avg_proc_s: float,
    pdf_bytes: bytes,
    pdf_filename: str,
    download_url: str | None = None,
    has_test_table: bool = False,
    generated_at: datetime | None = None,
) -> None:
    """
    Send the digest email via SES.  Raises on failure.

    recipients:     list of To: addresses
    pdf_bytes:      the report PDF to attach
    pdf_filename:   e.g. "mcnv-digest-2026-05-28.pdf"
    download_url:   presigned URL for images.zip (None → omit download block)
    has_test_table: if True, mention Table C in the body
    """
    if not recipients:
        logger.warning("No recipients configured — skipping email send.")
        return

    generated_at = generated_at or datetime.now(timezone.utc)

    period_label = (
        f"{period_start.strftime('%Y-%m-%d %H:%M UTC')}"
        f" → {period_end.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    download_block = (
        _DOWNLOAD_BLOCK.format(
            url=download_url,
            ttl_days=config.REPORT_PRESIGNED_TTL_DAYS,
        )
        if download_url
        else _NO_DOWNLOAD_BLOCK
    )

    test_table_note = "and Table C (test analyses)." if has_test_table else "."

    html_body = _HTML_TEMPLATE.format(
        subject=subject,
        generated_at=generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        period_label=period_label,
        n_analyses=n_analyses,
        n_positive=n_positive,
        n_negative=n_negative,
        avg_proc_s=f"{avg_proc_s:.1f}",
        download_block=download_block,
        test_table_note=test_table_note,
    )

    # Build MIME message
    msg = stdlib_email.mime.multipart.MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = config.REPORT_FROM
    msg["To"]      = ", ".join(recipients)

    # HTML part
    alt = stdlib_email.mime.multipart.MIMEMultipart("alternative")
    alt.attach(stdlib_email.mime.text.MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    # PDF attachment
    pdf_part = stdlib_email.mime.application.MIMEApplication(
        pdf_bytes, _subtype="pdf", Name=pdf_filename,
    )
    pdf_part.add_header(
        "Content-Disposition", "attachment", filename=pdf_filename,
    )
    msg.attach(pdf_part)

    # Send via SES
    ses = boto3.client("ses", region_name=config.AWS_REGION)
    response = ses.send_raw_email(
        Source=config.REPORT_FROM,
        Destinations=recipients,
        RawMessage={"Data": msg.as_bytes()},
    )
    message_id = response.get("MessageId", "?")
    logger.info(
        "Digest email sent → %s  SES MessageId=%s",
        ", ".join(recipients), message_id,
    )
