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
  # One-shot (single query, print & append to logs/workflow.logs):
  python scripts/job_timeline.py <job-id>

  # Live mode (re-query every 5 s, redraw until stage ⑨ seen or Ctrl-C):
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


# ── backend journal grep ─────────────────────────────────────────────────────

def journal_grep(job_id: str) -> list[tuple[datetime, str]]:
    """
    Pull appway-worker journal lines that contain job_id.
    Returns list of (utc_datetime, rest_of_line) sorted by time.

    Uses SSH / local journalctl.  If running ON the backend instance itself,
    just calls journalctl directly.  If running on the dev laptop, uses SSM.
    """
    # Try local journalctl first (works when we ARE the backend EC2)
    lines = _local_journal_grep(job_id)
    if lines is None:
        # Fall back to SSM
        lines = _ssm_journal_grep(job_id)
    return lines


def _local_journal_grep(job_id: str) -> Optional[list[tuple[datetime, str]]]:
    """
    Try `journalctl -u appway-worker --no-pager -o short-iso` locally.
    Returns None if the unit doesn't exist here (i.e. we're not the backend).
    """
    try:
        result = subprocess.run(
            [
                "journalctl", "-u", "appway-worker",
                "--no-pager", "-o", "short-iso",
                "--grep", re.escape(job_id),
            ],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 and "No entries" not in result.stderr:
            # Unit not found / journalctl not available → not the backend host
            if "not found" in result.stderr.lower() or "not exist" in result.stderr.lower():
                return None
        return _parse_journal_lines(result.stdout, job_id)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _ssm_journal_grep(job_id: str) -> list[tuple[datetime, str]]:
    """Grep the backend journal via SSM (Linux)."""
    cmd = (
        f"journalctl -u appway-worker --no-pager -o short-iso 2>/dev/null"
        f" | grep -F '{job_id}' | tail -200"
    )
    raw = ssm_run(BACKEND_INSTANCE, cmd)
    if raw.startswith("[SSM"):
        return []
    return _parse_journal_lines(raw, job_id)


# journal line format: "2026-05-17T22:50:09+0000 hostname appway-worker[pid]: MSG"
_JOURNAL_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\s+\S+\s+\S+:\s+(.*)"
)

def _parse_journal_lines(raw: str, job_id: str) -> list[tuple[datetime, str]]:
    out = []
    for line in raw.splitlines():
        if job_id not in line:
            continue
        m = _JOURNAL_TS_RE.match(line)
        if not m:
            continue
        ts_str, msg = m.group(1), m.group(2)
        try:
            # Python 3.7+ strptime with %z parses "+0000" / "+0200"
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        out.append((dt.astimezone(timezone.utc), msg))
    out.sort(key=lambda x: x[0])
    return out


# ── heyex2 log grep ──────────────────────────────────────────────────────────

_HEYEX_LOG_DIR = r"C:\HEYEX\LogFiles"
_APPWAY_LOG_DIR = r"C:\ProgramData\Heidelberg Engineering\AppWay Link\Logs"

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


