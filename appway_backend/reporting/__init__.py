"""
appway_backend.reporting — weekly and ad-hoc clinical digest reports.

Two entry points:

  weekly_report   — run by the systemd timer every Sunday.
                    Never includes test jobs. Advances state.json.

  manual_report   — run by the operator on-demand.
                    Includes test jobs by default (--no-tests to suppress).
                    NEVER advances state.json (read-only against state).

Shared engine lives in core.py.
"""
