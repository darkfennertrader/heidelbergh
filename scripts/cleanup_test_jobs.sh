#!/usr/bin/env bash
#
# cleanup_test_jobs.sh — remove artefacts produced by job injections from the
# four storage locations involved in the AppWay pipeline.
#
# What it deletes:
#   --test  (default)  test-* / result-test-*   — safe to run anytime
#   --final            final-* / result-final-*  — ⚠  real production data!
#   --all              both of the above
#
# Locations cleaned for every selected prefix:
#   1. S3   s3://appway-bridge-prod/incoming/<prefix>-*/
#   2. S3   s3://appway-bridge-prod/results/<prefix>-*/
#   3. Local /home/ubuntu/appway-backend/outputs/<prefix>-*/
#   4. Win  D:\AISolutionFolder\result-<prefix>-*\     (via SSM)
#
# Usage:
#   scripts/cleanup_test_jobs.sh [--test|--final|--all] [-y|--yes] [-h|--help]
#
#   --test     Clean test-* artefacts only (DEFAULT when no flag given)
#   --final    Clean final-* artefacts only  ⚠  requires confirmation
#   --all      Clean both test-* and final-*  ⚠  requires confirmation
#   -y|--yes   Skip the safety confirmation prompt (for automation)
#   -h|--help  Show this help and exit
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
    sed -n '2,31p' "$0" | sed 's/^#\s\{0,1\}//'
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse args
# ─────────────────────────────────────────────────────────────────────────────
MODE="test"       # default — clean test-* only
SKIP_CONFIRM=0    # 0 = ask, 1 = skip prompt (-y/--yes)

while [ $# -gt 0 ]; do
    case "$1" in
        --test)        MODE="test"  ;;
        --final)       MODE="final" ;;
        --all)         MODE="all"   ;;
        -y|--yes)      SKIP_CONFIRM=1 ;;
        -h|--help)     usage ;;
        *)             die "unknown argument: $1 (use --help)" ;;
    esac
    shift
done

# Build the list of prefixes to clean
case "$MODE" in
    test)  PREFIXES=("test") ;;
    final) PREFIXES=("final") ;;
    all)   PREFIXES=("test" "final") ;;
    *)     die "internal error: unknown MODE=$MODE" ;;
esac

# ─────────────────────────────────────────────────────────────────────────────
# Safety confirmation when touching final-* (real production data)
# ─────────────────────────────────────────────────────────────────────────────
if [[ "$MODE" == "final" || "$MODE" == "all" ]]; then
    echo ""
    echo "  ⚠️  WARNING — you are about to PERMANENTLY DELETE production 'final-*' artefacts:"
    echo ""
    echo "      s3://${S3_BUCKET}/incoming/final-*"
    echo "      s3://${S3_BUCKET}/results/final-*"
    echo "      ${OUTPUTS_ROOT}/final-*"
    echo "      D:\\AISolutionFolder\\result-final-*  (via SSM)"
    echo ""
    if [ "$SKIP_CONFIRM" -eq 1 ]; then
        echo "  (-y/--yes flag set — skipping confirmation prompt)"
    else
        read -r -p "  Type 'YES' to confirm: " REPLY
        if [ "$REPLY" != "YES" ]; then
            echo "  Aborted — nothing was deleted."
            exit 0
        fi
    fi
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────────────────
[ -x "$VENV_PY" ]    || die "project venv python not found at $VENV_PY (run 'uv sync' first)"
[ -f "$SSM_HELPER" ] || die "SSM helper not found at $SSM_HELPER"

# Human-readable label for the banner
PREFIX_LABEL="${PREFIXES[*]}"   # e.g. "test" / "final" / "test final"

echo "════════════════════════════════════════════════════════════════════════"
echo "  AppWay backend — cleanup of [${PREFIX_LABEL}] job artefacts"
echo "  S3 bucket          : ${S3_BUCKET}"
echo "  Local outputs dir  : ${OUTPUTS_ROOT}"
echo "  Windows EC2        : i-02a99abeba370f0a7 (D:\\AISolutionFolder)"
echo "════════════════════════════════════════════════════════════════════════"

# ─────────────────────────────────────────────────────────────────────────────
# 1 + 2. S3 — delete matching objects under incoming/ and results/
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "[1/3] Cleaning S3 objects under incoming/ and results/ for prefix(es): ${PREFIX_LABEL}…"
AWS_REGION="$AWS_REGION" S3_BUCKET="$S3_BUCKET" JOB_PREFIXES="${PREFIX_LABEL}" \
    "$VENV_PY" - <<'PYEOF'
