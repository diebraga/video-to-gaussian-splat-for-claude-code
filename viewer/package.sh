#!/usr/bin/env bash
# Bakes a .ply splat directly into a single, self-contained HTML file that
# opens straight in a browser via file:// — no local server needed.
#
# Usage: viewer/package.sh path/to/splat.ply [output.html]

set -euo pipefail

PLY_PATH="${1:?Usage: package.sh path/to/splat.ply [output.html]}"
OUT_PATH="${2:-${PLY_PATH%.ply}.html}"
VIEWER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER='<script src="main.js"></script>'

if [ ! -f "$PLY_PATH" ]; then
  echo "No such file: $PLY_PATH" >&2
  exit 1
fi

B64=$(base64 -i "$PLY_PATH" | tr -d '\n')

# Split index.html into everything before/after the main.js script tag, so
# it can be replaced with the embedded data + inlined main.js instead.
awk -v marker="$MARKER" 'index($0, marker) { exit } { print }' "$VIEWER_DIR/index.html" > "$OUT_PATH.head"
awk -v marker="$MARKER" 'found { print } index($0, marker) { found=1 }' "$VIEWER_DIR/index.html" > "$OUT_PATH.tail"

{
  cat "$OUT_PATH.head"
  printf '<script>window.__EMBEDDED_SPLAT_BASE64__ = "%s";</script>\n' "$B64"
  echo "<script>"
  cat "$VIEWER_DIR/main.js"
  echo "</script>"
  cat "$OUT_PATH.tail"
} > "$OUT_PATH"

rm -f "$OUT_PATH.head" "$OUT_PATH.tail"

echo "Wrote $OUT_PATH ($(du -h "$OUT_PATH" | cut -f1))"
echo "Open it directly — no server needed."