def _heyex_grep(job_id: str, result_filename: str) -> dict:
    """
    Grep heyex2 Windows logs via SSM PowerShell.

    Returns a dict with optional keys:
      'dicom_received'  : (utc_dt, filepath, size_bytes)
      'result_stored'   : (utc_dt, filepath, size_bytes)
      'click_error'     : (utc_dt, bad_path, error_msg)
    """
    found: dict = {}

    # ── MCAshvinsWorkstation.verbose.log — result stored + click error ──────
    ps_heyex = (
        r'$log = Get-ChildItem "' + _HEYEX_LOG_DIR + r'" -Filter "MCAshvinsWorkstation.verbose.log" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1; '
        r'if ($log) { Get-Content $log.FullName | Select-String -Pattern "' + re.escape(result_filename) + r'|ThreadLoadDICOMReport|DeleteImage-Done" } else { "LOG_NOT_FOUND" }'
    )
    heyex_out = ssm_run(HEYEX2_INSTANCE, ps_heyex)

    for line in heyex_out.splitlines():
        ts_m = _HEYEX_TS_RE.search(line)
        ts = _parse_heyex_ts(ts_m.group(1)) if ts_m else None

        if result_filename and result_filename in line and "result_stored" not in found:
            # Line mentions our result DCM → it was stored
            path_m = re.search(r'(C:\\[^\s"]+\.dcm)', line, re.IGNORECASE)
            path = path_m.group(1) if path_m else result_filename
            # Try to get file size via SSM
            size = _ssm_file_size(path)
            found["result_stored"] = (ts, path, size)

        if "ThreadLoadDICOMReport" in line and "click_error" not in found:
            # Could not open file
            path_m = re.search(r'(\\\\[^\s"]+\.dcm|C:\\[^\s"]+\.dcm)', line, re.IGNORECASE)
            bad_path = path_m.group(1) if path_m else "(unknown path)"
            found["click_error"] = (ts, bad_path, line.strip())

    # ── AppWay Link log — DICOM received + result downloaded ────────────────
    ps_appway = (
        r'$log = Get-ChildItem "' + _APPWAY_LOG_DIR + r'" -Filter "*.log" -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 3; '
        r'if ($log) { $log | Get-Content | Select-String -Pattern "' + re.escape(job_id) + r'" | Select-Object -Last 50 } else { "LOG_NOT_FOUND" }'
    )
    appway_out = ssm_run(HEYEX2_INSTANCE, ps_appway)

    for line in appway_out.splitlines():
        ts_m = _HEYEX_TS_RE.search(line)
        ts = _parse_heyex_ts(ts_m.group(1)) if ts_m else None

        low = line.lower()
        # AppWay Link uploads: "upload", "sending", "put"
        if any(kw in low for kw in ("upload", "put object", "sending")) and "dicom_received" not in found:
            path_m = re.search(r'([A-Z]:\\[^\s"]+\.dcm)', line, re.IGNORECASE)
            path = path_m.group(1) if path_m else "?"
            found["dicom_received"] = (ts, path, None)

        # AppWay Link downloads result: "download", "get object", "result"
        if any(kw in low for kw in ("download", "get object", "result")) and "result_downloaded" not in found:
            if "result" in low:
                found["result_downloaded"] = (ts, line.strip())

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

    dl_ts = _find_journal("STAGE 4/9 download_done")
    dl_files = _find_journal_kv("STAGE 4/9 download_done", "files")
    dl_bytes = _find_journal_kv("STAGE 4/9 download_done", "size_bytes")
    dl_prefix = _find_journal_kv("STAGE 4/9 download_done", "s3_prefix")
    dl_local  = _find_journal_kv("STAGE 4/9 download_done", "local_dir")
    dl_detail = ""
    if dl_prefix:
        n = dl_files or "?"
        sz = _fmt_size(int(dl_bytes)) if dl_bytes else "?"
        dl_detail = f"s3://{S3_BUCKET}/{dl_prefix}  →  {dl_local}  ({n} file(s), {sz})"
    stages.append(Stage("④", "Input downloaded by backend", dl_ts, dl_detail))

    proc_start = _find_journal("STAGE 5/9 processing_start")
    proc_done  = _find_journal("STAGE 5/9 processing_done")
    proc_detail = ""
    if proc_start and proc_done:
        elapsed = (proc_done - proc_start).total_seconds()
        proc_detail = f"duration {elapsed:.1f}s"
    stages.append(Stage("⑤", "Backend processes (YOLO + ePDF)", proc_done or proc_start, proc_detail))

    up_ts    = _find_journal("STAGE 6/9 upload_done")
    up_key   = _find_journal_kv("STAGE 6/9 upload_done", "s3_key")
    up_bytes = _find_journal_kv("STAGE 6/9 upload_done", "size_bytes")
    up_detail = ""
    if up_key:
        sz = _fmt_size(int(up_bytes)) if up_bytes else "?"
        up_detail = f"s3://{S3_BUCKET}/{up_key}  ({sz})"
    # Merge with S3 LastModified (S3 is authoritative for timing)
    if not up_ts and s3_result:
        up_ts = s3_result["last_modified"]
    if up_ts and s3_result and not up_detail:
        up_detail = f"s3://{S3_BUCKET}/{result_key}  ({_fmt_size(s3_result['size'])})"
    # Update stage ⑥ in place with journal detail if we got it
    for st in stages:
        if st.number == "⑥" and not st.detail.startswith("(not"):
            if up_detail:
                st.detail = up_detail
            if up_ts and not st.ts:
                st.ts = up_ts

    enq_ts = _find_journal("STAGE 7/9 result_enqueued")
    stages.append(Stage("⑦", "Result enqueued on SQS appway-results", enq_ts))

    # ── SSM: heyex2 stages ①, ⑧, ⑨ ─────────────────────────────────────────
    # Derive result filename from S3 key / journal
    result_fname = ""
    if up_key:
        result_fname = up_key.split("/")[-1].replace(".dcm", "")  # e.g. "result"
    # The actual filename on Windows is set by AppWay Link, not us.
    # We'll use the job_id to grep for the record; and the result path
    # from the verbose.log if found.

    heyex_data = _heyex_grep(job_id, result_fname)

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
    parser.add_argument("job_id", help="Job ID, e.g. final-5f1e35fa-3397-4604-b5c1-a7785919ea13")
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

    job_id = args.job_id.strip()

    def _run_once() -> tuple[str, bool]:
        """Query, render, return (rendered_text, stage9_seen)."""
        stages = build_timeline(job_id)
        text = render_timeline(job_id, stages, utc_only=args.utc_only)
        stage9_seen = any(
            st.number == "⑨" and st.ts is not None for st in stages
        )
        return text, stage9_seen

    if not args.live:
        # ── one-shot ─────────────────────────────────────────────────────────
        text, _ = _run_once()
        print(text)
        append_to_log(text)
        print(f"\n  ↳ Appended to {LOG_FILE}")
    else:
        # ── live mode ────────────────────────────────────────────────────────
        print(f"  Live mode — polling every {args.interval}s  (Ctrl-C to stop)")
        iteration = 0
        try:
            while True:
                iteration += 1
                text, done = _run_once()
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
            # Append the last snapshot
            append_to_log(text)
            print(f"  ↳ Last snapshot appended to {LOG_FILE}")


if __name__ == "__main__":
    main()
