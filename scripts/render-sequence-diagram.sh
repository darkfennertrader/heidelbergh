#!/usr/bin/env bash
#
# Render the mermaid sequence diagram in docs/workflow.md into a PNG.
#
# Usage:
#   ./scripts/render-sequence-diagram.sh
#
# Output:
#   docs/workflow.png
#
# Requirements:
#   curl, base64, awk  (all standard)
#
# Uses the public mermaid.ink rendering service:
#   https://mermaid.ink/img/<base64-url-safe-encoded-mermaid-source>

set -euo pipefail

# Resolve repo root (this script lives in <repo>/scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INPUT_MD="${REPO_ROOT}/docs/workflow.md"
OUTPUT_PNG="${REPO_ROOT}/docs/workflow.png"

if [[ ! -f "${INPUT_MD}" ]]; then
    echo "ERROR: ${INPUT_MD} not found" >&2
    exit 1
fi

echo "Extracting mermaid block from ${INPUT_MD}..."

# Extract the first ```mermaid ... ``` block from the markdown file.
MERMAID_SRC="$(awk '
    /^```mermaid[[:space:]]*$/ { in_block = 1; next }
    /^```[[:space:]]*$/        { if (in_block) { in_block = 0; exit } }
    in_block                   { print }
' "${INPUT_MD}")"

if [[ -z "${MERMAID_SRC}" ]]; then
    echo "ERROR: no \`\`\`mermaid block found in ${INPUT_MD}" >&2
    exit 1
fi

echo "Encoding diagram (URL-safe base64)..."

# mermaid.ink expects URL-safe base64 (no line breaks, '+'→'-', '/'→'_', no padding).
ENCODED="$(printf '%s' "${MERMAID_SRC}" \
    | base64 -w 0 \
    | tr '+/' '-_' \
    | tr -d '=')"

# Render options:
#   type=png         → PNG output
#   bgColor=!white   → solid white background (the '!' prefix makes it opaque,
#                      otherwise mermaid.ink keeps a transparent paper under it)
#   width=3000       → target width in pixels for higher definition / crisp text
URL="https://mermaid.ink/img/${ENCODED}?type=png&bgColor=!white&width=3000"



echo "Requesting: ${URL:0:80}... (${#ENCODED} chars)"

# Fetch the rendered image.
HTTP_CODE="$(curl -sS -L \
    --max-time 30 \
    -w '%{http_code}' \
    -o "${OUTPUT_PNG}" \
    "${URL}")"

if [[ "${HTTP_CODE}" != "200" ]]; then
    echo "ERROR: mermaid.ink returned HTTP ${HTTP_CODE}" >&2
    echo "Response body saved to ${OUTPUT_PNG} for debugging." >&2
    exit 1
fi

# Verify the output is actually an image (not an HTML error page).
FILE_TYPE="$(file -b --mime-type "${OUTPUT_PNG}")"
if [[ "${FILE_TYPE}" != image/* ]]; then
    echo "ERROR: mermaid.ink did not return an image (got ${FILE_TYPE})" >&2
    echo "Content:" >&2
    head -c 500 "${OUTPUT_PNG}" >&2
    echo >&2
    exit 1
fi

SIZE="$(stat -c '%s' "${OUTPUT_PNG}" 2>/dev/null || stat -f '%z' "${OUTPUT_PNG}")"
echo "✓ Wrote ${OUTPUT_PNG} (${FILE_TYPE}, ${SIZE} bytes)"
