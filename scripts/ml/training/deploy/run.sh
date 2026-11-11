#!/usr/bin/env bash
# One command from the Mac: sync the dataset tarball and code to a rented GPU box, train under the budget,
# export ONNX, evaluate, and copy the artefacts back.
#
#   scripts/ml/training/deploy/run.sh user@gpu-box a10            # Docker on the box (default)
#   scripts/ml/training/deploy/run.sh user@gpu-box a100 --mode venv
#   scripts/ml/training/deploy/run.sh user@gpu-box a10 --fetch     # re-attach / copy results after a dropped ssh
#   scripts/ml/training/deploy/run.sh local a10 --mode venv         # already on the box (no ssh)
#   scripts/ml/training/deploy/run.sh user@gpu-box a10 --dry-run   # check tarball + bundle, print the plan, touch nothing remote
#
# env: DATASET (default data/reviews/eyes/training/dataset-v1; the folder must be named dataset-v1, the configs read
#      work/dataset-v1)  SSH_OPTS (e.g. "-p 2222 -i ~/.ssh/key")  REMOTE_DIR (default addr-roof-train, under $HOME)
#      HARD_CAP_MINUTES (training only; default a10 120, a100 95)  KEEP_LAST=1 to also copy last.pt back
#      AUTO_SHUTDOWN_MINUTES=20 to have the box power itself off 20 min after the job (needs passwordless sudo)
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../../../.." && pwd)
HOST=${1:?usage: run.sh HOST PROFILE [--mode docker|venv] [--fetch]}
PROFILE=${2:-a10}
shift 2 || shift $#
MODE=docker; FETCH=0; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE=$2; shift 2 ;;
    --fetch) FETCH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown flag $1" >&2; exit 2 ;;
  esac
done
case "$PROFILE" in
  a10|a10-regularised) CAP=${HARD_CAP_MINUTES:-120} ;;
  a100) CAP=${HARD_CAP_MINUTES:-95} ;;
  *) echo "profile must be a10, a100 or a10-regularised" >&2; exit 2 ;;
esac
DATASET=${DATASET:-$REPO/data/reviews/eyes/training/dataset-v1}
REMOTE_DIR=${REMOTE_DIR:-addr-roof-train}
STAMP=$(date +%Y%m%dT%H%M%S)
RESULTS=${RESULTS:-$REPO/data/reviews/eyes/training/runs-remote/$PROFILE-$STAMP}
read -r -a SSH_EXTRA <<< "${SSH_OPTS:-}"
SSH=(ssh ${SSH_EXTRA[@]+"${SSH_EXTRA[@]}"} "$HOST")   # bash 3.2 (macOS) safe with an empty array
say() { printf '[run.sh] %s\n' "$*"; }

remote() { if [ "$HOST" = local ]; then (cd "$HOME/$REMOTE_DIR" && bash -c "$1"); else "${SSH[@]}" "cd $REMOTE_DIR && $1"; fi; }

fetch() {
  mkdir -p "$RESULTS"
  local exclude=(--exclude last.pt --exclude '*.tmp')
  [ "${KEEP_LAST:-0}" = 1 ] && exclude=(--exclude '*.tmp')
  if [ "$HOST" = local ]; then
    rsync -a "${exclude[@]}" "$HOME/$REMOTE_DIR/out/run/" "$RESULTS/"; cp "$HOME/$REMOTE_DIR/box.log" "$RESULTS/" || true
  else
    rsync -a -e "ssh ${SSH_OPTS:-}" "${exclude[@]}" "$HOST:$REMOTE_DIR/out/run/" "$RESULTS/"
    rsync -a -e "ssh ${SSH_OPTS:-}" "$HOST:$REMOTE_DIR/box.log" "$RESULTS/" || true
  fi
  say "artefacts in $RESULTS"
  if command -v sha256sum >/dev/null; then check=(sha256sum -c --quiet); else check=(shasum -a 256 -c --quiet); fi
  if [ -f "$RESULTS/ARTEFACTS.sha256" ]; then
    grep -v ' ./last.pt$' "$RESULTS/ARTEFACTS.sha256" > "$RESULTS/.copied.sha256"
    if (cd "$RESULTS" && "${check[@]}" .copied.sha256); then say "sha256 of copied artefacts verified"; else say "WARNING: sha256 mismatch"; fi
    rm -f "$RESULTS/.copied.sha256"
  else
    say "WARNING: no ARTEFACTS.sha256 (did box.sh finish?)"
  fi
  say "REMEMBER: stop or destroy the GPU instance now; idle hours are billed."
}

