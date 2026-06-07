"""
Weekly digest report — run by the systemd timer every Sunday.

  • Never includes test jobs (Tables A + B only)
  • Advances state.json (last_period_end → now)
  • Idempotent: if last_period_end == now (i.e. re-run within same minute)
    nothing happens.

Usage (systemd / cron):
    uv run python -m appway_backend.reporting.weekly_report
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Weekly report starting.")
    from .core import run_report
    run_report(
        include_tests=False,
        advance_state=True,
        dry_run=False,
    )
    logger.info("Weekly report done.")


if __name__ == "__main__":
    main()
