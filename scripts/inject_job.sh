#!/usr/bin/env bash
#
# inject_job.sh — manually kick off the MyopicCNV+ backend pipeline with one
# or more local DICOM files, skipping the Windows AppWay Link side.
#
# What it does:
#   1. Uploads each --files entry to s3://appway-bridge-prod/incoming/<job-id>/
#   2. Sends a job message on SQS appway-jobs (same format the publisher relay
#      on the Windows EC2 would have sent)
#   3. Streams the appway-worker.log until the job completes or fails (or 5 min
#      timeout)
#   4. Prints the per-job outputs directory (metadata.json / image*.png /
#      result.pdf) on the backend EC2 for operator review
#
# Usage:
#   scripts/inject_job.sh --files /path/to/a.dcm,/path/to/b.dcm
#
# Optional flags:
#   --job-id <id>     override the auto-generated job id
#                     (default: test-<YYYYMMDD_HHMMSS>  — same timestamp style
#                      as AppWay's production final-<…> ids, with test- prefix)
#   --timeout <sec>   how long to watch the log (default: 300)
#   --no-watch        just enqueue, don't tail the log
#   -h | --help       show this message
#
# Requirements (already present on the backend EC2):
#   - boto3 in the project venv at /home/ubuntu/appway-backend/.venv
#     (IAM role on the EC2 grants s3:PutObject + sqs:SendMessage)
#   - sudo      — only required if watching /var/log/appway-worker.log
#
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Defaults (match docs/backend.md → Configuration)
# ─────────────────────────────────────────────────────────────────────────────
AWS_REGION="${AWS_REGION:-eu-west-1}"
S3_BUCKET="${S3_BUCKET:-appway-bridge-prod}"
JOBS_QUEUE_URL="${JOBS_QUEUE_URL:-https://sqs.eu-west-1.amazonaws.com/911167932273/appway-jobs}"
WORKER_LOG="${WORKER_LOG:-/var/log/appway-worker.log}"
OUTPUTS_ROOT="${OUTPUTS_ROOT:-/home/ubuntu/appway-backend/outputs}"
VENV_PY="${VENV_PY:-/home/ubuntu/appway-backend/.venv/bin/python3}"

TIMEOUT=300
WATCH=1
JOB_ID=""
FILES_CSV=""

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
die() { echo "error: $*" >&2; exit 1; }

usage() {
    sed -n '2,29p' "$0" | sed 's/^#\s\{0,1\}//'
    exit 0
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse args
# ─────────────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --files)       FILES_CSV="$2"; shift 2 ;;
        --files=*)     FILES_CSV="${1#*=}"; shift ;;
        --job-id)      JOB_ID="$2"; shift 2 ;;
        --job-id=*)    JOB_ID="${1#*=}"; shift ;;
        --timeout)     TIMEOUT="$2"; shift 2 ;;
        --timeout=*)   TIMEOUT="${1#*=}"; shift ;;
        --no-watch)    WATCH=0; shift ;;
        -h|--help)     usage ;;
        *)             die "unknown argument: $1 (use --help)" ;;
    esac
done

[ -n "$FILES_CSV" ] || die "missing required --files <csv-of-dcm-paths>"

[ -x "$VENV_PY" ] || die "project venv python not found at $VENV_PY (run 'uv sync' first)"

# Split comma-separated list into an array, validate each path
IFS=',' read -r -a FILES <<< "$FILES_CSV"
[ "${#FILES[@]}" -gt 0 ] || die "--files is empty"

for f in "${FILES[@]}"; do
    [ -n "$f" ]        || die "empty filename in --files"
    [ -f "$f" ]        || die "not a file: $f"
    case "$f" in
        *.dcm|*.DCM)   ;;
        *)             die "expected a .dcm file: $f" ;;
    esac
done

# Job id (default matches AppWay's `final-<timestamp>` pattern but uses
# `test-` so manually injected jobs are immediately recognisable in S3 / SQS /
# outputs/ listings alongside real production jobs).
if [ -z "$JOB_ID" ]; then
    JOB_ID="test-$(date +%Y%m%d_%H%M%S)"
fi
INPUT_PREFIX="incoming/${JOB_ID}/"
RESULT_PREFIX="results/${JOB_ID}/"

echo "════════════════════════════════════════════════════════════════════════"
echo "  AppWay backend — manual job injection"
echo "  Job ID      : ${JOB_ID}"
echo "  Files (${#FILES[@]}) : ${FILES[*]}"
echo "  S3 input    : s3://${S3_BUCKET}/${INPUT_PREFIX}"
echo "  S3 output   : s3://${S3_BUCKET}/${RESULT_PREFIX}"
echo "  Outputs dir : ${OUTPUTS_ROOT}/${JOB_ID}/"
echo "════════════════════════════════════════════════════════════════════════"

