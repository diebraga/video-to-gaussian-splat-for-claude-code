#!/bin/bash
# Scores every frame_*.jpg in the given video folder(s) for blur (ffmpeg blurdetect:
# higher score = blurrier), and moves the blurriest ones out of the way.
#
# Usage: ./filter_blurry_frames.sh <videoN_folder> [<videoN_folder> ...] [--percent N]
#
# ponytail: percentile cutoff is a simple heuristic, not a validated threshold.
# Tune --percent (default 15, i.e. drop the blurriest 15%) per scene if needed.

set -euo pipefail

PERCENT=15
DIRS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --percent) PERCENT="$2"; shift 2 ;;
    *) DIRS+=("$1"); shift ;;
  esac
done

if [ ${#DIRS[@]} -eq 0 ]; then
  echo "Usage: $0 <videoN_folder> [<videoN_folder> ...] [--percent N]"
  exit 1
fi

for DIR in "${DIRS[@]}"; do
  DIR="${DIR%/}"
  EXCLUDED="$DIR/excluded_blurry"
  mkdir -p "$EXCLUDED"
  SCORES_FILE=$(mktemp)

  echo "=== Scoring frames in $DIR ==="
  n=0
  for f in "$DIR"/frame_*.jpg; do
    [ -e "$f" ] || continue
    score=$(ffprobe -f lavfi -i "movie='$f',blurdetect" -show_entries frame_tags=lavfi.blur -of csv=p=0 -v quiet)
    echo "$score $f" >> "$SCORES_FILE"
    n=$((n + 1))
  done

  if [ "$n" -eq 0 ]; then
    echo "  no frame_*.jpg files found, skipping"
    rm -f "$SCORES_FILE"
    continue
  fi

  cutoff_count=$(( n * PERCENT / 100 ))
  echo "  $n frames scored, dropping the blurriest $cutoff_count (top ${PERCENT}%)"

  sort -rn "$SCORES_FILE" | head -n "$cutoff_count" | while read -r score path; do
    mv "$path" "$EXCLUDED/"
  done

  echo "  kept: $(ls "$DIR"/frame_*.jpg 2>/dev/null | wc -l | tr -d ' ') | excluded: $(ls "$EXCLUDED" | wc -l | tr -d ' ')"
  rm -f "$SCORES_FILE"
done
