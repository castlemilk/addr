#!/bin/zsh
# Resume the Google tile crawl across runtime restarts, then build its R*Tree.
# Run inside a detached tmux session, not as an OpenCode background shell.
set -u
cd /Users/benebsworth/projects/addr
ROOT=/Volumes/gamma-systems-2/addr-tiles/google
LOG=$ROOT/crawl-resume3.log
while true; do
  scripts/ml/.venv/bin/python -u -m scripts.ml.google_tiles \
    --out "$ROOT" --zooms 18,19,20 --interval 0.5 --acknowledge-terms \
    --max-consecutive-failures 50 >> "$LOG" 2>&1
  status=$(scripts/ml/.venv/bin/python - <<'PY'
import json
try:
    print(json.load(open('/Volumes/gamma-systems-2/addr-tiles/google/latest.json'))['crawl']['status'])
except Exception:
    print('unknown')
PY
)
  [[ "$status" == "complete" ]] && break
  sleep 30
done
scripts/ml/.venv/bin/python -m scripts.ml.tile_index \
  --db "$ROOT/tiles.db" --provider google --layer satellite \
  --licence "Google Maps terms: internal review archive, not training" \
  --source "https://mt1.google.com/vt/lyrs=s" >> "$ROOT/index-google.log" 2>&1
