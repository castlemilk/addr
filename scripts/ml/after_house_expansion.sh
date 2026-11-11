#!/bin/sh
# After scripts/ml/expand_house_scenes.sh: refresh the confidence index for every scene the batch expanded, carry
# machine-only reviews onto the new frames through the editor's own recovery path, run the eyes guard, and export the
# outline dataset as dataset-v2 on the widened images. Human-decided scenes are NOT carried here (the reviewer decides).
# Usage: sh scripts/ml/after_house_expansion.sh [--out data/reviews/eyes/training/dataset-v2]
cd /Users/benebsworth/projects/addr
OUT=${2:-data/reviews/eyes/training/dataset-v2}
LOG=.run/after-house-expansion.log
PY=scripts/ml/.venv/bin/python
# A scene counts as expanded when its own log holds the extractor's summary line and the pixel check passes; the exit
# code alone is not enough (house-29888 wrote everything, then the ONNX runtime aborted at interpreter teardown, 134).
SCENES=""
: > "$LOG"
for id in $(grep -o "expanding house-[0-9]*" .run/expand-house-scenes.log | awk '{print $2}' | sort -u); do
  if ! grep -q "^{\"scene\": \"$id\"" ".run/expand-$id.log"; then echo "skip $id: no summary line (extraction did not finish)" >> "$LOG"; continue; fi
  old=$(python3 -c "import json; print(json.load(open('.run/expand-backup-$id.latest.json'))['model_signature'])")
  new=$(python3 -c "import json; print(json.load(open('data/scenes/$id/latest.json'))['model_signature'])")
  if [ "$old" = "$new" ]; then echo "skip $id: model unchanged" >> "$LOG"; continue; fi
  if $PY -m scripts.ml.scene_expand --verify-previous "data/scenes/$id/models/$old.json" --verify-current "data/scenes/$id/models/$new.json" >/dev/null 2>&1; then
    SCENES="$SCENES $id"
  else
    echo "FAILED pixel check $id (old $old new $new): not refreshed, needs a look" >> "$LOG"
  fi
done
echo "$(date -u +%FT%TZ) confidence refresh for: $(echo $SCENES | wc -w) scenes" >> "$LOG"
# shellcheck disable=SC2086
$PY -m scripts.ml.scene_confidence --scenes $SCENES 2>&1 | grep -v Warning | tail -3 >> "$LOG"
echo "$(date -u +%FT%TZ) carry-forward (machine-only reviews)" >> "$LOG"
# shellcheck disable=SC2086
APP_URL=http://localhost:3008 node scripts/carry-forward-reviews.mjs --scenes $SCENES 2>&1 | tail -1 | cut -c1-400 >> "$LOG"
echo "$(date -u +%FT%TZ) eyes guard" >> "$LOG"
$PY -m scripts.ml.eyes_guard 2>&1 | grep -v Warning | python3 -c "import sys,json; t=sys.stdin.read(); d=json.loads(t[t.index('{'):]); print('guard ok', d['ok'], 'failing', [c['name'] for c in d['checks'] if not c['ok']])" >> "$LOG"
echo "$(date -u +%FT%TZ) dataset export -> $OUT" >> "$LOG"
$PY -m scripts.ml.training.export_dataset --out "$OUT" > "$OUT.export.json" 2> .run/dataset-v2.export.err
echo "export exit=$?" >> "$LOG"
python3 - "$OUT" <<'PY' >> "$LOG"
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
for name in ('dataset-v1', out.name):
    m = json.load(open(out.parent / name / 'manifest.json'))
    imgs = m['images']; sizes = {}
    for i in imgs: sizes[(i.get('width'), i.get('height'))] = sizes.get((i.get('width'), i.get('height')), 0) + 1
    splits = {}
    for i in imgs: splits[i['split']] = splits.get(i['split'], 0) + 1
    print(name, 'images', len(imgs), 'splits', splits, 'sizes', sizes, 'ignore', m.get('ignore_reasons', {}).get('clipped_by_frame', 0), 'clipped_by_frame;', 'exclusions', {k: m['exclusions'].get(k) for k in ('review_frame_mismatch', 'clipped_by_frame')})
PY
echo "$(date -u +%FT%TZ) done" >> "$LOG"
tail -12 "$LOG"
