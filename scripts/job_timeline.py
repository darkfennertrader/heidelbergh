#!/usr/bin/env python3
"""
scripts/job_timeline.py — AppWay end-to-end job timeline

Assembles a human-readable, timestamped log of every stage a job passes
through — from DICOM received on HEYEX 2 all the way to the result stored
back in HEYEX — and appends it to  logs/workflow.logs.

Stages covered
──────────────
   [1]  DICOM received by AppWay Link               heyex2 AshvinsDistribution .zip
   [2]  DICOM uploaded to S3 incoming/              S3 object LastModified
   [3]  Job message enqueued on SQS appway-jobs     (derived: ~= stage [2])
   [4]  Input downloaded by backend                 /var/log/appway-worker.log
   [5]  Backend processes (YOLO + ePDF)             /var/log/appway-worker.log
   [6]  Result uploaded to S3 results/              S3 object LastModified
   [7]  Result message enqueued on appway-results   /var/log/appway-worker.log
   [8]  Result stored by AppWay Link                heyex2 AshvinsDistribution .dcm
   [9]  Result stored into HEYEX                    heyex2 UVOBackup AIResultBackup-<uuid> folder
   [X]  (if present) user-click failure             heyex2 MCAshvinsWorkstation log

Usage
─────
  # Auto-detect newest job and watch live (recommended after dragging DCM into HEYEX):
  python scripts/job_timeline.py --live

  # Auto-detect, one-shot (picks the newest existing job in S3 incoming/):
  python scripts/job_timeline.py

  # Explicit job id, one-shot:
  python scripts/job_timeline.py <job-id>

  # Explicit job id, live mode:
  python scripts/job_timeline.py <job-id> --live

  # UTC-only timestamps (default: CEST primary + UTC secondary):
  python scripts/job_timeline.py <job-id> --utc-only

  # Custom poll interval and idle timeout (--live only):
  python scripts/job_timeline.py --live --interval 10 --idle-timeout 120

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
AWS_REGION       = os.getenv("AWS_REGION", "eu-west-1")
S3_BUCKET        = os.getenv("S3_BUCKET",  "appway-bridge-prod")
HEYEX2_INSTANCE  = "i-02a7dd1797d85a099"
BACKEND_INSTANCE = "i-02a99abeba370f0a7"

CEST = timezone(timedelta(hours=2), "CEST")   # Europe/Berlin summer time (UTC+2)

LOGS_DIR = _PROJECT_ROOT / "logs"
LOG_FILE = LOGS_DIR / "workflow.logs"

SSM_TIMEOUT = 30   # seconds to wait for an SSM command to complete

SEP = "=" * 72   # plain ASCII separator (works in every font)

# ── timezone helpers ────────────────────────────────────────────────────────

def _to_cest(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CEST)


def _fmt_ts(dt: datetime, utc_only: bool = False) -> str:
    """
    default  → '2026-05-18 10:02:16 CEST  (08:02:16 UTC)'
    utc_only → '2026-05-18 08:02:16 UTC'
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc  = dt.astimezone(timezone.utc)
    if utc_only:
        return utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    cest = _to_cest(dt)
    return cest.strftime("%Y-%m-%d %H:%M:%S CEST") + "  (" + utc.strftime("%H:%M:%S UTC") + ")"


def _fmt_ts_short(dt: datetime) -> str:
    """Short CEST time used in streaming lines: '10:02:16 CEST'."""
    return _to_cest(dt).strftime("%H:%M:%S CEST")


def _fmt_delta(seconds: float) -> str:
    s = int(abs(seconds))
    h, rem = divmod(s, 3600)
    m, sc  = divmod(rem, 60)
    return f"+{h:02d}:{m:02d}:{sc:02d}"


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.2f} MB"


# ── log-file helpers ─────────────────────────────────────────────────────────

