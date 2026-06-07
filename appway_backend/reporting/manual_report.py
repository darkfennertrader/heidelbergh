"""
Ad-hoc digest report — run by the operator on demand.

  • Includes test analyses by default (use --no-tests to suppress)
  • NEVER advances state.json — completely read-only against state
  • --dry-run: build PDF + zip locally, print summary, skip email

Usage:
    # Default: period = last_period_end → now, with tests, sends email
    uv run python -m appway_backend.reporting.manual_report

    # Dry-run preview (no email, files saved to outputs/_report_preview/)
    uv run python -m appway_backend.reporting.manual_report --dry-run

    # Clinical-only (no Table C)
    uv run python -m appway_backend.reporting.manual_report --no-tests

    # Specific window
    uv run python -m appway_backend.reporting.manual_report --from 2026-05-01 --to 2026-05-21

    # Override recipients for this run only
    uv run python -m appway_backend.reporting.manual_report --recipients me@x.com,boss@y.com
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD (UTC midnight) or YYYY-MM-DDTHH:MM:SS."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        f"Invalid date '{s}' — expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m appway_backend.reporting.manual_report",
        description=(
            "Build and optionally send an ad-hoc mCNV+ digest report. "
            "Never advances state.json (use weekly_report for that)."
        ),
    )
    parser.add_argument(
        "--from", dest="from_dt", metavar="DATE",
        type=_parse_date, default=None,
        help="Period start (default: last_period_end from state.json)",
    )
    parser.add_argument(
        "--to", dest="to_dt", metavar="DATE",
        type=_parse_date, default=None,
        help="Period end (default: now)",
    )
    parser.add_argument(
        "--no-tests", action="store_true",
        help="Exclude test analyses (omit Table C)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build PDF + zip locally; do not send email; do not update state",
    )
    parser.add_argument(
        "--recipients", metavar="EMAIL[,EMAIL...]",
        default=None,
        help="Override REPORT_RECIPIENTS for this run (comma-separated)",
    )

    args = parser.parse_args(argv)

    recipients: list[str] | None = None
    if args.recipients:
        recipients = [a.strip() for a in args.recipients.split(",") if a.strip()]

    include_tests = not args.no_tests

    logger.info(
        "Manual report: from=%s  to=%s  include_tests=%s  dry_run=%s",
        args.from_dt or "(from state)",
        args.to_dt   or "(now)",
        include_tests,
        args.dry_run,
    )

    from .core import run_report
    run_report(
        include_tests=include_tests,
        advance_state=False,      # NEVER advance state from manual runs
        dry_run=args.dry_run,
        from_dt=args.from_dt,
        to_dt=args.to_dt,
        recipients=recipients,
    )


if __name__ == "__main__":
    main()
