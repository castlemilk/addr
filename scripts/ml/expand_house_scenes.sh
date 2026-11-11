#!/bin/sh
# Expand every 512 px house scene to its 1024 px registered context (scene_extract --expand-context --roof-models),
# skipping the protected checkpoints and scenes already expanded. One scene at a time; each takes about five minutes.
# Usage: sh scripts/ml/expand_house_scenes.sh [--dry-run]
cd /Users/benebsworth/projects/addr
LOG=.run/expand-house-scenes.log
# this repo's build only: other checkouts build on this machine too
if pgrep -f "projects/addr/node_modules/.bin/next build" >/dev/null; then echo "a web build is running; expansion beside it has killed builds before (exit 137). Wait for it." >&2; exit 2; fi
PROTECTED="house-30286 house-30296"
for dir in data/scenes/house-*/; do
  id=$(basename "$dir")
  case " $PROTECTED " in *" $id "*) echo "skip protected $id" >> "$LOG"; continue;; esac
  width=$(python3 -c "import json; print(json.load(open('$dir/latest.json'))['image_width'])")
  if [ "$width" != "512" ]; then echo "skip $id (width $width)" >> "$LOG"; continue; fi
  if [ "${1:-}" = "--dry-run" ]; then echo "would expand $id" >> "$LOG"; continue; fi
  echo "$(date -u +%FT%TZ) expanding $id" >> "$LOG"
  cp "$dir/latest.json" ".run/expand-backup-$id.latest.json"
  scripts/ml/.venv/bin/python -m scripts.ml.scene_extract --scene-id "$id" --expand-context --roof-models > ".run/expand-$id.log" 2>&1
  code=$?
  grep -vE "Warning|warn|^\s*$" ".run/expand-$id.log" | tail -1 | cut -c1-200 >> "$LOG"
  echo "exit=$code" >> "$LOG"
done
echo "$(date -u +%FT%TZ) expansion batch done" >> "$LOG"
