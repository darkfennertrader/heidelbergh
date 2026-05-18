#!/usr/bin/env python3
"""
scripts/job_timeline.py — AppWay end-to-end job timeline

Assembles a human-readable, timestamped log of every stage a job passes
through — from DICOM received on HEYEX 2 all the way to the result stored
back in HEYEX — and appends it to  logs/workflow.logs.

Stages covered
──────────────
  ①  DICOM received by AppWay Link               heyex2 AppWay Link log
  ②  DICOM uploaded to S3 incoming/              S3 object LastModified
  ③  Job message enqueued on SQS appway-jobs     (derived: ~= stage ②)
  ④  Input downloaded by backend                 backend journalctl / STAGE 4/9
  ⑤  Backend processes (YOLO + ePDF)             backend journalctl / STAGE 5/9
  ⑥  Result uploaded to S3 results/             S3 object LastModified + STAGE 6/9
  ⑦  Result message enqueued on appway-results   backend journalctl / STAGE 7/9
  ⑧  Result downloaded by AppWay Link            heyex2 AppWay Link log
  ⑨  Result stored into HEYEX                    heyex2 MCAshvinsWorkstation log
  ✗  (if present) user-click failure             heyex2 MCAshvinsWorkstation log

Usage
─────
  # Auto-detect newest job and watch live (recommended after dragging DCM into HEYEX):
  python scripts/job_timeline.py --live

  # Auto-detect, one-shot (picks the newest existing job in S3 incoming/):
  python scripts/job_timeline.py

  # Explicit job id, one-shot:
  python scripts/job_timeline.py <job-id>

  # Explicit job id, live mode (re-query every 5 s, redraw until stage ⑨ seen or Ctrl-C):
  python scripts/job_timeline.py <job-id> --live

  # UTC-only timestamps (default: CEST primary + UTC secondary):
  python scripts/job_timeline.py <job-id> --utc-only

  # Custom poll interval (--live only, seconds):
  python scripts/job_timeline.py <job-id> --live --interval 10

Environment
───────────
  Reads the same .env that the backend worker uses.
  Needs: AWS_REGION, S3_BUCKET (defaults to appway-bridge-prod),
         SSM access to heyex2 (i-02a7dd1797d85a099).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── ensure project root is importable (for config / dotenv) ────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv optional

import boto3
from botocore.exceptions import ClientError

# ── constants ──────────────────────────────────────────────────────────────
AWS_REGION   = os.getenv("AWS_REGION", "eu-west-1")
S3_BUCKET    = os.getenv("S3_BUCKET",  "appway-bridge-prod")
HEYEX2_INSTANCE = "i-02a7dd1797d85a099"
BACKEND_INSTANCE = "i-02a99abeba370f0a7"

CEST = timezone(timedelta(hours=2), "CEST")   # Europe/Berlin summer time (UTC+2)

LOGS_DIR  = _PROJECT_ROOT / "logs"
LOG_FILE  = LOGS_DIR / "workflow.logs"

SSM_TIMEOUT = 30   # seconds to wait for an SSM command to complete

# ── timezone helpers ────────────────────────────────────────────────────────

def _to_cest(dt: datetime) -> datetime:
    """Convert any aware datetime to CEST (UTC+2)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CEST)


def _fmt_ts(dt: datetime, utc_only: bool = False) -> str:
    """
    Format a timestamp.

      default   → '2026-05-18 00:45:39 CEST  (22:45:39 UTC)'
      utc_only  → '2026-05-18 22:45:39 UTC'
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    if utc_only:
        return utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    cest = _to_cest(dt)
    return (
        cest.strftime("%Y-%m-%d %H:%M:%S CEST")
        + "  ("
        + utc.strftime("%H:%M:%S UTC")
        + ")"
    )


def _fmt_delta(seconds: float) -> str:
    """Format elapsed seconds as +HH:MM:SS."""
    s = int(abs(seconds))
    h, rem = divmod(s, 3600)
    m, sc  = divmod(rem, 60)
    return f"+{h:02d}:{m:02d}:{sc:02d}"


def _fmt_size(n: int) -> str:
    """Human-readable file size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.2f} MB"


