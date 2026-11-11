#!/usr/bin/env bash
# Gated, chunked training for roof_corner_model.py: before every chunk of CHUNK iterations the job gate (.run/jobgate.sh)
# must be open; the chunk then resumes from the last checkpoint. Usage: roof_corner_model_gated.sh <val|full> [iters] [chunk]
set -u
cd "$(dirname "$0")/../../.."
MODE=$1; ITERS=${2:-4000}; CHUNK=${3:-500}; BUDGET=${BUDGET:-1500}; DEVICE=${DEVICE:-mps}
WORK=${WORK:-data/reviews/eyes/experiments/roof-corners-2026-10-10/model-v1}; EXTRA=${EXTRA:-}
PY=scripts/ml/training/.venv-sam3/bin/python
FLAG=""; [ "$MODE" = val ] && FLAG="--val"
. .run/jobgate.sh
done_iter() { $PY -c "import torch,sys;print(torch.load(sys.argv[1],map_location='cpu')['iter'])" "$WORK/ckpt-$MODE.pt" 2>/dev/null || echo 0; }
while :; do
  cur=$(done_iter); [ "$cur" -ge "$ITERS" ] && { echo "finished $MODE at $cur"; break; }
  until hdr=$(jobgate "$BUDGET"); do sleep 15; done
  echo "$hdr chunk_from=$cur" | tee -a "$WORK/jobgate-$MODE.log"
  nxt=$(( cur + CHUNK < ITERS ? cur + CHUNK : ITERS ))
  # the schedule spans ITERS; stop after this chunk by running to nxt with a stop flag
  $PY scripts/ml/experiments/roof_corner_model.py train $FLAG --resume --iters "$ITERS" --stop-at "$nxt" --device "$DEVICE" --work "$WORK" $EXTRA || exit 1
done
