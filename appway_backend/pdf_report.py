"""
Public API for the MyopicCNV+ PDF body generator.

Import from here rather than reaching into ``appway_backend.report.*``
directly. This thin shim keeps the public surface stable even if the
internal package layout changes.

Exported names:
    build_pdf(job: ReportJob, out_path: Path) -> Path
    ReportJob
    InputFileInfo
    PerImageResult
"""
from .report.generator import build_pdf, ReportJob, InputFileInfo, PerImageResult

__all__ = ["build_pdf", "ReportJob", "InputFileInfo", "PerImageResult"]