follow() {
  say "following box.log (Ctrl-C detaches; the job keeps running; re-attach with --fetch)"
  remote 'while [ -f box.pid ] && kill -0 "$(cat box.pid)" 2>/dev/null; do sleep 20; done & tail -n +1 -f box.log --pid=$!' || true
}

if [ "$FETCH" = 1 ]; then follow; fetch; exit 0; fi

# 1. dataset tarball (content-addressed by its sha256; the box refuses a mismatch)
if [ ! -f "$DATASET.tar" ]; then
  say "writing $DATASET.tar"
  (cd "$REPO" && scripts/ml/.venv/bin/python -c "from scripts.ml.training.common import write_tarball; import sys; print(write_tarball(sys.argv[1]))" "$DATASET")
fi
SHA=$(cut -d' ' -f1 "$DATASET.tar.sha256")
[ "$(shasum -a 256 "$DATASET.tar" | cut -d' ' -f1)" = "$SHA" ] || { echo "tarball does not match its .sha256" >&2; exit 2; }
say "dataset $(basename "$DATASET").tar sha256 $SHA"

# 2. code bundle: the training package and the three repo modules it imports, nothing else
BUNDLE=$(mktemp -t addr-roof-bundle).tar
(cd "$REPO" && tar -cf "$BUNDLE" --exclude .venv --exclude __pycache__ scripts/__init__.py scripts/ml/__init__.py \
  scripts/ml/sam3.py scripts/ml/segmentation.py scripts/ml/tiles.py scripts/ml/training)

if [ "$DRY_RUN" = 1 ]; then
  say "dry run: bundle $BUNDLE ($(wc -c < "$BUNDLE") bytes, $(tar -tf "$BUNDLE" | wc -l | tr -d ' ') entries)"
  say "dry run: would sync code.tar + dataset-v1.tar ($(wc -c < "$DATASET.tar") bytes) to $HOST:$REMOTE_DIR, run box.sh"
  say "dry run: profile=$PROFILE mode=$MODE training hard cap ${CAP} min; results -> $RESULTS"
  rm -f "$BUNDLE"; exit 0
fi

# 3. sync and launch detached (survives a dropped ssh)
LAUNCH="tar -xf code.tar && rm -f box.log && PROFILE=$PROFILE MODE=$MODE CAP_MINUTES=$CAP DATASET_SHA256=$SHA AUTO_SHUTDOWN_MINUTES=${AUTO_SHUTDOWN_MINUTES:-} nohup setsid bash scripts/ml/training/deploy/box.sh > box.log 2>&1 < /dev/null & sleep 2; cat box.log"
if [ "$HOST" = local ]; then
  mkdir -p "$HOME/$REMOTE_DIR"; cp "$BUNDLE" "$HOME/$REMOTE_DIR/code.tar"; cp "$DATASET.tar" "$HOME/$REMOTE_DIR/dataset-v1.tar"
else
  "${SSH[@]}" "mkdir -p $REMOTE_DIR"
  rsync -a --partial --progress -e "ssh ${SSH_OPTS:-}" "$BUNDLE" "$HOST:$REMOTE_DIR/code.tar"
  rsync -a --partial --progress -e "ssh ${SSH_OPTS:-}" "$DATASET.tar" "$HOST:$REMOTE_DIR/dataset-v1.tar"
fi
say "launching profile=$PROFILE mode=$MODE; caps: setup 30 + training ${CAP} + export 15 + evaluation 15 min"
remote "$LAUNCH"
follow
fetch
