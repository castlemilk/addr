#!/bin/sh
# Staged SAM3 fine-tune sweep on MPS (sam3-mac-sweep-v1). Stage 1: learning-rate sweep on raw teacher labels, one seed.
# Stage 2: best learning rate, raw and squared-label arms, two seeds, longer. Each run is judged on the human
# references (reference_eval.py) as soon as it finishes; results accumulate in sweep/results.jsonl.
# Usage: sh sweep.sh stage1 | stage2 <lr>
set -u
cd "$(dirname "$0")/../../../.."
PY=scripts/ml/training/.venv-sam3/bin/python
OUT=data/reviews/eyes/training/sam3-mac/sweep
export PYTORCH_ENABLE_MPS_FALLBACK=0
export PYTORCH_MPS_HIGH_WATERMARK_RATIO=${PYTORCH_MPS_HIGH_WATERMARK_RATIO:-0.7}   # cap the MPS allocator; the low mark must stay below it
export PYTORCH_MPS_LOW_WATERMARK_RATIO=${PYTORCH_MPS_LOW_WATERMARK_RATIO:-0.5}
run() {   # name coco steps lr seed
  name=$1; coco=$2; steps=$3; lr=$4; seed=$5
  echo "== $(date -u +%FT%TZ) run $name coco=$coco steps=$steps lr=$lr seed=$seed"
  $PY -W ignore -m scripts.ml.training.sam3_mac.finetune_mps --coco "$coco" --steps "$steps" --dtype bf16 --lr "$lr" \
      --seed "$seed" --warmup 50 --save-every 1000 --log-every 100 --out "$OUT/$name" > "$OUT/$name.train.log" 2>&1
  status=$?
  echo "train exit=$status"
  $PY -W ignore -m scripts.ml.training.sam3_mac.reference_eval --arm base --arm "tuned=$OUT/$name/trainable-final.pt" \
      --dtype fp32 --out "$OUT/$name/reference-eval" > "$OUT/$name.eval.log" 2>&1
  echo "eval exit=$?"
  $PY - "$name" "$coco" "$steps" "$lr" "$seed" "$OUT" <<'PYEOF'
import json, sys
name, coco, steps, lr, seed, out = sys.argv[1:7]
row = {'run': name, 'coco': coco, 'steps': int(steps), 'lr': float(lr), 'seed': int(seed)}
try:
    s = json.load(open(f'{out}/{name}/summary.json'))
    row.update({'skipped_batches': s.get('skipped_batches'), 'loss_first': s.get('loss_first'), 'loss_median_last_10': s.get('loss_median_last_10'),
                'seconds_per_step_median': s.get('seconds_per_step_median')})
except Exception as e:
    row['train_error'] = str(e)[:120]
try:
    e = json.load(open(f'{out}/{name}/reference-eval/summary.json'))['summary']
    row.update({'base': e['base'], 'tuned': e['tuned'], 'paired': e.get('paired'), 'negatives': e.get('negatives')})
except Exception as err:
    row['eval_error'] = str(err)[:120]
with open(f'{out}/results.jsonl', 'a') as h:
    h.write(json.dumps(row) + '\n')
print(json.dumps({k: row.get(k) for k in ('run', 'lr', 'seed', 'steps', 'loss_median_last_10', 'eval_error', 'train_error')}))
PYEOF
}
case "${1:-stage1}" in
  stage1)
    for lr in 8e-5 3e-4 1e-3; do
      run "s1-raw-lr$lr-seed1" data/reviews/eyes/training/sam3-mac/coco-teachers-v1 1500 "$lr" 1
    done ;;
  stage2)
    lr=${2:?best learning rate}
    for seed in 1 2; do
      run "s2-raw-lr$lr-seed$seed" data/reviews/eyes/training/sam3-mac/coco-teachers-v1 3000 "$lr" "$seed"
      run "s2-squared-lr$lr-seed$seed" data/reviews/eyes/training/sam3-mac/coco-teachers-squared-v1 3000 "$lr" "$seed"
    done ;;
esac
echo "== $(date -u +%FT%TZ) sweep ${1:-stage1} done"
