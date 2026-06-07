"""
Digest report PDF generator.

Produces a compact A4 PDF with up to three sections:

    Page 1:  Cover — period, headline numbers
    Page 2+: Table A — per-analysis rows for the current period (clinical only)
    Next:    Table B — cumulative summary, one row per past period (clinical only)
    Last:    Table C — live test analyses (only in manual_report, omitted in weekly)

Uses the Montserrat fonts already bundled in appway_backend/report/assets/.
The layout is deliberately simple (no background template art) so it can be
generated as a standalone document independent of the per-job ePDF.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .audit import AuditRecord
from .state import PeriodSummary

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Font registration
# ─────────────────────────────────────────────────────────────────────────────

_ASSETS = Path(__file__).resolve().parent.parent / "report" / "assets"

_FONTS_REGISTERED = False


def _register_fonts() -> None:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    for name, filename in [
        ("Montserrat",          "Montserrat-Regular.ttf"),
        ("Montserrat-Bold",     "Montserrat-Bold.ttf"),
        ("Montserrat-SemiBold", "Montserrat-SemiBold.ttf"),
        ("Montserrat-Light",    "Montserrat-Light.ttf"),
    ]:
        path = _ASSETS / filename
        if path.exists():
            pdfmetrics.registerFont(TTFont(name, str(path)))
    _FONTS_REGISTERED = True


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette (matches the per-job ePDF brand)
# ─────────────────────────────────────────────────────────────────────────────

_DARK      = colors.HexColor("#1A1A2E")
_BRAND     = colors.HexColor("#0E86D4")
_POSITIVE  = colors.HexColor("#E53935")
_NEGATIVE  = colors.HexColor("#43A047")
_LIGHT_BG  = colors.HexColor("#F5F7FA")
_MID_GREY  = colors.HexColor("#B0BEC5")
_TEST_BG   = colors.HexColor("#FFF8E1")   # warm yellow tint for test table


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _font(bold: bool = False, semi: bool = False) -> str:
    if bold:
        return "Montserrat-Bold"
    if semi:
        return "Montserrat-SemiBold"
    return "Montserrat"


def _verdict_text(verdict: str) -> str:
    return "▲ Positive" if verdict.lower() == "positive" else "● Negative"


def _verdict_color(verdict: str) -> colors.Color:
    return _POSITIVE if verdict.lower() == "positive" else _NEGATIVE


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _analysis_table_data(rows: list[AuditRecord]) -> tuple[list, TableStyle]:
    """Build (data, style) for Table A or Table C."""
    header = [
        "Date (UTC)", "ID", "# Images", "# Pos", "# Neg", "Proc (s)", "Verdict"
    ]
    data = [header]
    for r in rows:
        data.append([
            _fmt_dt(r.completed_at),
            r.display_id,
            str(r.n_images),
            str(r.n_positive),
            str(r.n_negative),
            f"{r.processing_time_s:.1f}",
            _verdict_text(r.verdict),
        ])

    # Column widths (points, A4 = 595pt, margins 2cm each side → 453pt usable)
    col_widths = [120, 130, 48, 36, 36, 46, 64]

    style = TableStyle([
        # Header row
        ("BACKGROUND",    (0, 0), (-1, 0),  _BRAND),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  _font(bold=True)),
        ("FONTSIZE",      (0, 0), (-1, 0),  8),
        ("ALIGN",         (0, 0), (-1, 0),  "CENTER"),
        # Body
        ("FONTNAME",      (0, 1), (-1, -1), _font()),
        ("FONTSIZE",      (0, 1), (-1, -1), 7.5),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, _LIGHT_BG]),
        ("GRID",          (0, 0), (-1, -1), 0.4, _MID_GREY),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        # Numeric columns right-aligned
        ("ALIGN",         (2, 1), (5, -1),  "CENTER"),
    ])

    # Colour the Verdict column per result
    for i, r in enumerate(rows, start=1):
        style.add("TEXTCOLOR", (6, i), (6, i), _verdict_color(r.verdict))
        style.add("FONTNAME",  (6, i), (6, i), _font(bold=True))

    return data, style, col_widths


def _cumulative_table_data(history: list[PeriodSummary]) -> tuple[list, TableStyle, list]:
    """Build (data, style, col_widths) for Table B."""
    header = ["Period", "# Analyses", "# Positive", "# Negative", "Avg Proc (s)"]
    data = [header]
    for p in history:
        data.append([
            f"{_fmt_date(p.period_start)} → {_fmt_date(p.period_end)}",
            str(p.n_analyses),
            str(p.n_positive),
            str(p.n_negative),
            f"{p.avg_proc_time_s:.1f}",
        ])
    # Totals row
    if history:
        total_n  = sum(p.n_analyses  for p in history)
        total_p  = sum(p.n_positive  for p in history)
        total_n2 = sum(p.n_negative  for p in history)
        avg_pt   = sum(p.avg_proc_time_s * p.n_analyses for p in history) / max(total_n, 1)
        data.append([
            f"Since {_fmt_date(history[0].period_start)} (total)",
            str(total_n), str(total_p), str(total_n2),
            f"{avg_pt:.1f}",
        ])

    col_widths = [210, 80, 80, 80, 80]

    style = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),   _DARK),
        ("TEXTCOLOR",     (0, 0), (-1, 0),   colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),   _font(bold=True)),
        ("FONTSIZE",      (0, 0), (-1, 0),   8),
        ("ALIGN",         (0, 0), (-1, 0),   "CENTER"),
        ("FONTNAME",      (0, 1), (-1, -1),  _font()),
        ("FONTSIZE",      (0, 1), (-1, -1),  8),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1),  [colors.white, _LIGHT_BG]),
        ("GRID",          (0, 0), (-1, -1),  0.4, _MID_GREY),
        ("VALIGN",        (0, 0), (-1, -1),  "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1),  4),
        ("BOTTOMPADDING", (0, 0), (-1, -1),  4),
        ("LEFTPADDING",   (0, 0), (-1, -1),  5),
        ("RIGHTPADDING",  (0, 0), (-1, -1),  5),
        ("ALIGN",         (1, 1), (-1, -1),  "CENTER"),
    ])
    # Bold + different bg for totals row
    if len(data) > 2:
        last = len(data) - 1
        style.add("FONTNAME",   (0, last), (-1, last), _font(bold=True))
        style.add("BACKGROUND", (0, last), (-1, last), colors.HexColor("#E3F2FD"))

    return data, style, col_widths


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_report_pdf(
    *,
    period_start: datetime,
    period_end: datetime,
    clinical_rows: list[AuditRecord],
    cumulative_history: list[PeriodSummary],
    test_rows: list[AuditRecord] | None = None,  # None → omit Table C entirely
    generated_at: datetime | None = None,
    download_url: str | None = None,
) -> bytes:
    """
    Build the digest report PDF and return it as bytes.

    clinical_rows:       per-analysis rows for Table A (this period, clinical only)
    cumulative_history:  per-period rows for Table B (all past periods)
    test_rows:           if not None, Table C is appended (manual_report only)
    download_url:        presigned URL for the images.zip (None in dry-run)
    """
    _register_fonts()
    generated_at = generated_at or datetime.now(timezone.utc)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm,  bottomMargin=2*cm,
        title=f"mCNV+ digest {_fmt_date(period_end)}",
        author="AppWay backend",
    )

    styles = getSampleStyleSheet()
    normal_style = ParagraphStyle(
        "Normal", fontName=_font(), fontSize=9, leading=14,
    )
    small_style = ParagraphStyle(
        "Small", fontName=_font(), fontSize=7.5, leading=11, textColor=colors.HexColor("#555555"),
    )
    h1 = ParagraphStyle(
        "H1", fontName=_font(bold=True), fontSize=18, leading=22, textColor=_DARK, spaceAfter=4,
    )
    h2 = ParagraphStyle(
        "H2", fontName=_font(bold=True), fontSize=12, leading=15, textColor=_DARK, spaceBefore=12, spaceAfter=4,
    )
    h3 = ParagraphStyle(
        "H3", fontName=_font(semi=True), fontSize=10, leading=13, textColor=_BRAND, spaceBefore=8, spaceAfter=2,
    )
    url_style = ParagraphStyle(
        "URL", fontName=_font(), fontSize=8, leading=11, textColor=_BRAND,
    )

    story = []

    # ── Cover ─────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph("MyopicCNV+", ParagraphStyle(
        "Brand", fontName=_font(bold=True), fontSize=26, textColor=_BRAND,
    )))
    story.append(Paragraph("Clinical Analysis Digest", ParagraphStyle(
        "Sub", fontName=_font(semi=True), fontSize=14, textColor=_DARK, spaceAfter=2,
    )))
    story.append(HRFlowable(width="100%", thickness=2, color=_BRAND, spaceAfter=6))

    story.append(Paragraph(
        f"Report generated: {_fmt_dt(generated_at)}",
        small_style,
    ))
    story.append(Paragraph(
        f"Period: {_fmt_dt(period_start)}  →  {_fmt_dt(period_end)}",
        normal_style,
    ))
    story.append(Spacer(1, 0.4*cm))

    # Headline numbers
    n_total    = len(clinical_rows)
    n_pos      = sum(1 for r in clinical_rows if r.verdict.lower() == "positive")
    n_neg      = n_total - n_pos
    avg_pt     = (
        sum(r.processing_time_s for r in clinical_rows) / n_total
        if n_total else 0.0
    )

    headline_data = [
        ["Analyses", "Positive", "Negative", "Avg proc (s)"],
        [str(n_total), str(n_pos), str(n_neg), f"{avg_pt:.1f}"],
    ]
    headline_style = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  _BRAND),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  _font(bold=True)),
        ("FONTSIZE",      (0, 0), (-1, 0),  9),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME",      (0, 1), (-1, 1),  _font(bold=True)),
        ("FONTSIZE",      (0, 1), (-1, 1),  22),
        ("TEXTCOLOR",     (0, 1), (0, 1),   _DARK),
        ("TEXTCOLOR",     (1, 1), (1, 1),   _POSITIVE),
        ("TEXTCOLOR",     (2, 1), (2, 1),   _NEGATIVE),
        ("TEXTCOLOR",     (3, 1), (3, 1),   _DARK),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("GRID",          (0, 0), (-1, -1), 0.4, _MID_GREY),
    ])
    story.append(Table(headline_data, colWidths=[113]*4, style=headline_style))
    story.append(Spacer(1, 0.4*cm))

    # Download link
    if download_url:
        story.append(Paragraph(
            "📥 <b>Download all images for this period (7-day link):</b>",
            normal_style,
        ))
        story.append(Paragraph(
            f'<a href="{download_url}" color="#0E86D4">{download_url}</a>',
            url_style,
        ))
    else:
        story.append(Paragraph(
            "(Image download link: generated when report is sent via email)",
            small_style,
        ))

    story.append(PageBreak())

    # ── Table A — Per-analysis (current period) ───────────────────────────────
    story.append(Paragraph("Table A — Clinical Analyses (Current Period)", h2))
    story.append(Paragraph(
        f"{_fmt_dt(period_start)} → {_fmt_dt(period_end)} · {n_total} analysis"
        f"{'es' if n_total != 1 else ''}",
        small_style,
    ))
    story.append(Spacer(1, 0.2*cm))

    if clinical_rows:
        tdata, tstyle, tcols = _analysis_table_data(clinical_rows)
        story.append(Table(tdata, colWidths=tcols, style=tstyle, repeatRows=1))
    else:
        story.append(Paragraph("No clinical analyses in this period.", normal_style))

    story.append(PageBreak())

    # ── Table B — Cumulative ──────────────────────────────────────────────────
    story.append(Paragraph("Table B — Cumulative Clinical Summary", h2))
    story.append(Paragraph("One row per reporting period (clinical cases only).", small_style))
    story.append(Spacer(1, 0.2*cm))

    if cumulative_history:
        cdata, cstyle, ccols = _cumulative_table_data(cumulative_history)
        story.append(Table(cdata, colWidths=ccols, style=cstyle, repeatRows=1))
    else:
        story.append(Paragraph("No prior periods — this is the first report.", normal_style))

    # ── Table C — Live test analyses (manual only) ────────────────────────────
    if test_rows is not None:
        story.append(PageBreak())
        story.append(Paragraph("Table C — Test Analyses (Currently in S3)", h2))
        story.append(Paragraph(
            "These are analyses whose job_id starts with the test- prefix. "
            "They are excluded from Tables A and B. "
            "Rows disappear automatically once cleanup_test_jobs.sh removes the S3 artefacts.",
            small_style,
        ))
        story.append(Spacer(1, 0.2*cm))

        if test_rows:
            tdata, tstyle, tcols = _analysis_table_data(test_rows)
            # Tint the header for the test table so it's visually distinct
            tstyle.add("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F57F17"))
            story.append(Table(tdata, colWidths=tcols, style=tstyle, repeatRows=1))
        else:
            story.append(Paragraph("No test analyses currently in S3.", normal_style))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.6*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_MID_GREY))
    story.append(Spacer(1, 0.15*cm))
    story.append(Paragraph(
        "Generated by AppWay backend · MyopicCNV+ pipeline · Confidential",
        ParagraphStyle("Footer", fontName=_font(), fontSize=7, textColor=_MID_GREY),
    ))

    doc.build(story)
    return buf.getvalue()