# ── AWS helpers ─────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3", region_name=AWS_REGION)


def _ssm():
    return boto3.client("ssm", region_name=AWS_REGION)


def discover_job_id(since: Optional[datetime] = None) -> Optional[tuple[str, datetime]]:
    """
    Find the most recent job_id in S3 incoming/.

    If *since* is given (UTC-aware datetime), only jobs whose newest S3 object
    was uploaded **after** that moment are considered.  This lets --live mode
    wait for a brand-new job to appear.

    Returns (job_id, last_modified_utc) or None when no qualifying job exists.
    """
    objs = s3_list_prefix("incoming/")
    # key format: incoming/<job_id>/<filename>
    by_job: dict[str, datetime] = {}
    for o in objs:
        parts = o["key"].split("/", 2)
        if len(parts) < 3:
            continue
        jid = parts[1]
        ts  = o["last_modified"]
        if since is not None and ts <= since:
            continue
        if jid not in by_job or ts > by_job[jid]:
            by_job[jid] = ts
    if not by_job:
        return None
    jid, ts = max(by_job.items(), key=lambda kv: kv[1])
    return jid, ts


def s3_list_prefix(prefix: str) -> list[dict]:
    """Return all S3 objects under prefix as list of {key, size, last_modified}."""
    client = _s3()
    results = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"] == prefix:
                continue  # skip placeholder directory object
            results.append({
                "key":           obj["Key"],
                "size":          obj["Size"],
                "last_modified": obj["LastModified"],  # always UTC-aware from boto3
            })
    return results


def s3_head(key: str) -> Optional[dict]:
    """Return {size, last_modified} for a single S3 key, or None if not found."""
    try:
        resp = _s3().head_object(Bucket=S3_BUCKET, Key=key)
        return {
            "size":          resp["ContentLength"],
            "last_modified": resp["LastModified"],
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


# ── SSM helper ───────────────────────────────────────────────────────────────

def ssm_run(instance_id: str, command: str, timeout: int = SSM_TIMEOUT) -> str:
    """
    Run a shell command on an EC2 instance via AWS SSM and return stdout.
    Returns an empty string on error / timeout rather than raising.

    For Windows instances the command must be PowerShell syntax — pass
    document_name="AWS-RunPowerShellScript".
    For Linux, document_name="AWS-RunShellScript".
    """
    # Auto-detect platform by instance prefix heuristics is tricky;
    # we know exactly which is which, so hard-code doc name per instance.
    is_windows = (instance_id == HEYEX2_INSTANCE)
    doc = "AWS-RunPowerShellScript" if is_windows else "AWS-RunShellScript"

    client = _ssm()
    try:
        resp = client.send_command(
            InstanceIds=[instance_id],
            DocumentName=doc,
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout,
        )
        command_id = resp["Command"]["CommandId"]
    except Exception as exc:
        return f"[SSM send_command failed: {exc}]"

    # Poll for completion
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            inv = client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
            status = inv["Status"]
            if status in ("Success", "Failed", "Cancelled", "TimedOut"):
                return (inv.get("StandardOutputContent") or "").strip()
        except client.exceptions.InvocationDoesNotExist:
            continue
        except Exception as exc:
            return f"[SSM poll failed: {exc}]"

    return "[SSM timed out]"


# ── backend log grep ─────────────────────────────────────────────────────────
#
# The worker is configured with:
#   StandardOutput=append:/var/log/appway-worker.log
# Log line format:
#   2026-05-18T05:37:24 [INFO] appway_backend.worker: [job_id] message
# Timestamps are UTC (no tz suffix).

_WORKER_LOG = Path("/var/log/appway-worker.log")

# Local log line regex:  "2026-05-18T05:37:24 [INFO] module: msg"
_WORKER_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+\[(?:INFO|WARNING|ERROR|DEBUG)\]\s+\S+:\s+(.*)"
)


