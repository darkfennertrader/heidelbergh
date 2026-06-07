"""
Persistent reporting state — stored as a single JSON file in S3.

    s3://<bucket>/reports/state.json

Schema:
    {
        "last_period_end":    ISO-8601 UTC string | null,
        "last_report_sent_at": ISO-8601 UTC string | null,
        "history": [
            {
                "period_start":  "2026-05-15T06:00:00Z",
                "period_end":    "2026-05-22T06:00:00Z",
                "n_analyses":    14,
                "n_positive":    2,
                "n_negative":    12,
                "avg_proc_time_s": 4.32,
                "report_s3_key": "reports/2026-05-22/report.pdf"
            },
            ...
        ]
    }

The state file is ONLY written by weekly_report (never by manual_report).
An ETag-based conditional write (If-Match) protects against concurrent
updates — though in practice only one cron job runs at a time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .. import config

logger = logging.getLogger(__name__)

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)   # system go-live date


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PeriodSummary:
    period_start: datetime
    period_end: datetime
    n_analyses: int
    n_positive: int
    n_negative: int
    avg_proc_time_s: float
    report_s3_key: str

    def to_dict(self) -> dict:
        return {
            "period_start":    self.period_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period_end":      self.period_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "n_analyses":      self.n_analyses,
            "n_positive":      self.n_positive,
            "n_negative":      self.n_negative,
            "avg_proc_time_s": round(self.avg_proc_time_s, 2),
            "report_s3_key":   self.report_s3_key,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PeriodSummary":
        return cls(
            period_start=datetime.fromisoformat(d["period_start"].replace("Z", "+00:00")).replace(tzinfo=timezone.utc),
            period_end  =datetime.fromisoformat(d["period_end"].replace("Z", "+00:00")).replace(tzinfo=timezone.utc),
            n_analyses  =int(d["n_analyses"]),
            n_positive  =int(d["n_positive"]),
            n_negative  =int(d["n_negative"]),
            avg_proc_time_s=float(d.get("avg_proc_time_s", 0.0)),
            report_s3_key=d.get("report_s3_key", ""),
        )


@dataclass
class ReportState:
    last_period_end: datetime            # when the last weekly report's window ended
    last_report_sent_at: datetime | None # when the last email was dispatched
    history: list[PeriodSummary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "last_period_end":     self.last_period_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_report_sent_at": (
                self.last_report_sent_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if self.last_report_sent_at else None
            ),
            "history": [p.to_dict() for p in self.history],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReportState":
        lpe = d.get("last_period_end")
        lrsa = d.get("last_report_sent_at")
        return cls(
            last_period_end=datetime.fromisoformat(lpe.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) if lpe else _EPOCH,
            last_report_sent_at=datetime.fromisoformat(lrsa.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) if lrsa else None,
            history=[PeriodSummary.from_dict(p) for p in d.get("history", [])],
        )

    @classmethod
    def new(cls) -> "ReportState":
        """Fresh state for a brand-new deployment (no prior reports)."""
        return cls(last_period_end=_EPOCH, last_report_sent_at=None, history=[])


# ─────────────────────────────────────────────────────────────────────────────
# S3 I/O
# ─────────────────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3", region_name=config.AWS_REGION)


def read_state() -> ReportState:
    """
    Read state.json from S3.  Returns a fresh ReportState if the key does
    not exist yet (first-ever run).  Never raises on missing key.
    """
    s3 = _s3()
    try:
        resp = s3.get_object(Bucket=config.S3_BUCKET, Key=config.REPORT_STATE_KEY)
        data = json.loads(resp["Body"].read())
        state = ReportState.from_dict(data)
        logger.info(
            "Loaded state: last_period_end=%s  history_entries=%d",
            state.last_period_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            len(state.history),
        )
        return state
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            logger.info("No existing state.json — starting fresh.")
            return ReportState.new()
        raise


def write_state(state: ReportState) -> None:
    """
    Persist state.json to S3.  Uses a simple overwrite (not conditional)
    because only weekly_report calls this, and it runs in a single-instance
    environment.  If you ever run multiple workers, add ETag-based locking.
    """
    s3 = _s3()
    body = json.dumps(state.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
    s3.put_object(
        Bucket=config.S3_BUCKET,
        Key=config.REPORT_STATE_KEY,
        Body=body,
        ContentType="application/json; charset=utf-8",
    )
    logger.info(
        "State written → s3://%s/%s (last_period_end=%s)",
        config.S3_BUCKET,
        config.REPORT_STATE_KEY,
        state.last_period_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
