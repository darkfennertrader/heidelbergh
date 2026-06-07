"""
Audit trail helpers.

Per-job audit records are stored as tiny JSON files on S3:

    s3://<bucket>/audit/<YYYY>/<MM>/<DD>/<job-id>.json

Schema:
    {
        "job_id":              str,
        "completed_at":        ISO-8601 UTC string,
        "accession_number":    str | null,
        "study_instance_uid":  str | null,
        "n_images":            int,
        "n_positive":          int,
        "n_negative":          int,
        "verdict":             "Positive" | "Negative" | "Unknown",
        "processing_time_s":   float,
        "is_test":             bool   # true if job_id starts with REPORT_TEST_JOB_PREFIX
    }

Writing:  write_audit_for_job() is called by processor.py at the end of every
          successful job. Idempotent — silently skips if the key already exists.

Reading:  list_audits(from_dt, to_dt, include_tests) yields AuditRecord objects
          for every matching job in chronological order.

For Table C (live test rows) in manual_report we also do a liveness check —
if the corresponding results/<job-id>/result.dcm no longer exists in S3 the
row is silently dropped.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import boto3
from botocore.exceptions import ClientError

from .. import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AuditRecord:
    job_id: str
    completed_at: datetime            # UTC
    accession_number: str | None
    study_instance_uid: str | None
    n_images: int
    n_positive: int
    n_negative: int
    verdict: str                      # "Positive" | "Negative" | "Unknown"
    processing_time_s: float
    is_test: bool

    # ── derived ──────────────────────────────────────────────────────────────

    @property
    def short_job_id(self) -> str:
        """First 8 chars of job_id — used as the display identifier."""
        return self.job_id[:8]

    @property
    def display_id(self) -> str:
        """
        Single display column: 'ACC-2026-00042 · 20260522'
        Falls back to just the short job id if no accession number.
        """
        if self.accession_number:
            return f"{self.accession_number} · {self.short_job_id}"
        return self.short_job_id

    @property
    def folder_name(self) -> str:
        """
        Filesystem-safe name for the per-analysis subfolder inside the zip.
        e.g. 'ACC-2026-00042_20260522' or just '20260522' if no accession.
        """
        if self.accession_number:
            safe_acc = self.accession_number.replace("/", "_").replace("\\", "_")
            return f"{safe_acc}_{self.short_job_id}"
        return self.short_job_id

    # ── serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "completed_at": self.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "accession_number": self.accession_number,
            "study_instance_uid": self.study_instance_uid,
            "n_images": self.n_images,
            "n_positive": self.n_positive,
            "n_negative": self.n_negative,
            "verdict": self.verdict,
            "processing_time_s": round(self.processing_time_s, 2),
            "is_test": self.is_test,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuditRecord":
        return cls(
            job_id=d["job_id"],
            completed_at=datetime.fromisoformat(
                d["completed_at"].replace("Z", "+00:00")
            ).replace(tzinfo=timezone.utc),
            accession_number=d.get("accession_number"),
            study_instance_uid=d.get("study_instance_uid"),
            n_images=int(d.get("n_images", 0)),
            n_positive=int(d.get("n_positive", 0)),
            n_negative=int(d.get("n_negative", 0)),
            verdict=d.get("verdict", "Unknown"),
            processing_time_s=float(d.get("processing_time_s", 0.0)),
            is_test=bool(d.get("is_test", False)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3", region_name=config.AWS_REGION)


def _audit_key(job_id: str, completed_at: datetime) -> str:
    """Build the S3 key for a job's audit JSON."""
    y = completed_at.strftime("%Y")
    m = completed_at.strftime("%m")
    d = completed_at.strftime("%d")
    return f"{config.AUDIT_PREFIX}{y}/{m}/{d}/{job_id}.json"


def _result_key(job_id: str) -> str:
    return f"results/{job_id}/result.dcm"


