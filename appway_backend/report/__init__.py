"""
appway_backend.report — PDF layout engine for MyopicCNV+.

Public API surface (re-exported from generator.py):
    build_pdf(job: ReportJob, out_path: Path) -> Path
    ReportJob
    InputFileInfo
    PerImageResult

Everything else in this package (templates, sample data, preview runner)
is an internal implementation detail. Import from
``appway_backend.pdf_report`` (the thin shim at the package root) rather
than reaching into this sub-package directly.
"""
from .generator import build_pdf, ReportJob, InputFileInfo, PerImageResult

__all__ = ["build_pdf", "ReportJob", "InputFileInfo", "PerImageResult"]