def _log_write(line: str) -> None:
    """Append a single line (+ newline) to logs/workflow.logs, creating it if needed."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ── AWS helpers ─────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3", region_name=AWS_REGION)


def _ssm():
    return boto3.client("ssm", region_name=AWS_REGION)


def discover_job_id(since: Optional[datetime] = None) -> Optional[tuple[str, datetime]]:
    """
    Find the most-recent job_id in S3 incoming/.

    If *since* is given (UTC-aware), only jobs uploaded **after** that moment
    are considered — lets --live wait for a brand-new job.

    Returns (job_id, last_modified_utc) or None.
    """
    objs = s3_list_prefix("incoming/")
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
    client = _s3()
    results = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"] == prefix:
                continue
            results.append({
                "key":           obj["Key"],
                "size":          obj["Size"],
                "last_modified": obj["LastModified"],
            })
    return results


def s3_head(key: str) -> Optional[dict]:
    try:
        resp = _s3().head_object(Bucket=S3_BUCKET, Key=key)
        return {"size": resp["ContentLength"], "last_modified": resp["LastModified"]}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


# ── SSM helper ───────────────────────────────────────────────────────────────

def ssm_run(instance_id: str, command: str, timeout: int = SSM_TIMEOUT) -> str:
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

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            inv    = client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            status = inv["Status"]
            if status in ("Success", "Failed", "Cancelled", "TimedOut"):
                return (inv.get("StandardOutputContent") or "").strip()
        except client.exceptions.InvocationDoesNotExist:
            continue
        except Exception as exc:
            return f"[SSM poll failed: {exc}]"
    return "[SSM timed out]"


# ── backend log grep ─────────────────────────────────────────────────────────

_WORKER_LOG    = Path("/var/log/appway-worker.log")
_WORKER_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+\[(?:INFO|WARNING|ERROR|DEBUG)\]\s+\S+:\s+(.*)"
)


def journal_grep(job_id: str) -> list[tuple[datetime, str]]:
    lines = _local_worker_log_grep(job_id)
    if lines is None:
        lines = _ssm_worker_log_grep(job_id)
    return lines


def _local_worker_log_grep(job_id: str) -> Optional[list[tuple[datetime, str]]]:
    if not _WORKER_LOG.exists():
        return None
    try:
        return _parse_worker_log_lines(_WORKER_LOG.read_text(errors="replace"), job_id)
    except Exception:
        return None


def _ssm_worker_log_grep(job_id: str) -> list[tuple[datetime, str]]:
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
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((dt, m.group(2)))
    out.sort(key=lambda x: x[0])
    return out


# ── heyex2 log grep ──────────────────────────────────────────────────────────

_HEYEX_LOG_DIR  = r"C:\HEYEX\logfiles"
_APPWAY_LOG_DIR = r"C:\HEYEX\AshvinsDistribution"
_UVOB_DIR       = r"C:\HEYEX\ImagwPool\UVOBackup"
_HEYEX_TS_RE    = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)")
# Folder name timestamp embedded in UVOBackup entries:
# e.g. E0ee1bc04-dd6f_Qf73528ea-2026.05.18-11.11.31.183-UVOJob-19-DeleteImage-Done
# e.g. K1b9b9cf6-ac92_Gcda68a17_2026.05.18-11.11.24.589-AIResultBackup-<uuid>
_FOLDER_TS_RE   = re.compile(r"(\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2})\.\d+-")


def _parse_heyex_ts(ts_str: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts_str.strip(), fmt).replace(tzinfo=CEST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _parse_folder_ts(ts_str: str) -> Optional[datetime]:
    """Parse YYYY.MM.DD-HH.MM.SS folder timestamp (CEST) → UTC-aware datetime."""
    try:
        return datetime.strptime(ts_str, "%Y.%m.%d-%H.%M.%S").replace(tzinfo=CEST).astimezone(timezone.utc)
    except ValueError:
        return None


def _heyex_grep(job_id: str, job_origin_ts: Optional[datetime] = None) -> dict:
    r"""
    Query heyex2 via SSM for stage markers.

    Stage sources (confirmed by SSM probes 2026-05-18):

      [1]  AshvinsDistribution\ -- most-recent .zip whose LastWriteTime is in
           window [job_origin_ts − 90 s, job_origin_ts + 60 s].
           AppWay Link creates the zip BEFORE pushing to S3/SQS, so it arrives
           ~30 s before the SQS message that defines job_origin_ts.

      [8]  AshvinsDistribution\ -- most-recent .dcm file (NOT the input zip)
           with LastWriteTime >= job_origin_ts and size < 100 KB.
           AppWay Link writes the AI result here as a small .dcm (no ".rtc."
           infix — confirmed on real traffic).

      [9]  ImagwPool\UVOBackup\ -- a "…-UVOJob-N-DeleteImage-Done" folder
           whose folder-name timestamp >= job_origin_ts.
           HEYEX creates an AIResultBackup-<uuid> folder when it *starts*
           importing, then creates UVOJob-N-DeleteImage-Done when it *finishes*.
           We fire [9] only on the Done folder.

      [X]  MCAshvinsWorkstation.verbose.log -- "couldn't open file" entries
           with ts >= job_origin_ts (user-click WebView2 failure).
    """
    found: dict = {}

    # Window for [1]: allow the .zip to arrive up to 90 s before the SQS
    # job_origin_ts (AppWay zips the DICOM first, then uploads to S3, then
    # enqueues to SQS — the zip can be 20-60 s earlier than the SQS event).
    zip_earliest = (job_origin_ts - timedelta(seconds=90)) if job_origin_ts else None

    # ── [1] and [8] from AshvinsDistribution ─────────────────────────────────
    # Fetch up to 30 most-recent entries so we don't miss anything.
    ps_appway = (
        r'$dir = "' + _APPWAY_LOG_DIR + r'"; '
        r'if (Test-Path $dir) { '
        r'  Get-ChildItem $dir -ErrorAction SilentlyContinue | '
        r'  Sort-Object LastWriteTime -Descending | Select-Object -First 30 | '
        r'  Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize '
        r'} else { "DIR_NOT_FOUND" }'
    )
    appway_out = ssm_run(HEYEX2_INSTANCE, ps_appway)

    for line in appway_out.splitlines():
        line = line.strip()
        if not line:
            continue
        ts_m = re.search(r'(\d+/\d+/\d{4}\s+\d+:\d+:\d+\s+[AP]M)', line)
        if not ts_m:
            continue
        try:
            ts = datetime.strptime(ts_m.group(1), "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=CEST).astimezone(timezone.utc)
        except ValueError:
            continue

        name_parts = line.split()
        filename   = name_parts[0] if name_parts else ""
        name_lower = filename.lower()

        # Try to extract the file size from Format-Table output
        # Format-Table -AutoSize renders: Name   Length   LastWriteTime
        # After splitting, Length is the second column when present
        size_val: Optional[int] = None
        for part in name_parts[1:]:
            if part.isdigit():
                size_val = int(part)
                break

        # [1]: .zip in the window [zip_earliest, job_origin_ts+60s]
        if name_lower.endswith(".zip") and "appway_rcvd" not in found:
            too_old = zip_earliest is not None and ts < zip_earliest
            too_new = job_origin_ts is not None and ts > (job_origin_ts + timedelta(seconds=60))
            if not too_old and not too_new:
                found["appway_rcvd"] = (ts, filename)   # stage [1]

        # [8]: .dcm (result) — must be AFTER job_origin_ts and small (<100 KB)
        if name_lower.endswith(".dcm") and "result_downloaded" not in found:
            if job_origin_ts is None or ts >= job_origin_ts:
                # Exclude large input DICOMs (>100 KB); result is typically ~280-650 B
                if size_val is None or size_val < 100_000:
                    found["result_downloaded"] = (ts, filename)   # stage [8]

    # ── [9] from UVOBackup AIResultBackup folders ────────────────────────────
    # Confirmed behaviour (2026-05-18 live testing):
    #   • HEYEX creates AIResultBackup-<uuid> when it STARTS importing the AI
    #     result; this is when the report becomes visible in the UI (~1 s after [8]).
    #   • UVOJob-N-DeleteImage-Done appears later (seconds → minutes) as a
    #     background cleanup step — NOT the right trigger for "report is ready".
    #
    # Parallel-job correctness:
    #   We floor the search at (this job's [8] timestamp − 5 s) so that two
    #   concurrent watchers never claim the same AIResultBackup folder.
    #   The "result_downloaded" key carries the [8] timestamp if already found.
    floor_ts = job_origin_ts  # default: no [8] yet, fall back to job_origin_ts
    if "result_downloaded" in found:
        rd_ts = found["result_downloaded"][0]
        floor_ts = rd_ts - timedelta(seconds=5)

    ps_uvo = (
        r'$dir = "' + _UVOB_DIR + r'"; '
        r'if (Test-Path $dir) { '
        r'  Get-ChildItem $dir -ErrorAction SilentlyContinue | '
        r'  Where-Object { $_.Name -like "*AIResultBackup*" } | '
        r'  Sort-Object CreationTime -Descending | Select-Object -First 20 | '
        r'  Select-Object Name, CreationTime | Format-Table -AutoSize -Wrap '
        r'} else { "DIR_NOT_FOUND" }'
    )
    uvo_out = ssm_run(HEYEX2_INSTANCE, ps_uvo)

    # Collect all AIResultBackup entries, sorted ascending
    _uvo_entries: list[tuple[datetime, str]] = []
    for line in uvo_out.splitlines():
        line = line.strip()
        if "AIResultBackup" not in line:
            continue
        # Prefer folder-name embedded timestamp (most accurate, CEST)
        fn_m = _FOLDER_TS_RE.search(line)
        ts   = _parse_folder_ts(fn_m.group(1)) if fn_m else None
        # Fall back to Format-Table CreationTime column
        if ts is None:
            ts_m = re.search(r'(\d+/\d+/\d{4}\s+\d+:\d+:\d+\s+[AP]M)', line)
            if ts_m:
                try:
                    ts = datetime.strptime(ts_m.group(1), "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=CEST).astimezone(timezone.utc)
                except ValueError:
                    pass
        if ts is None:
            continue
        name_parts = line.split()
        folder_name = name_parts[0] if name_parts else line[:120]
        _uvo_entries.append((ts, folder_name))

    _uvo_entries.sort(key=lambda x: x[0])

    for ts, folder_name in _uvo_entries:
        # Use floor_ts for tight pairing with [8] (parallel-job safety)
        if floor_ts is not None and ts < floor_ts:
            continue
        if "AIResultBackup" in folder_name and "result_stored" not in found:
            uuid_m = re.search(r"AIResultBackup-([0-9a-f-]{36})", folder_name)
            label  = f"UVOBackup/…AIResultBackup-{uuid_m.group(1)}" if uuid_m else folder_name[:80]
            found["result_stored"] = (ts, label, None)
            break

    # ── [X] click errors from MCAshvinsWorkstation.verbose.log ────────────────
    ps_heyex = (
        r'$log = "' + _HEYEX_LOG_DIR + r'\MCAshvinsWorkstation.verbose.log"; '
        r'if (Test-Path $log) { '
        r'  Get-Content $log | Select-String -Pattern "couldn''t open file" | Select-Object -Last 20 '
        r'} else { "LOG_NOT_FOUND" }'
    )
    heyex_out = ssm_run(HEYEX2_INSTANCE, ps_heyex)

    for line in heyex_out.splitlines():
        ts_m   = _HEYEX_TS_RE.search(line)
        ts     = _parse_heyex_ts(ts_m.group(1)) if ts_m else None
        path_m = re.search(r'(\\\\[^\s\t<>]+\.dcm|C:\\[^\s\t<>]+\.dcm)', line, re.IGNORECASE)
        if "couldn't open file" in line.lower():
            bad_path = path_m.group(1) if path_m else "(unknown path)"
            if "click_error" not in found and ts is not None:
                if job_origin_ts is None or ts >= job_origin_ts:
                    found["click_error"] = (ts, bad_path, line.strip()[:200])

    return found


# ── Stage dataclass ───────────────────────────────────────────────────────────

class Stage:
    # Maps internal number strings to display tags
    _TAG: dict[str, str] = {
        "1": "[1]", "2": "[2]", "3": "[3]", "4": "[4]", "5": "[5]",
        "6": "[6]", "7": "[7]", "8": "[8]", "9": "[9]", "X": "[X]",
    }

    def __init__(self, number: str, label: str, ts: Optional[datetime] = None, detail: str = ""):
        self.number = number   # "1".."9" or "X"
        self.label  = label
        self.ts     = ts       # UTC-aware or None
        self.detail = detail

    @property
    def tag(self) -> str:
        return self._TAG.get(self.number, f"[{self.number}]")


# ── timeline builder ─────────────────────────────────────────────────────────

def build_timeline(job_id: str) -> list[Stage]:
    """Query all sources; return Stage list sorted by timestamp (Nones last)."""
    stages: list[Stage] = []

    incoming_prefix = f"incoming/{job_id}/"
    result_key      = f"results/{job_id}/result.dcm"

    # [2] S3 incoming
    s3_incoming = s3_list_prefix(incoming_prefix)
    if s3_incoming:
        first = min(s3_incoming, key=lambda x: x["last_modified"])
        detail = f"s3://{S3_BUCKET}/{first['key']}  ({_fmt_size(first['size'])})"
        if len(s3_incoming) > 1:
            total = sum(o["size"] for o in s3_incoming)
            detail += f"\n  ... {len(s3_incoming)} file(s) total, {_fmt_size(total)}"
        stages.append(Stage("2", "DICOM uploaded to S3", first["last_modified"], detail))
    else:
        stages.append(Stage("2", "DICOM uploaded to S3", None, f"s3://{S3_BUCKET}/{incoming_prefix}  (not found)"))

    # [6] S3 result
    s3_result = s3_head(result_key)
    if s3_result:
        stages.append(Stage("6", "ePDF result uploaded to S3", s3_result["last_modified"],
                            f"s3://{S3_BUCKET}/{result_key}  ({_fmt_size(s3_result['size'])})"))
    else:
        stages.append(Stage("6", "ePDF result uploaded to S3", None,
                            f"s3://{S3_BUCKET}/{result_key}  (not yet present)"))

    # [4] [5] [7] from backend worker log
    journal = journal_grep(job_id)

    def _find(pattern: str) -> Optional[datetime]:
        for dt, msg in journal:
            if pattern in msg:
                return dt
        return None

    # [4] download
    dl_ts     = _find("Downloaded") or _find("Downloading input")
    dl_detail = ""
    for _, msg in journal:
        if "Downloaded" in msg and "incoming/" in msg:
            m = re.search(r"Downloaded (\d+) file\(s\) from (s3://\S+)", msg)
            if m:
                dl_detail = f"{m.group(2)}  ({m.group(1)} file(s))"
            break
    stages.append(Stage("4", "Input downloaded by backend", dl_ts, dl_detail))

    # [5] processing
    proc_start  = _find("Processing\u2026") or _find("Processing DICOM")
    proc_done   = _find("Processor complete") or _find("ePDF DICOM saved")
    proc_detail = ""
    if proc_start and proc_done:
        proc_detail = f"duration {(proc_done - proc_start).total_seconds():.1f}s"
    for _, msg in journal:
        if "Inference result:" in msg:
            proc_detail = msg.split("] ", 1)[-1] if "] " in msg else msg
            break
    stages.append(Stage("5", "Backend processes (YOLO + ePDF)", proc_done or proc_start, proc_detail))

    # [6] upload detail merge  + [7] enqueue
    up_ts     = _find("Uploaded 1 file") or _find("Uploading output")
    up_detail = ""
    for _, msg in journal:
        if "Uploaded" in msg and "results/" in msg:
            m = re.search(r"Uploaded \d+ file\(s\) to (s3://\S+)", msg)
            if m:
                up_detail = m.group(1)
            break
    if not up_ts and s3_result:
        up_ts = s3_result["last_modified"]
    if s3_result and not up_detail:
        up_detail = f"s3://{S3_BUCKET}/{result_key}  ({_fmt_size(s3_result['size'])})"
    for st in stages:
        if st.number == "6" and not st.detail.startswith("(not"):
            if up_detail:
                st.detail = up_detail
            if up_ts and not st.ts:
                st.ts = up_ts

    enq_ts     = _find("Sent result message")
    enq_detail = ""
    for _, msg in journal:
        if "Sent result message" in msg:
            enq_detail = msg.split("] ", 1)[-1] if "] " in msg else msg
            break
    stages.append(Stage("7", "Result enqueued on SQS appway-results", enq_ts, enq_detail))

    # [3] Job enqueued on SQS appway-jobs
    # This is a synthetic stage: the SQS enqueue happens at approximately the same
    # time as the S3 upload ([2]).  We emit it at the [2] timestamp (or the first
    # worker log timestamp if available) so it appears in the timeline and summary
    # rather than always being listed as "missed".
    s3_job_ts = s3_incoming[0]["last_modified"] if s3_incoming else None
    job_enq_ts = s3_job_ts  # best-available proxy; usually within 1-2 s of actual enqueue
    stages.append(Stage("3", "Job enqueued on SQS appway-jobs",
                        job_enq_ts, "(derived ≈ S3 upload time)"))

    # [1] [8] [9] [X] from heyex2 via SSM
    job_origin_ts = s3_incoming[0]["last_modified"] if s3_incoming else None
    heyex_data    = _heyex_grep(job_id, job_origin_ts=job_origin_ts)

    # [1] AppWay received DICOM (.zip in AshvinsDistribution)
    if "appway_rcvd" in heyex_data:
        ts, filename = heyex_data["appway_rcvd"]
        stages.append(Stage("1", "DICOM received by AppWay Link",  ts,
                            f"AshvinsDistribution/{filename}"))
    else:
        stages.append(Stage("1", "DICOM received by AppWay Link", None,
                            "(no .zip found in AshvinsDistribution)"))

    # [8] AppWay stored result (.dcm in AshvinsDistribution, size < 100 KB)
    if "result_downloaded" in heyex_data:
        ts, filename = heyex_data["result_downloaded"]
        stages.append(Stage("8", "Result stored by AppWay Link", ts,
                            f"AshvinsDistribution/{filename}"))
    else:
        stages.append(Stage("8", "Result stored by AppWay Link", None,
                            "(no result .dcm found in AshvinsDistribution yet)"))

    # [9] HEYEX started importing AI result (AIResultBackup-<uuid> in UVOBackup)
    # This is when the report becomes visible in the HEYEX UI.
    if "result_stored" in heyex_data:
        ts, label, _ = heyex_data["result_stored"]
        stages.append(Stage("9", "Result stored in HEYEX", ts, label))
    else:
        stages.append(Stage("9", "Result stored in HEYEX", None,
                            "(no AIResultBackup-* folder found in UVOBackup yet)"))

    if "click_error" in heyex_data:
        ts, bad_path, msg = heyex_data["click_error"]
        stages.append(Stage("X", "User-click failure (can't reach this page)", ts,
                            f"Tried to open: {bad_path}\n  {msg[:200]}"))

    # sort: timestamped stages first (chronological), None-ts last
    stages.sort(key=lambda s: s.ts if s.ts is not None else datetime.max.replace(tzinfo=timezone.utc))
    return stages


# ── one-shot renderer ─────────────────────────────────────────────────────────

def render_timeline(job_id: str, stages: list[Stage], utc_only: bool = False) -> str:
    lines: list[str] = []
    lines.append(SEP)
    lines.append(f"  AppWay job timeline:  {job_id}")
    lines.append(f"  Generated at:         {_fmt_ts(datetime.now(timezone.utc), utc_only)}")
    lines.append(SEP)

    origin: Optional[datetime] = next(
        (s.ts for s in stages if s.ts is not None and s.number != "X"), None
    )

    for st in stages:
        if st.ts is not None:
            ts_str  = _fmt_ts(st.ts, utc_only)
            delta   = _fmt_delta((st.ts - origin).total_seconds()) if origin else "+00:00:00"
        else:
            ts_str  = "(not yet seen)"
            delta   = "+??:??:??"
        lines.append(f"  [{delta}]  {ts_str}   {st.tag} {st.label}")
        for dl in st.detail.split("\n"):
            if dl.strip():
                lines.append(f"               {dl}")
        lines.append("")

    seen = [s for s in stages if s.ts is not None and s.number != "X"]
    if len(seen) >= 2:
        total_s = (seen[-1].ts - seen[0].ts).total_seconds()
        m, s = divmod(int(total_s), 60)
        lines.append(f"  Total elapsed: {m}m {s:02d}s   ({seen[0].tag} -> {seen[-1].tag})")

    lines.append(SEP)
    return "\n".join(lines)


# ── streaming live helpers ────────────────────────────────────────────────────
#
# Column layout (fixed widths, plain ASCII):
#
#   TIME          ELAPSED    STAGE  LABEL
#   ────────────  ─────────  ─────  ──────────────────────────────────────────
#   10:02:16 CEST  +00:00:00   [4]  Input downloaded by backend
#                                   s3://…/incoming/…/  (1 file)
#
# TIME     = 13 chars ("HH:MM:SS CEST")
# ELAPSED  =  9 chars ("+HH:MM:SS")
# STAGE    =  3 chars ("[N]")
# LABEL/DETAIL = rest of line
#
# Header is emitted once; detail lines are indented to DETAIL_COL.

_COL_TIME    = 13   # "HH:MM:SS CEST"
_COL_ELAPSED =  9   # "+HH:MM:SS"
_COL_STAGE   =  3   # "[N]"
_COL_GAP     =  2   # spaces between columns
_DETAIL_COL  = _COL_TIME + _COL_GAP + _COL_ELAPSED + _COL_GAP + _COL_STAGE + _COL_GAP  # indent for detail lines = 31


def _stream_header(utc_only: bool) -> str:
    tz_label = "UTC" if utc_only else "CEST"
    time_hdr  = f"TIME ({tz_label})".ljust(_COL_TIME)
    elap_hdr  = "ELAPSED".ljust(_COL_ELAPSED)
    stage_hdr = "ST"
    return (
        f"\n  {time_hdr}  {elap_hdr}  {stage_hdr}  STAGE\n"
        f"  {'─' * _COL_TIME}  {'─' * _COL_ELAPSED}  {'─' * _COL_STAGE}  {'─' * 42}"
    )


def _stream_stage_line(st: Stage, origin: datetime, utc_only: bool) -> str:
    """Return one or two terminal lines for one newly-seen stage (fixed columns)."""
    if utc_only:
        ts_short = st.ts.astimezone(timezone.utc).strftime("%H:%M:%S UTC")
    else:
        ts_short = _fmt_ts_short(st.ts)           # "HH:MM:SS CEST"
    delta = _fmt_delta((st.ts - origin).total_seconds())

    # fixed-width columns
    tc = ts_short.ljust(_COL_TIME)   # 13
    ec = delta.ljust(_COL_ELAPSED)   # 9
    sc = st.tag.ljust(_COL_STAGE)    # 3

    line = f"  {tc}  {ec}  {sc}  {st.label}"

    # detail line(s) indented to _DETAIL_COL
    indent = " " * (2 + _DETAIL_COL)    # 2 for "  " prefix + column offset
    for dl in st.detail.split("\n"):
        dl = dl.strip()
        if dl:
            line += f"\n{indent}{dl}"
    return line


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
                            help="Stream new events as they appear; exit on stage [9] or idle timeout")
    parser.add_argument("--interval", type=int, default=5, metavar="SEC",
                        help="Poll interval for --live mode (default: 5 s)")
    parser.add_argument("--idle-timeout", type=int, default=900, metavar="SEC",
                        help="Seconds of no new events after stage [6] before auto-exit (default: 900 = 15 min)")
    parser.add_argument("--quick", action="store_true",
                        help="Shortcut for --idle-timeout 300 (5 min); useful for dev/CI runs")
    parser.add_argument("--utc-only", action="store_true",
                        help="Show UTC timestamps only (default: CEST primary + UTC secondary)")
    parser.add_argument("--no-heyex", action="store_true",
                        help="Skip SSM queries to heyex2 (faster, backend-only view)")
    args = parser.parse_args()

    if args.quick:
        args.idle_timeout = 300

    if args.live:
        args.one_shot = False

    # ── resolve job_id ────────────────────────────────────────────────────────
    job_id = args.job_id.strip() if args.job_id else None

    if job_id is None and not args.live:
        result = discover_job_id()
        if result is None:
            print(f"  [X] No jobs found in s3://{S3_BUCKET}/incoming/ — nothing to show.", file=sys.stderr)
            sys.exit(1)
        job_id, ts = result
        print(f"  Auto-detected job: {job_id}  (uploaded {_fmt_ts(ts)})")

    # ── one-shot ──────────────────────────────────────────────────────────────
    if not args.live:
        stages = build_timeline(job_id)
        text   = render_timeline(job_id, stages, utc_only=args.utc_only)
        print(text)
        _log_write(text)
        print(f"\n  -> Appended to {LOG_FILE}")
        return

    # ── live mode ─────────────────────────────────────────────────────────────
    #
    # Phase 1 (auto-detect only): wait for a brand-new job in S3.
    # Phase 2: stream new events as single lines; never re-render past output.
    #          Write each new line to logs/workflow.logs immediately.
    #          Exit when stage [9] is seen, or after idle_timeout seconds with
    #          no new events following stage [7].

    if job_id is None:
        # ── phase 1: wait for new job ────────────────────────────────────────
        script_start = datetime.now(timezone.utc)
        print(f"  Live mode — waiting for a new job in s3://{S3_BUCKET}/incoming/")
        print(f"  (started at {_fmt_ts(script_start)} · poll every {args.interval}s · Ctrl-C to stop)")
        try:
            while True:
                found = discover_job_id(since=script_start)
                if found:
                    job_id, ts = found
                    # clear the waiting line
                    print(f"\r  New job: {job_id}  (uploaded {_fmt_ts(ts)})" + " " * 10)
                    break
                elapsed = int((datetime.now(timezone.utc) - script_start).total_seconds())
                print(f"\r  Waiting...  {elapsed}s elapsed", end="", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  Stopped by user (no job seen).")
            return

    # ── phase 2: streaming timeline ──────────────────────────────────────────
    header = (
        f"\n{SEP}\n"
        f"  AppWay job:  {job_id}\n"
        f"  Watching at: {_fmt_ts(datetime.now(timezone.utc))}\n"
        f"{SEP}"
    )
    print(header)
    _log_write(header)

    seen_numbers: set[str] = set()   # stage numbers already printed
    origin: Optional[datetime] = None
    last_new_event_time = time.monotonic()
    stage6_seen = False   # idle clock starts after stage [6] (result on S3)

    try:
        while True:
            stages = build_timeline(job_id)

            # collect newly-resolved stages
            new_stages = [
                s for s in stages
                if s.ts is not None and s.number not in seen_numbers
            ]
            # sort new stages by their actual timestamp
            new_stages.sort(key=lambda s: s.ts)

            for st in new_stages:
                if origin is None:
                    origin = st.ts
                    # print header row once, before the first data row
                    # If the countdown line is active, clear it first
                    if stage6_seen:
                        print("\r\033[K", end="", flush=True)
                    hdr = _stream_header(args.utc_only)
                    print(hdr)
                    _log_write(hdr)
                # Clear any active \r countdown line before printing a new event
                if stage6_seen:
                    print("\r\033[K", end="", flush=True)
                line = _stream_stage_line(st, origin, args.utc_only)
                print(line)
                _log_write(line)
                seen_numbers.add(st.number)
                last_new_event_time = time.monotonic()
                if st.number == "6":
                    stage6_seen = True

            # exit conditions
            if "9" in seen_numbers:
                summary = _build_summary(stages, seen_numbers)
                print(summary)
                _log_write(summary)
                footer = f"  -> Appended to {LOG_FILE}"
                print(footer)
                _log_write(SEP)
                break

            if stage6_seen:
                idle = time.monotonic() - last_new_event_time
                remaining = int(args.idle_timeout - idle)
                if idle >= args.idle_timeout:
                    msg = f"\n  (!) Idle timeout reached — stages [8]/[9] not detected; exiting."
                    print(msg)
                    _log_write(msg)
                    summary = _build_summary(stages, seen_numbers)
                    print(summary)
                    _log_write(summary)
                    _log_write(SEP)
                    break
                # show countdown on a single overwriting line; indicate which stage we're waiting for
                mins, secs = divmod(remaining, 60)
                countdown = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                if "8" not in seen_numbers:
                    waiting_for = "[8] (AppWay Link → drop folder)"
                else:
                    waiting_for = "[9] (HEYEX importing AI result)"
                print(f"\r  Waiting for {waiting_for}...  idle timeout in {countdown}   ", end="", flush=True)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n  Stopped by user.")
        summary = _build_summary(build_timeline(job_id), seen_numbers)
        print(summary)
        _log_write(summary)
        _log_write(SEP)


def _build_summary(stages: list[Stage], seen_numbers: set[str]) -> str:
    all_tags  = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    seen_tags = [Stage._TAG.get(n, f"[{n}]") for n in all_tags if n in seen_numbers]
    miss_tags = [Stage._TAG.get(n, f"[{n}]") for n in all_tags if n not in seen_numbers]
    seen_st   = [s for s in stages if s.ts is not None and s.number not in ("X",)]
    elapsed   = ""
    if len(seen_st) >= 2:
        total_s = (seen_st[-1].ts - seen_st[0].ts).total_seconds()
        m, s = divmod(int(total_s), 60)
        elapsed = f"  Total elapsed:  {m}m {s:02d}s   ({seen_st[0].tag} -> {seen_st[-1].tag})\n"
    seen_str = " ".join(seen_tags) if seen_tags else "(none)"
    miss_str = " ".join(miss_tags) if miss_tags else "(none)"
    return (
        f"\n  Summary\n"
        f"  -------\n"
        f"  Stages seen   : {seen_str}\n"
        f"  Stages missed : {miss_str}\n"
        f"{elapsed}"
    )


if __name__ == "__main__":
    main()