def journal_grep(job_id: str) -> list[tuple[datetime, str]]:
    """
    Pull appway-worker log lines that contain job_id.
    Returns list of (utc_datetime, message) sorted by time.

    Tries local /var/log/appway-worker.log first.
    Falls back to SSM grep on BACKEND_INSTANCE if not available locally.
    """
    lines = _local_worker_log_grep(job_id)
    if lines is None:
        lines = _ssm_worker_log_grep(job_id)
    return lines


def _local_worker_log_grep(job_id: str) -> Optional[list[tuple[datetime, str]]]:
    """Read /var/log/appway-worker.log locally if it exists."""
    if not _WORKER_LOG.exists():
        return None
    try:
        raw = _WORKER_LOG.read_text(errors="replace")
        return _parse_worker_log_lines(raw, job_id)
    except Exception:
        return None


def _ssm_worker_log_grep(job_id: str) -> list[tuple[datetime, str]]:
    """Grep the backend worker log via SSM (Linux)."""
    cmd = f"grep -F '{job_id}' /var/log/appway-worker.log 2>/dev/null | tail -500"
    raw = ssm_run(BACKEND_INSTANCE, cmd, timeout=60)
    if raw.startswith("[SSM"):
        return []
    return _parse_worker_log_lines(raw, job_id)


def _parse_worker_log_lines(raw: str, job_id: str) -> list[tuple[datetime, str]]:
    out = []
    for line in raw.splitlines():
        if job_id not in line:
            continue
        m = _WORKER_LOG_RE.match(line)
        if not m:
            continue
        ts_str, msg = m.group(1), m.group(2)
        try:
            # Timestamps in the log are UTC (no tz suffix)
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((dt, msg))
    out.sort(key=lambda x: x[0])
    return out


# ── heyex2 log grep ──────────────────────────────────────────────────────────

_HEYEX_LOG_DIR = r"C:\HEYEX\logfiles"   # note: lowercase on this instance
_APPWAY_LOG_DIR = r"C:\HEYEX\AshvinsDistribution"   # AppWay/AI Marketplace drop dir

_HEYEX_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)"  # "2026-05-17 23:02:02.229"
)


