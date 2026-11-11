#!/bin/sh
# Resume stage 2 after the 2026-09-29 stop: judge the finished raw seed-1 run, then raw seed 2, then both squared seeds.
cd /Users/benebsworth/projects/addr
PY=scripts/ml/training/.venv-sam3/bin/python
OUT=data/reviews/eyes/training/sam3-mac/sweep
LOG=$OUT/stage2.log
export PYTORCH_ENABLE_MPS_FALLBACK=0
export PYTORCH_MPS_HIGH_WATERMARK_RATIO=${PYTORCH_MPS_HIGH_WATERMARK_RATIO:-0.7}   # cap the MPS allocator; the low mark must stay below it
export PYTORCH_MPS_LOW_WATERMARK_RATIO=${PYTORCH_MPS_LOW_WATERMARK_RATIO:-0.5}
record() {   # name coco steps lr seed
  $PY - "$1" "$2" "$3" "$4" "$5" "$OUT" <<'PYEOF'
import json, sys
name, coco, steps, lr, seed, out = sys.argv[1:7]
row = {'run': name, 'coco': coco, 'steps': int(steps), 'lr': float(lr), 'seed': int(seed)}
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
print(json.dumps({k: row.get(k) for k in ('run', 'lr', 'seed', 'eval_error', 'train_error')}))
PYEOF
}
judge() {   # name
  $PY -W ignore -m scripts.ml.training.sam3_mac.reference_eval --arm base --arm "tuned=$OUT/$1/trainable-final.pt" --dtype fp32 --out "$OUT/$1/reference-eval" > "$OUT/$1.eval.log" 2>&1
  echo "eval $1 exit=$?" >> "$LOG"
}
train() {   # name coco seed
  $PY -W ignore -m scripts.ml.training.sam3_mac.finetune_mps --coco "$2" --steps 3000 --dtype bf16 --lr 8e-5 --seed "$3" --warmup 50 --save-every 1000 --log-every 100 --out "$OUT/$1" > "$OUT/$1.train.log" 2>&1
  echo "train $1 exit=$?" >> "$LOG"
}
RAW=data/reviews/eyes/training/sam3-mac/coco-teachers-v1
SQ=data/reviews/eyes/training/sam3-mac/coco-teachers-squared-v1
echo "$(date -u +%FT%TZ) stage 2 resumed (MPS cache returned each step, allocator capped at 60 percent)" >> "$LOG"
if [ -f "$OUT/s2-raw-lr8e-5-seed1/trainable-final.pt" ] && [ ! -f "$OUT/s2-raw-lr8e-5-seed1/reference-eval/summary.json" ]; then judge s2-raw-lr8e-5-seed1; record s2-raw-lr8e-5-seed1 "$RAW" 3000 8e-5 1 >> "$LOG"; fi
train s2-raw-lr8e-5-seed2 "$RAW" 2; judge s2-raw-lr8e-5-seed2; record s2-raw-lr8e-5-seed2 "$RAW" 3000 8e-5 2 >> "$LOG"
for seed in 1 2; do train "s2-squared-lr8e-5-seed$seed" "$SQ" "$seed"; judge "s2-squared-lr8e-5-seed$seed"; record "s2-squared-lr8e-5-seed$seed" "$SQ" 3000 8e-5 "$seed" >> "$LOG"; done
echo "$(date -u +%FT%TZ) stage 2 done" >> "$LOG"
