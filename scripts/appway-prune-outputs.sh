#!/usr/bin/env bash
# Prune local outputs/<job-id>/ folders older than RETENTION_DAYS.
# The weekly digest now reads assets from S3, so local copies are only
# needed for short-term operator inspection.
set -euo pipefail

OUTPUTS=/home/ubuntu/appway-backend/outputs
RETENTION_DAYS=3

if [ ! -d "$OUTPUTS" ]; then
    echo "$(date -Iseconds) outputs dir missing — nothing to prune"
    exit 0
fi

mapfile -t victims < <(
    find "$OUTPUTS" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" 2>/dev/null
)

if [ "${#victims[@]}" -eq 0 ]; then
    echo "$(date -Iseconds) nothing older than ${RETENTION_DAYS}d — outputs clean"
    exit 0
fi

# Compute total size to report
bytes=$(du -sb "${victims[@]}" 2>/dev/null | awk '{s+=$1} END{print s+0}')
mb=$(( bytes / 1024 / 1024 ))
echo "$(date -Iseconds) pruning ${#victims[@]} job folder(s) (~${mb} MB) older than ${RETENTION_DAYS}d"

for v in "${victims[@]}"; do
    echo "  rm -rf $v"
    rm -rf "$v"
done
echo "$(date -Iseconds) prune complete"