import os, sys
import boto3

region  = os.environ["AWS_REGION"]
bucket  = os.environ["S3_BUCKET"]
prefixes = os.environ["JOB_PREFIXES"].split()   # e.g. ["test"] or ["test","final"]

s3 = boto3.client("s3", region_name=region)

to_delete = []
for root in ("incoming/", "results/"):
    for p in prefixes:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{root}{p}-"):
            for obj in page.get("Contents", []):
                to_delete.append({"Key": obj["Key"]})

print(f"  Found {len(to_delete)} matching object(s) to delete.")
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
# 3. Local — delete outputs/<prefix>-* directories
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "[2/3] Cleaning local ${OUTPUTS_ROOT}/<prefix>-* directories for: ${PREFIX_LABEL}…"
if [ ! -d "$OUTPUTS_ROOT" ]; then
    echo "  (no outputs dir at $OUTPUTS_ROOT — nothing to do)"
else
    TOTAL_LOCAL=0
    for p in "${PREFIXES[@]}"; do
        mapfile -t MATCHED_DIRS < <(find "$OUTPUTS_ROOT" -mindepth 1 -maxdepth 1 -type d -name "${p}-*" -print)
        if [ "${#MATCHED_DIRS[@]}" -eq 0 ]; then
            echo "  No local ${p}-* directories found."
        else
            for d in "${MATCHED_DIRS[@]}"; do
                rm -rf -- "$d" || die "Failed to remove local directory: $d"
                echo "  ✓ deleted $d"
            done
            echo "  Deleted ${#MATCHED_DIRS[@]} local ${p}-* directory(ies)."
            TOTAL_LOCAL=$(( TOTAL_LOCAL + ${#MATCHED_DIRS[@]} ))
        fi
    done
    if [ "$TOTAL_LOCAL" -eq 0 ]; then
        echo "  No local directories found for any selected prefix."
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. Windows — delete D:\AISolutionFolder\result-<prefix>-*\ via SSM
# ─────────────────────────────────────────────────────────────────────────────
echo
echo "[3/3] Cleaning D:\\AISolutionFolder\\result-<prefix>-* on the Windows EC2 via SSM…"
echo "      Prefix(es): ${PREFIX_LABEL}"

# Build a PowerShell array literal from the prefixes, e.g. @('test') or @('test','final')
PS_PREFIX_ARRAY="@($(printf "'%s'," "${PREFIXES[@]}" | sed 's/,$//') )"

PS_SCRIPT=$(cat <<PSEOF
\$ErrorActionPreference = 'Stop'
\$root = 'D:\\AISolutionFolder'
if (-not (Test-Path \$root)) {
    Write-Output "  (no \$root — nothing to do)"
    exit 0
}
\$prefixes = ${PS_PREFIX_ARRAY}
\$totalDeleted = 0
foreach (\$p in \$prefixes) {
    \$filter = "result-\$p-*"
    \$dirs = Get-ChildItem -Path \$root -Directory -Filter \$filter -ErrorAction SilentlyContinue
    if (\$null -eq \$dirs -or \$dirs.Count -eq 0) {
        Write-Output "  No \$filter directories found on Windows."
        continue
    }
    foreach (\$d in \$dirs) {
        try {
            Remove-Item -Path \$d.FullName -Recurse -Force -ErrorAction Stop
            Write-Output ("  deleted " + \$d.FullName)
        } catch {
            Write-Error ("  FAILED to delete " + \$d.FullName + " : " + \$_.Exception.Message)
            exit 1
        }
    }
    Write-Output ("  Deleted " + \$dirs.Count + " Windows \$filter director(ies).")
    \$totalDeleted += \$dirs.Count
}
if (\$totalDeleted -eq 0) {
    Write-Output "  No Windows directories found for any selected prefix."
}
PSEOF
)

"$VENV_PY" "$SSM_HELPER" "$PS_SCRIPT" || die "Windows SSM cleanup failed"

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────

# Build the summary line
SUMMARY_PARTS=()
for p in "${PREFIXES[@]}"; do
    SUMMARY_PARTS+=("S3 incoming/${p}-*, S3 results/${p}-*, local outputs/${p}-*, Windows result-${p}-*")
done
SUMMARY=$(printf "    %s\n" "${SUMMARY_PARTS[@]}")

echo
echo "════════════════════════════════════════════════════════════════════════"
echo "  ✓ All four locations cleaned for prefix(es): ${PREFIX_LABEL}"
echo "$SUMMARY"
echo "════════════════════════════════════════════════════════════════════════"