def _parse_heyex_ts(ts_str: str) -> Optional[datetime]:
    """
    Parse a HEYEX/AppWay timestamp that is in LOCAL TIME (CEST = UTC+2).
    Returns UTC-aware datetime.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            local_dt = datetime.strptime(ts_str.strip(), fmt)
            # HEYEX 2 is configured as CEST (UTC+2)
            cest_dt = local_dt.replace(tzinfo=CEST)
            return cest_dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _heyex_grep(job_id: str, result_filename: str, job_origin_ts: Optional[datetime] = None) -> dict:
    """
    Grep heyex2 Windows logs via SSM PowerShell.

    Returns a dict with optional keys:
      'dicom_received'  : (utc_dt, filepath, size_bytes)
      'result_stored'   : (utc_dt, filepath, size_bytes)
      'click_error'     : (utc_dt, bad_path, error_msg)
    """
    found: dict = {}

    # ── MCAshvinsWorkstation.verbose.log — result stored + click error ──────
    # The verbose.log has lines like:
    #   2026-05-18 07:47:41.967  10756  166  MiiiDcmFile  constructor  error, couldn't open file \\host\ImagwPool\...
    #   2026-05-18 07:47:41.978  10756  166  MCLogFile    LogException ...  prepare \\host\ImagwPool\...
    # We target lines with "couldn't open file" (has timestamp + UNC path on same line)
    # and lines with "ImagwPool" but NOT "couldn't" (= successful store).
    ps_heyex = (
        r'$log = "' + _HEYEX_LOG_DIR + r'\MCAshvinsWorkstation.verbose.log"; '
        r'if (Test-Path $log) { '
        r'  Get-Content $log | Select-String -Pattern "couldn''t open file|ImagwPool" | Select-Object -Last 40 '
        r'} else { "LOG_NOT_FOUND" }'
    )
    heyex_out = ssm_run(HEYEX2_INSTANCE, ps_heyex)

    for line in heyex_out.splitlines():
        ts_m = _HEYEX_TS_RE.search(line)
        ts = _parse_heyex_ts(ts_m.group(1)) if ts_m else None

        path_m = re.search(r'(\\\\[^\s\t<>]+\.dcm|C:\\[^\s\t<>]+\.dcm)', line, re.IGNORECASE)

        # "couldn't open file" → WebView2 click error (has timestamp + UNC path)
        # Only record errors within ±2h of the job to avoid stale entries from old jobs.
        if "couldn't open file" in line.lower() or "could not read the dicom" in line.lower():
            bad_path = path_m.group(1) if path_m else "(unknown path)"
            if "click_error" not in found and ts is not None:
                if job_origin_ts is None or abs((ts - job_origin_ts).total_seconds()) < 7200:
                    found["click_error"] = (ts, bad_path, line.strip()[:200])

        # ImagwPool without error → result was written/stored in HEYEX (has timestamp)
        elif "ImagwPool" in line and ts is not None and "result_stored" not in found:
            path = path_m.group(1) if path_m else "?"
            found["result_stored"] = (ts, path, None)

    # ── AshvinsDistribution dir — infer ① and ⑧ from file timestamps ───────
    # AppWay Link drops the incoming ZIP and sends back a .rtc.dcm response.
    # The ZIP arrival ≈ stage ⑧ (result downloaded), the .rtc.dcm ≈ stage ①
    # For stage ①: match the incoming DICOM upload time (≈ S3 stage ② time)
    # We infer from AshvinsDistribution file timestamps near our job time.
    ps_appway = (
        r'$dir = "' + _APPWAY_LOG_DIR + r'"; '
        r'if (Test-Path $dir) { '
        r'  Get-ChildItem $dir -ErrorAction SilentlyContinue | '
        r'  Sort-Object LastWriteTime -Descending | Select-Object -First 10 | '
        r'  Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize '
        r'} else { "DIR_NOT_FOUND" }'
    )
    appway_out = ssm_run(HEYEX2_INSTANCE, ps_appway)

    # Parse the table output to find ZIP file (= result downloaded by AppWay Link)
    # Format: "Name   Length   LastWriteTime"
    for line in appway_out.splitlines():
        # Look for a .zip file (AppWay result delivery)
        if ".zip" in line.lower() and "result_downloaded" not in found:
            # Extract timestamp from Format-Table output: last column is date/time
            ts_m = re.search(r'(\d+/\d+/\d{4}\s+\d+:\d+:\d+\s+[AP]M)', line)
            if ts_m:
                try:
                    local_dt = datetime.strptime(ts_m.group(1), "%m/%d/%Y %I:%M:%S %p")
                    ts = local_dt.replace(tzinfo=CEST).astimezone(timezone.utc)
                    found["result_downloaded"] = (ts, line.strip())
                except ValueError:
                    pass

        # Look for .rtc.dcm (AppWay Link confirmation sent back to HEYEX → ≈ stage ⑨)
        if ".rtc.dcm" in line.lower() and "result_stored" not in found:
            ts_m = re.search(r'(\d+/\d+/\d{4}\s+\d+:\d+:\d+\s+[AP]M)', line)
            if ts_m:
                try:
                    local_dt = datetime.strptime(ts_m.group(1), "%m/%d/%Y %I:%M:%S %p")
                    ts = local_dt.replace(tzinfo=CEST).astimezone(timezone.utc)
                    found["result_stored"] = (ts, f"AshvinsDistribution/{line.split()[0]}", None)
                except ValueError:
                    pass

    # Stage ① (DICOM received by AppWay Link): infer from incoming S3 time + small offset
    # AppWay Link polls SQS, so ① ≈ ② + SQS polling delay (typically < 30s)
    # We mark it as "inferred" if we can't find it in logs directly.

    return found


def _ssm_file_size(win_path: str) -> Optional[int]:
    """Get file size in bytes for a Windows path via SSM, or None."""
    ps = f'(Get-Item "{win_path}" -ErrorAction SilentlyContinue).Length'
    out = ssm_run(HEYEX2_INSTANCE, ps).strip()
    try:
        return int(out)
    except (ValueError, TypeError):
        return None


# ── timeline builder ─────────────────────────────────────────────────────────

class Stage:
    def __init__(
        self,
        number: str,
        label: str,
        ts: Optional[datetime] = None,
        detail: str = "",
    ):
        self.number = number   # e.g. "①" or "✗"
        self.label  = label
        self.ts     = ts       # UTC-aware datetime or None
        self.detail = detail   # extra path / size info


def build_timeline(job_id: str) -> list[Stage]:
    """
    Query all sources and return a list of Stage objects in chronological order.
    Stages with ts=None are listed as '(not yet seen)'.
    """
    stages: list[Stage] = []

    incoming_prefix = f"incoming/{job_id}/"
    result_key      = f"results/{job_id}/result.dcm"

    # ── S3: stage ② — DICOM in incoming/ ────────────────────────────────────
    s3_incoming = s3_list_prefix(incoming_prefix)
    if s3_incoming:
        first = min(s3_incoming, key=lambda x: x["last_modified"])
        detail_parts = [f"s3://{S3_BUCKET}/{first['key']}  ({_fmt_size(first['size'])})"]
        if len(s3_incoming) > 1:
            total = sum(o["size"] for o in s3_incoming)
            detail_parts.append(f"  … {len(s3_incoming)} file(s) total, {_fmt_size(total)}")
        stages.append(Stage("②", "DICOM uploaded to S3", first["last_modified"], "\n            ".join(detail_parts)))
    else:
        stages.append(Stage("②", "DICOM uploaded to S3", None, f"s3://{S3_BUCKET}/{incoming_prefix}  (not found)"))

    # ── S3: stage ⑥ — result in results/ ────────────────────────────────────
    s3_result = s3_head(result_key)
    if s3_result:
        stages.append(Stage("⑥", "ePDF result uploaded to S3", s3_result["last_modified"],
                            f"s3://{S3_BUCKET}/{result_key}  ({_fmt_size(s3_result['size'])})"))
    else:
        stages.append(Stage("⑥", "ePDF result uploaded to S3", None,
                            f"s3://{S3_BUCKET}/{result_key}  (not yet present)"))

    # ── Backend journal: stages ④, ⑤, ⑦ ────────────────────────────────────
    journal = journal_grep(job_id)

    def _find_journal(pattern: str) -> Optional[datetime]:
        for dt, msg in journal:
            if pattern in msg:
                return dt
        return None

    def _find_journal_kv(pattern: str, key: str) -> Optional[str]:
        for _, msg in journal:
            if pattern in msg:
                m = re.search(key + r"=(\S+)", msg)
                if m:
                    return m.group(1)
        return None

    # ── Stage ④ — backend download ─────────────────────────────────────────
    # Match: "Downloaded N file(s) from s3://…/incoming/job_id/" OR "Downloading input"
    dl_ts = _find_journal("Downloaded") or _find_journal("Downloading input")
    dl_detail = ""
    for _, msg in journal:
        if "Downloaded" in msg and "incoming/" in msg:
            # "Downloaded 1 file(s) from s3://bucket/incoming/job_id/"
            m = re.search(r"Downloaded (\d+) file\(s\) from (s3://\S+)", msg)
            if m:
                dl_detail = f"{m.group(2)}  ({m.group(1)} file(s))"
            break
    stages.append(Stage("④", "Input downloaded by backend", dl_ts, dl_detail))

    # ── Stage ⑤ — processing ───────────────────────────────────────────────
    # Start: "Processing…"  Done: "Processor complete" or "Inference result"
    proc_start = _find_journal("Processing\u2026") or _find_journal("Processing DICOM")
    proc_done  = _find_journal("Processor complete") or _find_journal("ePDF DICOM saved")
    proc_detail = ""
    if proc_start and proc_done:
        elapsed = (proc_done - proc_start).total_seconds()
        proc_detail = f"duration {elapsed:.1f}s"
    # Look for inference result detail
    for _, msg in journal:
        if "Inference result:" in msg:
            proc_detail = msg.split("] ", 1)[-1] if "] " in msg else msg
            break
    stages.append(Stage("⑤", "Backend processes (YOLO + ePDF)", proc_done or proc_start, proc_detail))

    # ── Stage ⑥ upload + ⑦ enqueue ────────────────────────────────────────
    up_ts = _find_journal("Uploaded 1 file") or _find_journal("Uploading output")
    up_detail = ""
    for _, msg in journal:
        if "Uploaded" in msg and "results/" in msg:
            m = re.search(r"Uploaded \d+ file\(s\) to (s3://\S+)", msg)
            if m:
                up_detail = f"{m.group(1)}"
            break

    # Merge with S3 LastModified (S3 is authoritative for timing)
    if not up_ts and s3_result:
        up_ts = s3_result["last_modified"]
    if s3_result and not up_detail:
        up_detail = f"s3://{S3_BUCKET}/{result_key}  ({_fmt_size(s3_result['size'])})"
    # Update stage ⑥ in place with journal detail if we got it
    for st in stages:
        if st.number == "⑥" and not st.detail.startswith("(not"):
            if up_detail:
                st.detail = up_detail
            if up_ts and not st.ts:
                st.ts = up_ts

    enq_ts = _find_journal("Sent result message")
    enq_detail = ""
    for _, msg in journal:
        if "Sent result message" in msg:
            enq_detail = msg.split("] ", 1)[-1] if "] " in msg else msg
            break
    stages.append(Stage("⑦", "Result enqueued on SQS appway-results", enq_ts, enq_detail))

    # ── SSM: heyex2 stages ①, ⑧, ⑨ ─────────────────────────────────────────
    # result_fname: used to grep the HEYEX verbose.log for the stored DCM.
    # AppWay Link names the stored file with a timestamp, not our job_id.
    # We leave result_fname empty and rely on ImagwPool pattern matching.
    result_fname = ""

    # Provide S3 incoming timestamp as the "job origin" for heyex time-window filtering
    job_origin_ts = s3_incoming[0]["last_modified"] if s3_incoming else None
    heyex_data = _heyex_grep(job_id, result_fname, job_origin_ts=job_origin_ts)

    # Stage ① — DICOM received
    if "dicom_received" in heyex_data:
        ts, path, size = heyex_data["dicom_received"]
        sz_str = _fmt_size(size) if size else "?"
        stages.append(Stage("①", "DICOM received by AppWay Link", ts, f"{path}  ({sz_str})"))
    else:
        stages.append(Stage("①", "DICOM received by AppWay Link", None, "(not found in AppWay Link logs)"))

    # Stage ⑧ — result downloaded
    if "result_downloaded" in heyex_data:
        ts, raw = heyex_data["result_downloaded"]
        stages.append(Stage("⑧", "Result downloaded by AppWay Link", ts, raw[:120]))
    else:
        stages.append(Stage("⑧", "Result downloaded by AppWay Link", None, "(not found in AppWay Link logs)"))

    # Stage ⑨ — stored in HEYEX
    if "result_stored" in heyex_data:
        ts, path, size = heyex_data["result_stored"]
        sz_str = _fmt_size(size) if size else "?"
        stages.append(Stage("⑨", "Result stored in HEYEX", ts, f"{path}  ({sz_str})"))
    else:
        stages.append(Stage("⑨", "Result stored in HEYEX", None, "(not found in MCAshvinsWorkstation log)"))

    # Stage ✗ — click error (optional)
    if "click_error" in heyex_data:
        ts, bad_path, msg = heyex_data["click_error"]
        stages.append(Stage("✗", "User-click failure (ThreadLoadDICOMReport)", ts,
                            f"Tried to open: {bad_path}\n            {msg[:200]}"))

    # ── Sort by timestamp (Nones go to end) ─────────────────────────────────
    def _sort_key(st: Stage):
        if st.ts is None:
            return datetime.max.replace(tzinfo=timezone.utc)
        return st.ts

    stages.sort(key=_sort_key)
    return stages


# ── renderer ─────────────────────────────────────────────────────────────────

def render_timeline(job_id: str, stages: list[Stage], utc_only: bool = False) -> str:
    """Render stages to a human-readable string."""
    lines: list[str] = []
    sep = "═" * 72

    lines.append(sep)
    lines.append(f"  AppWay job timeline:  {job_id}")
    now_str = _fmt_ts(datetime.now(timezone.utc), utc_only)
    lines.append(f"  Generated at: {now_str}")
    lines.append(sep)

    # Find the first real timestamp (origin for Δ)
    origin: Optional[datetime] = None
    for st in stages:
        if st.ts is not None and st.number != "✗":
            origin = st.ts
            break

    for st in stages:
        if st.ts is not None:
            ts_str  = _fmt_ts(st.ts, utc_only)
            delta_s = (st.ts - origin).total_seconds() if origin else 0.0
            delta   = _fmt_delta(delta_s)
        else:
            ts_str = "(not yet seen)"
            delta  = "+??:??:??"

        prefix = f"[{delta}]  {ts_str}"
        header = f"  {prefix}   {st.number} {st.label}"
        lines.append(header)
        if st.detail:
            for dl in st.detail.split("\n"):
                lines.append(f"            {dl}")
        lines.append("")

    # Total elapsed
    seen = [st for st in stages if st.ts is not None and st.number not in ("✗",)]
    if len(seen) >= 2:
        total_s = (seen[-1].ts - seen[0].ts).total_seconds()
        m, s = divmod(int(total_s), 60)
        lines.append(f"  Total elapsed: {m}m {s:02d}s   ({seen[0].number} → {seen[-1].number})")

    lines.append(sep)
    return "\n".join(lines)


# ── append to logs/workflow.logs ─────────────────────────────────────────────

def append_to_log(content: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write(content)
        fh.write("\n")


# ── ANSI clear-line helper for --live redraw ─────────────────────────────────

def _clear_screen():
    # Move cursor to top-left and clear the screen (works in most terminals)
    print("\033[H\033[J", end="", flush=True)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AppWay end-to-end job timeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("job_id", nargs="?", default=None,
                        help="Job ID (e.g. final-…). If omitted, auto-detected from S3 incoming/.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--one-shot", action="store_true", default=True,
                            help="Query once, print, append to logs/workflow.logs, exit (default)")
    mode_group.add_argument("--live", action="store_true",
                            help="Re-query every INTERVAL seconds; exit when stage ⑨ is seen or Ctrl-C")
    parser.add_argument("--interval", type=int, default=5, metavar="SEC",
                        help="Re-poll interval for --live mode (default: 5 s)")
    parser.add_argument("--utc-only", action="store_true",
                        help="Show UTC timestamps only (default: CEST primary + UTC secondary)")
    parser.add_argument("--no-heyex", action="store_true",
                        help="Skip SSM queries to heyex2 (faster, backend-only view)")
    args = parser.parse_args()

    # --live sets one-shot=False
    if args.live:
        args.one_shot = False

    # ── resolve job_id ───────────────────────────────────────────────────────
    job_id = args.job_id.strip() if args.job_id else None

    if job_id is None and not args.live:
        # One-shot auto-detect: pick the newest job already in S3
        result = discover_job_id()
        if result is None:
            print("  ✗ No jobs found in s3://appway-bridge-prod/incoming/ — nothing to show.", file=sys.stderr)
            sys.exit(1)
        job_id, ts = result
        print(f"  Auto-detected job: {job_id}  (uploaded {_fmt_ts(ts)})")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _run_once(jid: str) -> tuple[str, bool]:
        """Query, render, return (rendered_text, stage9_seen)."""
        stages = build_timeline(jid)
        text = render_timeline(jid, stages, utc_only=args.utc_only)
        stage9_seen = any(
            st.number == "⑨" and st.ts is not None for st in stages
        )
        return text, stage9_seen

    if not args.live:
        # ── one-shot ─────────────────────────────────────────────────────────
        text, _ = _run_once(job_id)
        print(text)
        append_to_log(text)
        print(f"\n  ↳ Appended to {LOG_FILE}")
    else:
        # ── live mode ────────────────────────────────────────────────────────
        if job_id:
            # Explicit job_id supplied — go straight to timeline view
            print(f"  Live mode — polling every {args.interval}s  (Ctrl-C to stop)")
            iteration = 0
            text = ""
            try:
                while True:
                    iteration += 1
                    text, done = _run_once(job_id)
                    _clear_screen()
                    print(text)
                    print(f"\n  [live]  iteration #{iteration}  |  next refresh in {args.interval}s  |  Ctrl-C to stop")
                    if done:
                        append_to_log(text)
                        print(f"\n  ✓ Stage ⑨ seen — job complete. Appended to {LOG_FILE}")
                        break
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n  Stopped by user.")
                if text:
                    append_to_log(text)
                    print(f"  ↳ Last snapshot appended to {LOG_FILE}")
        else:
            # Auto-detect mode — first wait for a NEW job to appear, then watch it
            script_start = datetime.now(timezone.utc)
            bucket_url   = f"s3://{S3_BUCKET}/incoming/"
            print(f"  Live mode — waiting for a new job to appear in {bucket_url}")
            print(f"  (started at {_fmt_ts(script_start)} · polling every {args.interval}s · Ctrl-C to stop)")

            # Phase 1: wait for a brand-new upload
            wait_iter = 0
            text = ""
            try:
                while True:
                    wait_iter += 1
                    found = discover_job_id(since=script_start)
                    if found:
                        job_id, ts = found
                        _clear_screen()
                        print(f"  ✓ New job detected: {job_id}  (uploaded {_fmt_ts(ts)})")
                        print(f"  → switching to timeline view  (polling every {args.interval}s)\n")
                        break
                    elapsed = int((datetime.now(timezone.utc) - script_start).total_seconds())
                    print(f"\r  ⏳  no new job yet  ({elapsed}s elapsed) …", end="", flush=True)
                    time.sleep(args.interval)

                # Phase 2: live timeline for the discovered job
                iteration = 0
                while True:
                    iteration += 1
                    text, done = _run_once(job_id)
                    _clear_screen()
                    print(text)
                    print(f"\n  [live]  iteration #{iteration}  |  next refresh in {args.interval}s  |  Ctrl-C to stop")
                    if done:
                        append_to_log(text)
                        print(f"\n  ✓ Stage ⑨ seen — job complete. Appended to {LOG_FILE}")
                        break
                    time.sleep(args.interval)

            except KeyboardInterrupt:
                print("\n  Stopped by user.")
                if text:
                    append_to_log(text)
                    print(f"  ↳ Last snapshot appended to {LOG_FILE}")


if __name__ == "__main__":
    main()
