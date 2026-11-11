#!/usr/bin/env bash
# Runs ON the GPU box inside the unpacked bundle directory (run.sh puts it there and starts this detached).
# Verifies the dataset tarball, trains under the budget guard with an external hard cap, exports ONNX, re-evaluates
# the ONNX graph on the held-out references and writes a sha256 list of every artefact.
#   env: PROFILE (a10|a100|a10-regularised)  MODE (docker|venv)  CAP_MINUTES (training)  DATASET_SHA256
#        SETUP_CAP_MINUTES (image build / pip install, default 30)  POST_CAP_MINUTES (export, evaluation; default 15 each)
#        AUTO_SHUTDOWN_MINUTES (unset: never) schedules `sudo shutdown -h +N` after the job so an unattended box stops;
#        on most marketplaces a stopped instance still bills storage, a destroyed one does not.
set -uo pipefail
cd "$(dirname "$0")/../../../.." || exit 2
PROFILE=${PROFILE:-a10}; MODE=${MODE:-docker}; CAP_MINUTES=${CAP_MINUTES:-120}
POST_CAP_MINUTES=${POST_CAP_MINUTES:-15}; SETUP_CAP_MINUTES=${SETUP_CAP_MINUTES:-30}
echo $$ > box.pid
log() { printf '%s box.sh %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
finish() {
  log "BOX_DONE status=$1"; rm -f box.pid
  if [ -n "${AUTO_SHUTDOWN_MINUTES:-}" ]; then log "shutdown in ${AUTO_SHUTDOWN_MINUTES} min"; sudo -n shutdown -h "+${AUTO_SHUTDOWN_MINUTES}" || log "shutdown not permitted"; fi
  exit "$1"
}

log "profile=$PROFILE mode=$MODE cap=${CAP_MINUTES}m"
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
actual=$(sha256sum dataset-v1.tar | cut -d' ' -f1)
if [ -z "${DATASET_SHA256:-}" ] || [ "$actual" != "$DATASET_SHA256" ]; then
  log "dataset sha256 mismatch: expected ${DATASET_SHA256:-<unset>} got $actual"; finish 2
fi
mkdir -p work out/run
[ -d work/dataset-v1 ] || tar -xf dataset-v1.tar -C work || finish 2

if [ "$MODE" = docker ]; then
  printf 'work\nout\n*.tar\n*.tar.sha256\n*.log\n.venv-gpu\n' > .dockerignore
  timeout "${SETUP_CAP_MINUTES}m" docker build -t addr-roof-train -f scripts/ml/training/deploy/Dockerfile . || finish 4
  RUN=(docker run --rm --gpus all --shm-size 8g --user "$(id -u):$(id -g)" -e HOME=/tmp
       -v "$PWD/work:/app/work" -v "$PWD/out:/app/out" addr-roof-train python)
elif [ "$MODE" = venv ]; then
  if [ ! -x .venv-gpu/bin/python ]; then
    python3.12 -m venv .venv-gpu && timeout "${SETUP_CAP_MINUTES}m" .venv-gpu/bin/pip install -q -r scripts/ml/training/requirements-gpu.txt || finish 4
  fi
  RUN=(.venv-gpu/bin/python)
else
  log "unknown MODE=$MODE"; finish 2
fi

log "train start"
timeout --signal=TERM --kill-after=120 "${CAP_MINUTES}m" "${RUN[@]}" -m scripts.ml.training.train \
  --config "scripts/ml/training/configs/${PROFILE}.json" --dataset work/dataset-v1 --run-dir out/run --resume auto
status=$?
log "train exit=$status (0 done, 3 interrupted and resumable, 124 hard cap hit)"

checkpoint=out/run/best.pt; [ -f "$checkpoint" ] || checkpoint=out/run/last.pt
if [ -f "$checkpoint" ]; then
  check_image=$(ls work/dataset-v1/images/map-d34a1fe62200fa75.png 2>/dev/null || ls work/dataset-v1/images/*.png | head -1)
  timeout "${POST_CAP_MINUTES}m" "${RUN[@]}" -m scripts.ml.training.export_onnx --checkpoint "$checkpoint" \
    --out out/run/roof_outline.onnx --check-image "$check_image" || log "onnx export failed"
  if [ -f out/run/roof_outline.onnx ]; then
    timeout "${POST_CAP_MINUTES}m" "${RUN[@]}" -m scripts.ml.training.evaluate --dataset work/dataset-v1 \
      --onnx out/run/roof_outline.onnx --out out/run/onnx_eval > out/run/onnx_eval.stdout.json || log "onnx evaluation failed"
  fi
fi
(cd out/run 2>/dev/null && find . -type f ! -name ARTEFACTS.sha256 -print0 | sort -z | xargs -0 sha256sum > ARTEFACTS.sha256)
finish "$status"