# ─────────────────────────────────────────────────────────────────────────────
# 1+2. Upload DICOMs to S3 and send SQS job message (via boto3 in project venv)
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "[1/3] Uploading ${#FILES[@]} DICOM(s) to S3 and [2/3] sending SQS job message…"
AWS_REGION="$AWS_REGION" \
S3_BUCKET="$S3_BUCKET" \
JOBS_QUEUE_URL="$JOBS_QUEUE_URL" \
JOB_ID="$JOB_ID" \
INPUT_PREFIX="$INPUT_PREFIX" \
RESULT_PREFIX="$RESULT_PREFIX" \
FILES_CSV="${FILES[*]}" \
"$VENV_PY" - <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
import boto3

region        = os.environ["AWS_REGION"]
bucket        = os.environ["S3_BUCKET"]
queue_url     = os.environ["JOBS_QUEUE_URL"]
job_id        = os.environ["JOB_ID"]
input_prefix  = os.environ["INPUT_PREFIX"]
result_prefix = os.environ["RESULT_PREFIX"]
files         = os.environ["FILES_CSV"].split()

s3  = boto3.client("s3",  region_name=region)
sqs = boto3.client("sqs", region_name=region)

# Upload each DICOM
for f in files:
    bn = Path(f).name
    key = f"{input_prefix}{bn}"
    s3.upload_file(f, bucket, key)
    print(f"  ✓ s3://{bucket}/{key}")

# Send job message
body = {
    "job_id":        job_id,
    "bucket":        bucket,
    "input_prefix":  input_prefix,
    "result_prefix": result_prefix,
    "source_folder": "inject_job.sh (manual)",
    "published_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
}
resp = sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))
print(f"  ✓ MessageId={resp['MessageId']}")
PYEOF

# ─────────────────────────────────────────────────────────────────────────────
# 3. Watch the worker log
# ─────────────────────────────────────────────────────────────────────────────
if [ "$WATCH" -eq 0 ]; then
    echo
    echo "Job enqueued. Skipping log watch (--no-watch)."
    echo "Follow progress with:  sudo grep ${JOB_ID} ${WORKER_LOG}"
    exit 0
fi

echo
echo "[3/3] Watching ${WORKER_LOG} for job ${JOB_ID} (timeout: ${TIMEOUT}s)…"
echo "---------------------------------------------------------------------"

# Stream the log filtered by the job id. We terminate the tail when we see the
# terminal line, or when the timeout fires — whichever happens first.
set +e
# shellcheck disable=SC2016
sudo timeout "$TIMEOUT" bash -c '
    tail -n0 -F "$1" 2>/dev/null | awk -v jid="$2" -v start="$(date +%s)" -v tout="$3" "
        /\\[/ && index(\$0, jid) { print; fflush(); if (\$0 ~ /Job complete ✓|Job failed at application level|Job failed/) exit 0 }
    "
' _ "$WORKER_LOG" "$JOB_ID" "$TIMEOUT"
STATUS=$?
set -e

echo "---------------------------------------------------------------------"

# ─────────────────────────────────────────────────────────────────────────────
# Final summary
# ─────────────────────────────────────────────────────────────────────────────
JOB_OUTPUT_DIR="${OUTPUTS_ROOT}/${JOB_ID}"
echo
if [ -d "$JOB_OUTPUT_DIR" ]; then
    echo "✓ Done.  Operator artefacts in:"
    echo "   ${JOB_OUTPUT_DIR}/"
    if [ -f "${JOB_OUTPUT_DIR}/result.pdf" ]; then
        sz=$(stat -c%s "${JOB_OUTPUT_DIR}/result.pdf")
        echo "   └─ result.pdf (${sz} bytes) — open with:  xdg-open ${JOB_OUTPUT_DIR}/result.pdf"
    fi
    # List per-DICOM subdirs
    find "${JOB_OUTPUT_DIR}" -mindepth 1 -maxdepth 1 -type d -printf "   └─ %f/\n" 2>/dev/null || true
else
    echo "⚠ No operator artefacts found at ${JOB_OUTPUT_DIR} (job may still be running or have failed)."
    echo "  Check the worker log:  sudo grep ${JOB_ID} ${WORKER_LOG}"
fi

exit "$STATUS"
