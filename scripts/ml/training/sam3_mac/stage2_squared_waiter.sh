#!/bin/sh
# Runs the squared-label arm of stage 2 once the squared COCO set exists and the main stage-2 script has finished
# (its own squared runs fail fast while the set is still being exported). Detached; appends to sweep/stage2.log.
cd /Users/benebsworth/projects/addr
LOG=data/reviews/eyes/training/sam3-mac/sweep/stage2.log
LR=${1:-8e-5}
until [ -f data/reviews/eyes/training/sam3-mac/coco-teachers-squared-v1/manifest.json ]; do sleep 120; done
until ! pgrep -f "sweep.sh stage2" >/dev/null; do sleep 120; done
echo "$(date -u +%FT%TZ) squared set ready and stage 2 raw runs finished; running squared arm at lr $LR" >> "$LOG"
PY=scripts/ml/training/.venv-sam3/bin/python
OUT=data/reviews/eyes/training/sam3-mac/sweep
export PYTORCH_ENABLE_MPS_FALLBACK=0
export PYTORCH_MPS_HIGH_WATERMARK_RATIO=${PYTORCH_MPS_HIGH_WATERMARK_RATIO:-0.7}   # cap the MPS allocator; the low mark must stay below it
export PYTORCH_MPS_LOW_WATERMARK_RATIO=${PYTORCH_MPS_LOW_WATERMARK_RATIO:-0.5}
for seed in 1 2; do
  name="s2-squared-lr$LR-seed$seed"
  echo "== $(date -u +%FT%TZ) run $name" >> "$LOG"
  $PY -W ignore -m scripts.ml.training.sam3_mac.finetune_mps --coco data/reviews/eyes/training/sam3-mac/coco-teachers-squared-v1 --steps 3000 --dtype bf16 --lr "$LR" --seed "$seed" --warmup 50 --save-every 1000 --log-every 100 --out "$OUT/$name" > "$OUT/$name.train.log" 2>&1
  echo "train exit=$?" >> "$LOG"
  $PY -W ignore -m scripts.ml.training.sam3_mac.reference_eval --arm base --arm "tuned=$OUT/$name/trainable-final.pt" --dtype fp32 --out "$OUT/$name/reference-eval" > "$OUT/$name.eval.log" 2>&1
  echo "eval exit=$?" >> "$LOG"
  $PY - "$name" "$LR" "$seed" "$OUT" <<'PYEOF'
import json, sys
name, lr, seed, out = sys.argv[1:5]
row = {'run': name, 'coco': 'data/reviews/eyes/training/sam3-mac/coco-teachers-squared-v1', 'steps': 3000, 'lr': float(lr), 'seed': int(seed)}
try:
    s = json.load(open(f'{out}/{name}/summary.json'))
    row.update({'skipped_batches': s.get('skipped_batches'), 'loss_first': s.get('loss_first'), 'loss_median_last_10': s.get('loss_median_last_10'), 'seconds_per_step_median': s.get('seconds_per_step_median')})
except Exception as e:
    row['train_error'] = str(e)[:120]
try:
    e = json.load(open(f'{out}/{name}/reference-eval/summary.json'))['summary']
    row.update({'base': e['base'], 'tuned': e['tuned'], 'paired': e.get('paired'), 'negatives': e.get('negatives')})
except Exception as err:
    row['eval_error'] = str(err)[:120]
open(f'{out}/results.jsonl', 'a').write(json.dumps(row) + '\n')
PYEOF
done
echo "$(date -u +%FT%TZ) squared arm done" >> "$LOG"