def _object_exists(s3_client, key: str) -> bool:
    try:
        s3_client.head_object(Bucket=config.S3_BUCKET, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────

def write_audit_for_job(
    *,
    job_id: str,
    completed_at: datetime,
    accession_number: str | None,
    study_instance_uid: str | None,
    n_images: int,
    n_positive: int,
    n_negative: int,
    verdict: str,
    processing_time_s: float,
) -> None:
    """
    Write a per-job audit JSON to S3. Idempotent — silently skips if the
    key already exists (so a retried processor.py call is harmless).

    Called from processor.py at the end of every successful job.
    Never raises — audit failure must not block the main processing flow.
    """
    is_test = job_id.startswith(config.REPORT_TEST_JOB_PREFIX)
    record = AuditRecord(
        job_id=job_id,
        completed_at=completed_at,
        accession_number=accession_number,
        study_instance_uid=study_instance_uid,
        n_images=n_images,
        n_positive=n_positive,
        n_negative=n_negative,
        verdict=verdict,
        processing_time_s=processing_time_s,
        is_test=is_test,
    )
    key = _audit_key(job_id, completed_at)
    s3 = _s3()

    try:
        # Idempotency check
        if _object_exists(s3, key):
            logger.debug("[%s] Audit record already exists at %s — skipping", job_id, key)
            return

        body = json.dumps(record.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        s3.put_object(
            Bucket=config.S3_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
        )
        logger.info("[%s] Audit record written → s3://%s/%s", job_id, config.S3_BUCKET, key)
    except Exception:
        logger.exception("[%s] Failed to write audit record to s3://%s/%s", job_id, config.S3_BUCKET, key)


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def _iter_keys_in_date_range(
    s3_client,
    from_dt: datetime,
    to_dt: datetime,
) -> Iterator[str]:
    """
    Yield every S3 key under AUDIT_PREFIX that falls within [from_dt, to_dt]
    by listing day-level prefixes.  We list one prefix per calendar day so
    the date-partitioned layout gives us cheap, focused scans.
    """
    from datetime import timedelta

    # Normalise to UTC dates
    start_date = from_dt.astimezone(timezone.utc).date()
    end_date   = to_dt.astimezone(timezone.utc).date()

    paginator = s3_client.get_paginator("list_objects_v2")

    current = start_date
    while current <= end_date:
        prefix = f"{config.AUDIT_PREFIX}{current.year:04d}/{current.month:02d}/{current.day:02d}/"
        for page in paginator.paginate(Bucket=config.S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]
        current += timedelta(days=1)


def list_audits(
    from_dt: datetime,
    to_dt: datetime,
    *,
    include_tests: bool = True,
    liveness_check: bool = False,
) -> list[AuditRecord]:
    """
    Return all AuditRecord objects whose completed_at falls in [from_dt, to_dt].

    include_tests:    if False, records with is_test=True are excluded.
    liveness_check:   if True, verify that results/<job-id>/result.dcm still
                      exists in S3 — drop records whose result has been deleted.
                      Used for Table C (live test rows) in manual_report.

    Records are returned sorted by completed_at ascending.
    """
    s3 = _s3()
    records: list[AuditRecord] = []

    for key in _iter_keys_in_date_range(s3, from_dt, to_dt):
        try:
            resp = s3.get_object(Bucket=config.S3_BUCKET, Key=key)
            data = json.loads(resp["Body"].read())
            rec = AuditRecord.from_dict(data)
        except Exception:
            logger.warning("Could not parse audit record at s3://%s/%s — skipping", config.S3_BUCKET, key)
            continue

        # Date filter (the key prefix gives us approximate range; double-check)
        if not (from_dt <= rec.completed_at <= to_dt):
            continue

        # Test filter
        if not include_tests and rec.is_test:
            continue

        # Liveness check (only for test rows typically)
        if liveness_check and rec.is_test:
            if not _object_exists(s3, _result_key(rec.job_id)):
                logger.debug("Test job %s has no live result — dropping from Table C", rec.job_id)
                continue

        records.append(rec)

    records.sort(key=lambda r: r.completed_at)
    return records


def list_all_test_audits(*, liveness_check: bool = True) -> list[AuditRecord]:
    """
    Return all test AuditRecords that exist in S3 (no date filter).
    Used for Table C in manual_report — shows *all* current test analyses.

    liveness_check: if True, only include jobs whose result.dcm is still alive.
    """
    from datetime import timedelta

    # Scan from the epoch of this system (2026-01-01) to far future.
    far_past   = datetime(2026, 1, 1, tzinfo=timezone.utc)
    far_future = datetime(2099, 12, 31, tzinfo=timezone.utc)

    # list_audits will enumerate date-prefixes — for "all" we need a different
    # approach: list all keys under the audit/ prefix unconditionally.
    s3 = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    records: list[AuditRecord] = []

    for page in paginator.paginate(Bucket=config.S3_BUCKET, Prefix=config.AUDIT_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            try:
                resp = s3.get_object(Bucket=config.S3_BUCKET, Key=key)
                data = json.loads(resp["Body"].read())
                rec = AuditRecord.from_dict(data)
            except Exception:
                logger.warning("Could not parse audit record at %s — skipping", key)
                continue

            if not rec.is_test:
                continue

            if liveness_check:
                if not _object_exists(s3, _result_key(rec.job_id)):
                    continue

            records.append(rec)

    records.sort(key=lambda r: r.completed_at)
    return records
