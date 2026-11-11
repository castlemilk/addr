#!/bin/zsh
# Newcastle high-res COG (1024 px exports) plus its Sentinel-2 context COG.
set -u
cd /Users/benebsworth/projects/addr
ROOT=/Volumes/gamma-systems-2/addr-imagery/open
LOG=$ROOT/nsw-newcastle-1024.log
scripts/ml/training/.venv/bin/python -u -m scripts.ml.open_imagery fetch \
  --source nsw --bbox=151.74,-32.95,151.79,-32.91 --max-px 1024 --pause 0.1 \
  --out "$ROOT" >> "$LOG" 2>&1
scripts/ml/training/.venv/bin/python -u -m scripts.ml.open_imagery fetch \
  --source sentinel2 --bbox=151.74,-32.95,151.79,-32.91 --out "$ROOT" >> "$LOG" 2>&1
