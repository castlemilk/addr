#!/usr/bin/env bash
# Local end-to-end check before any GPU spend (CPU, random init, ~2-3 minutes on an M-series Mac):
#   1. export 3 tiles (2 train suburb tiles + 1 held-out capture with human references) into a temp folder
#   2. smoke-train 2 steps, then resume to step 3 (checkpoint/resume path)
#   3. export ONNX and check torch-vs-onnxruntime parity
#   4. infer on one tile through the Sam3TextSegmenter-style interface and assert shapes
#   5. evaluate the ONNX graph against the held-out references (runs; numbers are meaningless for a 3-step model)
# Prints VALIDATE_LOCAL_OK on success. Writes only under $OUT (default: a fresh temp dir).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../../../.." && pwd)
cd "$REPO"
PY=${TRAIN_PY:-scripts/ml/training/.venv/bin/python}
OUT=${OUT:-$(mktemp -d -t addr-roof-validate)}
mkdir -p "$OUT"
mkdir -p "$OUT"
TRAIN_A=mooroolbark-50011498-17-118437-80408
TRAIN_B=mooroolbark-50011498-17-118438-80408
TEST_C=map-d34a1fe62200fa75
step() { printf '\n== %s\n' "$*"; }
[ -x "$PY" ] || { echo "training venv missing: $PY (see scripts/ml/training/README.md)" >&2; exit 2; }

step "1/5 export 3 tiles -> $OUT/dataset"
"$PY" -m scripts.ml.training.export_dataset --out "$OUT/dataset" --scene-ids "$TRAIN_A" "$TRAIN_B" "$TEST_C" > "$OUT/export.json"
"$PY" - "$OUT/dataset" <<'PY'
import json, sys
from pathlib import Path
from scripts.ml.training import common
root = Path(sys.argv[1])
m = json.loads((root / 'manifest.json').read_text())
splits = {i['scene_id']: i['split'] for i in m['images']}
assert len(m['images']) == 3, m['images']
assert list(splits.values()).count('train') >= 1, splits
assert splits['map-d34a1fe62200fa75'] == 'test', splits
refs = json.loads((root / 'references.json').read_text())['references']
assert sum(r['boundary_reference'] for r in refs) >= 1, 'no boundary reference in the test tile'
for image in m['images']:
    assert common.sha256_file(root / image['file']) == image['sha256']
    assert common.sha256_file(root / image['label_file']) == image['label_sha256']
print('export ok:', splits, 'boundary refs', sum(r['boundary_reference'] for r in refs))
PY

step "2/5 smoke train (2 steps) and resume to step 3"
"$PY" -m scripts.ml.training.train --smoke --dataset "$OUT/dataset" --run-dir "$OUT/run" > "$OUT/train.json"
"$PY" -m scripts.ml.training.train --smoke --dataset "$OUT/dataset" --run-dir "$OUT/run" --max-steps 3 --skip-test > "$OUT/resume.json"
"$PY" - "$OUT/run" <<'PY'
import json, sys
from pathlib import Path
run = Path(sys.argv[1])
events = [json.loads(line) for line in (run / 'train_log.jsonl').read_text().splitlines()]
steps = [e['step'] for e in events if e['event'] == 'step']
assert steps == [1, 2, 3], steps
assert [e['event'] for e in events].count('resume') == 1
assert all(e['run_id'] == events[0]['run_id'] for e in events)
assert (run / 'last.pt').exists() and (run / 'best.pt').exists() and (run / 'summary.json').exists()
summary = json.loads((run / 'test_summary.json').read_text())
model = summary['strata']['same_image_all']['model']
assert model['n_references'] >= 1, model
print('train ok: steps', steps, 'stop', [e['reason'] for e in events if e['event'] == 'stop'], 'test refs', model['n_references'])
PY

step "3/5 export ONNX"
"$PY" -m scripts.ml.training.export_onnx --checkpoint "$OUT/run/best.pt" --out "$OUT/run/roof_outline.onnx" \
  --check-image "$OUT/dataset/images/$TEST_C.png" > "$OUT/onnx.json"
"$PY" - "$OUT/run/roof_outline.json" <<'PY'
import json, sys
p = json.loads(open(sys.argv[1]).read())['parity']
assert p['n_torch'] == p['n_onnx'], p
assert p['boxes_shape_onnx'][1:] == [4] and p['mask_prob_shape_onnx'][1:] == [1, 56, 56], p
if p['top_k']:
    assert p['max_box_diff_px'] < 1e-2 and p['max_score_diff'] < 1e-3, p
print('onnx ok:', {k: p[k] for k in ('n_torch', 'n_onnx', 'max_box_diff_px', 'max_score_diff', 'mask_prob_shape_onnx')})
PY

step "4/5 infer on one tile (proposal interface)"
"$PY" - "$OUT/run/roof_outline.onnx" "$OUT/dataset/images/$TRAIN_A.png" <<'PY'
import sys
import numpy as np
from PIL import Image
from scripts.ml.sam3 import Sam3Proposal
from scripts.ml.training.infer import RoofOutlineSegmenter
segmenter = RoofOutlineSegmenter(sys.argv[1])
image = Image.open(sys.argv[2]).convert('RGB')
features = segmenter.encode_image(image)
props = segmenter.proposals(features, 'building roof', image.size, threshold=0., max_proposals=5)
assert props, 'no proposals at threshold 0'
for p in props:
    assert isinstance(p, Sam3Proposal)
    assert p.mask.shape == (image.height, image.width) and p.mask.dtype == np.bool_, (p.mask.shape, p.mask.dtype)
    assert len(p.box) == 4 and all(0. <= v <= 1. for v in p.box), p.box
    assert isinstance(p.score, float)
assert segmenter.proposals(features, 'tree canopy', image.size) == []
half = segmenter.proposals(features, 'building roof', (image.width // 2, image.height // 2), threshold=0., max_proposals=1)
assert half[0].mask.shape == (image.height // 2, image.width // 2)
print('infer ok:', len(props), 'proposals, mask', props[0].mask.shape, props[0].mask.dtype, 'box', [round(v, 3) for v in props[0].box])
PY
if [ -x scripts/ml/.venv/bin/python ]; then
  scripts/ml/.venv/bin/python -m scripts.ml.training.infer --model "$OUT/run/roof_outline.onnx" \
    --image "$OUT/dataset/images/$TEST_C.png" --threshold 0 --max-proposals 3 > "$OUT/infer-main-venv.json"
  echo "main ML venv (no torch) runs the ONNX model: $(grep -c mask_shapes "$OUT/infer-main-venv.json") summary written"
fi

step "5/5 evaluate the ONNX graph on the held-out references"
"$PY" -m scripts.ml.training.evaluate --dataset "$OUT/dataset" --onnx "$OUT/run/roof_outline.onnx" --out "$OUT/onnx_eval" > "$OUT/evaluate.json"
test -s "$OUT/onnx_eval/test_summary.json"
echo "outputs in $OUT"
echo VALIDATE_LOCAL_OK
