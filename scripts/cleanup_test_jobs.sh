#!/usr/bin/env bash
#
# cleanup_test_jobs.sh — remove all artefacts produced by manual `test-*`
# injections (see scripts/inject_job.sh) from the four storage locations
# involved in the AppWay pipeline.
#
# What it deletes (only `test-*` / `result-test-*` prefixes — real production
# `final-*` / `result-final-*` data is NEVER touched):
#
#   1. S3   s3://appway-bridge-prod/incoming/test-*/
#   2. S3   s3://appway-bridge-prod/results/test-*/
#   3. Local /home/ubuntu/appway-backend/outputs/test-*/
#   4. Win  D:\AISolutionFolder\result-test-*\     (via SSM)
#
# Usage:
#   scripts/cleanup_test_jobs.sh [-h|--help]
#
# Exit code:
#   0 — all four locations cleaned
#   non-zero — at least one location failed (script aborts at the first failure)
#
# Requirements (already present on the backend EC2):
#   - boto3 in the project venv at /home/ubuntu/appway-backend/.venv
#   - IAM role on the EC2: s3:ListBucket + s3:DeleteObject on appway-bridge-prod
#                          ssm:SendCommand / ssm:GetCommandInvocation for the
#                          Windows EC2 (i-02a99abeba370f0a7)
#
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────
AWS_REGION="${AWS_REGION:-eu-west-1}"
S3_BUCKET="${S3_BUCKET:-appway-bridge-prod}"
OUTPUTS_ROOT="${OUTPUTS_ROOT:-/home/ubuntu/appway-backend/outputs}"
VENV_PY="${VENV_PY:-/home/ubuntu/appway-backend/.venv/bin/python3}"
SSM_HELPER="${SSM_HELPER:-/home/ubuntu/appway-backend/scripts/ssm_run.py}"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
die() { echo "error: $*" >&2; exit 1; }

usage() {
    sed -n '2,28p' "$0" | sed 's/^#\s\{0,1\}//'
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse args
# ─────────────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage ;;
        *)         die "unknown argument: $1 (use --help)" ;;
    esac
done

[ -x "$VENV_PY" ]    || die "project venv python not found at $VENV_PY (run 'uv sync' first)"
[ -f "$SSM_HELPER" ] || die "SSM helper not found at $SSM_HELPER"

echo "════════════════════════════════════════════════════════════════════════"
echo "  AppWay backend — cleanup of manual test-* injections"
echo "  S3 bucket          : ${S3_BUCKET}"
echo "  Local outputs dir  : ${OUTPUTS_ROOT}"
echo "  Windows EC2        : i-02a99abeba370f0a7 (D:\\AISolutionFolder)"
echo "════════════════════════════════════════════════════════════════════════"

# ─────────────────────────────────────────────────────────────────────────────
# 1 + 2. S3 — delete incoming/test-*/ and results/test-*/
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "[1/3] Cleaning S3 test-* objects under incoming/ and results/…"
AWS_REGION="$AWS_REGION" S3_BUCKET="$S3_BUCKET" "$VENV_PY" - <<'PYEOF'
import os, sys
import boto3

region = os.environ["AWS_REGION"]
bucket = os.environ["S3_BUCKET"]

s3 = boto3.client("s3", region_name=region)

to_delete = []
for root in ("incoming/", "results/"):
    paginator = s3.get_paginator("list_objects_v2")
    # Listing with prefix "incoming/test-" / "results/test-" means we match
    # ONLY test-* subfolders and never touch incoming/final-* or results/final-*.
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{root}test-"):
        for obj in page.get("Contents", []):
            to_delete.append({"Key": obj["Key"]})

print(f"  Found {len(to_delete)} test-* object(s) to delete.")
if not to_delete:
    sys.exit(0)

# Batch delete (max 1000 per call)
total_deleted = 0
for i in range(0, len(to_delete), 1000):
    batch = to_delete[i:i+1000]
    resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
    errs = resp.get("Errors", [])
    if errs:
        print(f"  S3 delete errors: {errs}", file=sys.stderr)
        sys.exit(1)
    total_deleted += len(batch)
    for obj in batch:
        print(f"  ✓ deleted s3://{bucket}/{obj['Key']}")

print(f"  Deleted {total_deleted} S3 object(s).")
PYEOF
S3_STATUS=$?
[ "$S3_STATUS" -eq 0 ] || die "S3 cleanup failed (exit $S3_STATUS)"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Local — delete outputs/test-*/
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "[2/3] Cleaning local ${OUTPUTS_ROOT}/test-* directories…"
if [ ! -d "$OUTPUTS_ROOT" ]; then
    echo "  (no outputs dir at $OUTPUTS_ROOT — nothing to do)"
else
    # Use find with -maxdepth 1 to avoid recursing further than needed.
    mapfile -t TEST_DIRS < <(find "$OUTPUTS_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'test-*' -print)
    if [ "${#TEST_DIRS[@]}" -eq 0 ]; then
        echo "  No local test-* directories found."
    else
        for d in "${TEST_DIRS[@]}"; do
            rm -rf -- "$d" || die "Failed to remove local directory: $d"
            echo "  ✓ deleted $d"
        done
        echo "  Deleted ${#TEST_DIRS[@]} local directory(ies)."
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. Windows — delete D:\AISolutionFolder\result-test-*\ via SSM
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "[3/3] Cleaning D:\\AISolutionFolder\\result-test-* on the Windows EC2 via SSM…"

# PowerShell one-liner — enumerates result-test-* dirs, removes them, prints
# a line per deletion.  We pipe the output through and fail-hard if SSM reports
# non-success.
PS_SCRIPT=$(cat <<'PSEOF'
$ErrorActionPreference = 'Stop'
$root = 'D:\AISolutionFolder'
if (-not (Test-Path $root)) {
    Write-Output "  (no $root — nothing to do)"
    exit 0
}
$dirs = Get-ChildItem -Path $root -Directory -Filter 'result-test-*' -ErrorAction SilentlyContinue
if ($null -eq $dirs -or $dirs.Count -eq 0) {
    Write-Output "  No result-test-* directories found on Windows."
    exit 0
}
foreach ($d in $dirs) {
    try {
        Remove-Item -Path $d.FullName -Recurse -Force -ErrorAction Stop
        Write-Output ("  deleted " + $d.FullName)
    } catch {
        Write-Error ("  FAILED to delete " + $d.FullName + " : " + $_.Exception.Message)
        exit 1
    }
}
Write-Output ("  Deleted " + $dirs.Count + " Windows directory(ies).")
PSEOF
)

"$VENV_PY" "$SSM_HELPER" "$PS_SCRIPT" || die "Windows SSM cleanup failed"

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════════════"
echo "  ✓ All four locations cleaned — S3 incoming/test-*, S3 results/test-*,"
echo "    local outputs/test-*, Windows D:\\AISolutionFolder\\result-test-*"
echo "════════════════════════════════════════════════════════════════════════"
