#!/usr/bin/env bash
# run-weekly-report.sh — wrapper invoked by the systemd timer.
# Runs the official weekly mCNV+ digest report:
#   - Covers the window from last_period_end (state.json) → now
#   - Clinical analyses only (no test jobs)
#   - Sends email to REPORT_RECIPIENTS
#   - Advances state.json so the next weekly run picks up from here

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Activate the project venv and run the weekly report module
exec "$PROJECT_DIR/.venv/bin/python" -m appway_backend.reporting.weekly_report
