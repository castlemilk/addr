#!/bin/sh
# SAM3 teacher-loop round 3 waiter (sam3-mac-round3-v1): one fine-tune at a time, judged as soon as it finishes, one row
# per arm appended to data/reviews/eyes/training/sam3-mac/round3/results.jsonl. Modelled on stage2_resume.sh.
#
#   nohup sh scripts/ml/training/sam3_mac/round3.sh phase1 > data/reviews/eyes/training/sam3-mac/round3/round3.log 2>&1 &
#   sh scripts/ml/training/sam3_mac/round3.sh phase2 | reserve | all
#   DRY_RUN=1 sh scripts/ml/training/sam3_mac/round3.sh phase1     # print the plan and the exact commands; start nothing
#
# Per arm, in order: results-row check (skip when done) -> wait until no finetune_mps process runs -> fold gate (0 train
# images over the 200-annotation cap) -> train detached (nohup, bf16, MPS allocator capped at HIGH 0.7 / LOW 0.5, log in
# the run folder; resumes from the newest trainable-step*.pt; skipped when trainable-final.pt exists) -> loader gate
# ('got loading error' count must be 0) -> judge (reference_eval: dataset-v2 test primary, dataset-v1 test continuity,
# dataset-v2 val for calibration; skipped per block when its summary.json exists) -> calibrate / post-filter when
# r3_calibrate.py, r3_postfilter.py and RULES.json exist -> r3_record appends exactly one row.
# Interruption-safe: re-running the same phase skips finished arms and finished judge blocks, resumes an interrupted
# fine-tune from its last checkpoint, and refuses to start while another round3.sh holds the lock or any finetune_mps
# process runs. A failed gate or a failed train writes a <run>_error row and stops the sequence.
# Wording everywhere: agreement, model estimate, reference. Nothing is promoted; production files are never written.
set -u
cd "$(dirname "$0")/../../../.." || exit 1
V=data/reviews/eyes/training
R=${ROUND3_ROOT:-$V/sam3-mac/round3}
PY=scripts/ml/.venv/bin/python
PY3=scripts/ml/training/.venv-sam3/bin/python
LOG=$R/round3.log
LOCK=$R/round3.lock
PHASE=${1:-phase1}
DRY=${DRY_RUN:-0}
STEPS=3000; LR=8e-5; SEED=1; WARMUP=50; SAVE_EVERY=1500; LOG_EVERY=100; CAP=200
RAW_T=0.2                      # judge collects predictions down to 0.2; r3_postfilter cuts the 0.45 and matched views
FP1=data/elevation/derived/ms-buildings/bbox.geojsonl.gz
FP2=data/elevation/derived/ms-buildings/bbox-inner-melb.geojsonl.gz
CTRL=r3-ctrl-sq-c200-seed1
HERE=scripts/ml/training/sam3_mac
export PYTORCH_ENABLE_MPS_FALLBACK=0
export PYTORCH_MPS_HIGH_WATERMARK_RATIO=${PYTORCH_MPS_HIGH_WATERMARK_RATIO:-0.7}   # cap the MPS allocator; the low mark must stay below it
export PYTORCH_MPS_LOW_WATERMARK_RATIO=${PYTORCH_MPS_LOW_WATERMARK_RATIO:-0.5}
MPSENV="PYTORCH_ENABLE_MPS_FALLBACK=0 PYTORCH_MPS_HIGH_WATERMARK_RATIO=$PYTORCH_MPS_HIGH_WATERMARK_RATIO PYTORCH_MPS_LOW_WATERMARK_RATIO=$PYTORCH_MPS_LOW_WATERMARK_RATIO"

usage() { echo "usage: sh $0 phase1|phase2|reserve|all   (DRY_RUN=1 prints the plan; ROUND3_ROOT overrides $R)" >&2; exit 2; }
log() {
  if [ "$DRY" = 1 ]; then echo "$*"; else echo "$(date -u +%FT%TZ) $*" >> "$LOG"; fi
}
mkdir -p "$R"
[ -x "$PY" ] || { echo "missing $PY" >&2; exit 1; }
[ -x "$PY3" ] || { echo "missing $PY3" >&2; exit 1; }

# --- judge capabilities: the spec's flags (--dataset --split --pair --footprints) land with reference_eval 1a; until
# then the judge runs in legacy mode (dataset-v1 only, base+tuned, plus a separate ctrl+tuned invocation at 0.45)
HELP=$($PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval --help 2>&1)
has_flag() { printf '%s\n' "$HELP" | grep -Eq -- "^ *$1( |$|,)"; }
if has_flag --dataset && has_flag --split && has_flag --pair && has_flag --footprints; then JUDGE_MODE=full; else JUDGE_MODE=legacy; fi
POSTFILTER=0; [ -f "$HERE/r3_postfilter.py" ] && POSTFILTER=1
CALIBRATE=0; [ -f "$HERE/r3_calibrate.py" ] && [ -f "$R/RULES.json" ] && CALIBRATE=1
if [ "$JUDGE_MODE" = full ] && [ "$POSTFILTER" = 1 ]; then JUDGE_T=$RAW_T; else JUDGE_T=0.45; fi

# --- gates ---------------------------------------------------------------------------------------------------------
wait_idle() {   # never start a train or a judge beside another finetune_mps
  if pgrep -f finetune_mps >/dev/null; then log "waiting: finetune_mps running (pid $(pgrep -f finetune_mps | tr '\n' ' '))"; fi
  until ! pgrep -f finetune_mps >/dev/null; do sleep 60; done
}
gate_fold() {   # fold -> 0 when trainable (0 train images over the cap)
  $PY -m scripts.ml.training.sam3_mac.r3_record gate --coco "$1" --cap "$CAP"
}
loader_gate() {   # run -> 0 when the train log holds no 'got loading error'
  [ -f "$R/$1.train.log" ] || return 1
  n=$(grep -c 'got loading error' "$R/$1.train.log" 2>/dev/null); n=${n:-0}
  [ "$n" -eq 0 ]
}
is_done() { $PY -m scripts.ml.training.sam3_mac.r3_record done --root "$R" --run "$1"; }
fail() {   # run reason fold -> error row, stop the sequence
  log "abort $1: $2"
  $PY -m scripts.ml.training.sam3_mac.r3_record error --root "$R" --run "$1" --reason "$2" --coco "$3" >> "$LOG" 2>&1
  exit 1
}

# --- train ---------------------------------------------------------------------------------------------------------
train() {   # run fold
  run=$1; fold=$2; out=$R/$run
  if [ -f "$out/trainable-final.pt" ]; then log "train $run: trainable-final.pt exists, skipping"; return 0; fi
  resume=$($PY -m scripts.ml.training.sam3_mac.r3_record latest --run-dir "$out")
  set -- --coco "$fold" --steps "$STEPS" --dtype bf16 --lr "$LR" --seed "$SEED" --warmup "$WARMUP" --save-every "$SAVE_EVERY" --log-every "$LOG_EVERY" --out "$out"
  [ -n "$resume" ] && set -- "$@" --resume "$resume"
  if [ "$DRY" = 1 ]; then
    echo "  would run: nohup env $MPSENV $PY3 -W ignore -m scripts.ml.training.sam3_mac.finetune_mps $* >> $out.train.log 2>&1 &"
    return 0
  fi
  wait_idle
  t0=$(date +%s)
  [ -f "$out.train.log" ] && echo "==== $(date -u +%FT%TZ) round3.sh restart${resume:+, resume from $resume} ====" >> "$out.train.log"
  log "train $run start${resume:+ (resume from $resume)}"
  # shellcheck disable=SC2086
  nohup env $MPSENV "$PY3" -W ignore -m scripts.ml.training.sam3_mac.finetune_mps "$@" >> "$out.train.log" 2>&1 &
  pid=$!
  echo "$pid" > "$out.train.pid"
  wait "$pid"; status=$?
  wall=$(( $(date +%s) - t0 ))
  echo "$wall" >> "$out.train.wall"
  log "train $run exit=$status wall=${wall}s${resume:+ (resumed from $resume)}"
  return $status
}

# --- judge ---------------------------------------------------------------------------------------------------------
with_ctrl() {   # run -> 0 when the ctrl checkpoint should join this judge
  [ "$1" != "$CTRL" ] && [ -f "$R/$CTRL/trainable-final.pt" ]
}
judge_block() {   # run dataset split name   (full mode: one invocation, base [+ ctrl] + tuned, pairs, both footprint caches)
  run=$1; ds=$2; split=$3; name=$4; out=$R/$run/reference-eval-$name
  if [ -f "$out/summary.json" ]; then log "judge $run $name: summary.json exists, skipping"; return 0; fi
  set -- --dataset "$V/$ds" --split "$split" --arm base
  if with_ctrl "$run"; then set -- "$@" --arm "ctrl=$R/$CTRL/trainable-final.pt"; fi
  set -- "$@" --arm "tuned=$R/$run/trainable-final.pt" --pair tuned:base
  if with_ctrl "$run"; then set -- "$@" --pair tuned:ctrl --pair ctrl:base; fi
  set -- "$@" --dtype fp32 --threshold "$JUDGE_T"
  for fp in "$FP1" "$FP2"; do [ -f "$fp" ] && set -- "$@" --footprints "$fp"; done
  set -- "$@" --out "$out"
  if [ "$DRY" = 1 ]; then echo "  would judge: $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval $*"; return 0; fi
  wait_idle
  $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval "$@" >> "$R/$run.eval.log" 2>&1
  log "judge $run $name exit=$? threshold=$JUDGE_T"
}
judge_legacy() {   # run   (today's reference_eval: dataset-v1 test only, two arms per invocation, default 0.45)
  run=$1; out=$R/$run/reference-eval-v1
  if [ -f "$out/summary.json" ]; then log "judge $run v1 (legacy): summary.json exists, skipping"; else
    set -- --arm base --arm "tuned=$R/$run/trainable-final.pt" --dtype fp32 --out "$out"
    if [ "$DRY" = 1 ]; then echo "  would judge: $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval $*"; else
      wait_idle
      $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval "$@" >> "$R/$run.eval.log" 2>&1
      log "judge $run v1 (legacy, base+tuned) exit=$?"
    fi
  fi
  if with_ctrl "$run"; then
    out=$R/$run/reference-eval-v1-vs-ctrl
    if [ -f "$out/summary.json" ]; then log "judge $run v1-vs-ctrl (legacy): summary.json exists, skipping"; else
      set -- --arm "ctrl=$R/$CTRL/trainable-final.pt" --arm "tuned=$R/$run/trainable-final.pt" --dtype fp32 --out "$out"
      if [ "$DRY" = 1 ]; then echo "  would judge: $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval $*"; else
        wait_idle
        $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval "$@" >> "$R/$run.eval.log" 2>&1
        log "judge $run v1-vs-ctrl (legacy, ctrl+tuned) exit=$?"
      fi
    fi
  fi
  [ "$DRY" = 1 ] && echo "  note: legacy judge; dataset-v2 blocks and calibration recorded as not_run"
  return 0
}
judge_all() {   # run
  if [ "$JUDGE_MODE" = legacy ]; then judge_legacy "$1"; return 0; fi
  judge_block "$1" dataset-v1 test v1
  judge_block "$1" dataset-v2 test v2
  judge_block "$1" dataset-v2 val v2val
}
calibrate() {   # run -> calibration.json from the val block (val only; never a human reference)
  run=$1
  [ "$JUDGE_MODE" = full ] || return 0
  if [ "$CALIBRATE" != 1 ]; then log "calibrate $run: skipped ($HERE/r3_calibrate.py or $R/RULES.json absent)"; return 0; fi
  [ -f "$R/$run/calibration.json" ] && return 0
  if [ "$DRY" = 1 ]; then echo "  would calibrate: $PY3 -m scripts.ml.training.sam3_mac.r3_calibrate --rule $R/RULES.json --val $R/$run/reference-eval-v2val/predictions.jsonl --out $R/$run/calibration.json"; return 0; fi
  [ -f "$R/$run/reference-eval-v2val/predictions.jsonl" ] || { log "calibrate $run: no val predictions.jsonl"; return 0; }
  $PY3 -m scripts.ml.training.sam3_mac.r3_calibrate --rule "$R/RULES.json" --val "$R/$run/reference-eval-v2val/predictions.jsonl" --out "$R/$run/calibration.json" >> "$R/$run.eval.log" 2>&1
  log "calibrate $run exit=$?"
}
postfilter() {   # run block threshold suffix -> reference-eval-<block><suffix>/summary.json from the raw rows
  run=$1; block=$2; t=$3; suffix=$4
  src=$R/$run/reference-eval-$block/predictions.jsonl; out=$R/$run/reference-eval-$block$suffix
  [ -f "$out/summary.json" ] && return 0
  if [ "$DRY" = 1 ]; then echo "  would post-filter: $PY3 -m scripts.ml.training.sam3_mac.r3_postfilter --predictions $src --threshold $t --out $out"; return 0; fi
  [ -f "$src" ] || { log "postfilter $run $block$suffix: no predictions.jsonl"; return 0; }
  $PY3 -m scripts.ml.training.sam3_mac.r3_postfilter --predictions "$src" --threshold "$t" --out "$out" >> "$R/$run.eval.log" 2>&1
  log "postfilter $run $block$suffix t=$t exit=$?"
}
views() {   # run -> 0.45 view of every block, matched view at t* when calibration produced one
  run=$1
  [ "$JUDGE_MODE" = full ] && [ "$POSTFILTER" = 1 ] || return 0
  for block in v1 v2 v2val; do postfilter "$run" "$block" 0.45 -t045; done
  tstar=''
  [ -f "$R/$run/calibration.json" ] && tstar=$($PY -c 'import json,sys; v=json.load(open(sys.argv[1])).get("t_star"); print("" if v is None else v)' "$R/$run/calibration.json")
  if [ -n "$tstar" ]; then for block in v1 v2; do postfilter "$run" "$block" "$tstar" -matched; done
  else log "views $run: no t_star, matched views not built"; fi
}
record() {   # run fold floor policy label_target rule phase
  set -- --root "$R" --run "$1" --coco "$2" --floor-px "$3" --policy "$4" --label-target "$5" --arm-rule "$6" --cap "$CAP" --steps "$STEPS" --lr "$LR" --seed "$SEED" --save-every "$SAVE_EVERY" --judge-mode "$JUDGE_MODE" --ctrl "$CTRL" --extra "{\"phase\": \"$7\", \"judge_threshold_raw\": $JUDGE_T}"
  if [ "$DRY" = 1 ]; then echo "  would record: $PY -m scripts.ml.training.sam3_mac.r3_record record $*"; return 0; fi
  $PY -m scripts.ml.training.sam3_mac.r3_record record "$@" >> "$LOG" 2>&1
  log "record $1 exit=$?"
}

# --- per-arm driver ------------------------------------------------------------------------------------------------
arm() {   # run fold floor_px policy label_target "rule" phase
  run=$1; fold=$R/$2; floor=$3; policy=$4; target=$5; rule=$6; phase=$7
  if is_done "$run"; then log "skip $run: results row exists"; return 0; fi
  if [ "$DRY" = 1 ]; then
    echo "arm $run  fold=$fold  floor_px=$floor policy=$policy label_target=$target"
    echo "  fold present: $([ -f "$fold/train/_annotations.coco.json" ] && echo yes || echo no)   final.pt: $([ -f "$R/$run/trainable-final.pt" ] && echo yes || echo no)   resume candidate: $($PY -m scripts.ml.training.sam3_mac.r3_record latest --run-dir "$R/$run")"
    echo "  fold gate: $PY -m scripts.ml.training.sam3_mac.r3_record gate --coco $fold --cap $CAP"
    train "$run" "$fold"; judge_all "$run"; calibrate "$run"; views "$run"; record "$run" "$fold" "$floor" "$policy" "$target" "$rule" "$phase"
    return 0
  fi
  log "== arm $run fold=$fold floor_px=$floor policy=$policy label_target=$target judge_mode=$JUDGE_MODE judge_t=$JUDGE_T"
  wait_idle
  gate_fold "$fold" >> "$LOG" 2>&1 || fail "$run" fold_gate_failed "$fold"
  train "$run" "$fold" || fail "$run" "train_exit_$?" "$fold"
  loader_gate "$run" || fail "$run" loader_errors "$fold"
  judge_all "$run"
  calibrate "$run"
  views "$run"
  record "$run" "$fold" "$floor" "$policy" "$target" "$rule" "$phase"
}
preflight() {   # fold... -> every fold of the phase must exist before anything starts (no error rows for a missing fold)
  missing=0
  for f in "$@"; do [ -f "$R/$f/train/_annotations.coco.json" ] || { echo "fold missing: $R/$f (build it with prepare_coco first)" >&2; missing=1; }; done
  [ "$DRY" = 1 ] && return 0
  [ "$missing" = 0 ] || exit 1
}
cumulative_wall() {   # run... -> seconds summed over the runs' .train.wall files
  cat "$@" 2>/dev/null | awk '{s+=$1} END {print s+0}'
}

# --- arms (order pre-registered in the round-3 spec) --------------------------------------------------------------
phase1() {
  preflight coco-r3-sq-c200-v1 coco-r3-sq-f12-ledger-c200-v1 coco-r3-sq-c200-mixv2-v1
  arm r3-ctrl-sq-c200-seed1 coco-r3-sq-c200-v1 16 none squared_gated \
      "H0 control: stage-2 squared teacher labels, per-image cap 200 only (recovers the 60 windows stage 2 dropped)" phase1
  arm r3-floor12-sq-ledger-c200-seed1 coco-r3-sq-f12-ledger-c200-v1 216 ledger-v1 squared_gated \
      "H1 small-label prior: teacher instances under 12 m2 (216 px at 0.236 m/px) are background; 2025 set from its ledger-v1 export (adds 1 instance beyond the floor)" phase1
  arm r3-mixv2-sq-c200-seed1 coco-r3-sq-c200-mixv2-v1 16 none "squared_gated + machine_pseudo (dataset-v2-trainclean)" \
      "H2 density / domain prior: control fold plus dataset-v2-trainclean once (student domain, production SAM3 pseudo labels)" phase1
}
phase2() {
  preflight coco-r3-sq-merge-c200-v1
  arm r3-merge-sq-c200-seed1 coco-r3-sq-merge-c200-v1 16 none whole_squared_gated \
      "H3 instance granularity: both Christchurch sets from whole_squared_gated exports (teacher pieces merged per LINZ outline)" phase2
  total=$(cumulative_wall "$R/r3-ctrl-sq-c200-seed1.train.wall" "$R/r3-floor12-sq-ledger-c200-seed1.train.wall" "$R/r3-mixv2-sq-c200-seed1.train.wall" "$R/r3-merge-sq-c200-seed1.train.wall")
  if [ "$total" -gt 0 ] && [ "$total" -lt 28800 ]; then
    log "reserve decision: cumulative train wall of runs 1-4 = ${total}s (< 8 h): build the outline-v1 exports and fold, then run 'round3.sh reserve'"
  else
    log "reserve decision: cumulative train wall of runs 1-4 = ${total}s (>= 8 h or unknown): queue r3-outline-f12 as the first round-4 arm"
  fi
}
reserve() {
  preflight coco-r3-sq-outline-f12-c200-v1
  arm r3-outline-f12-sq-c200-seed1 coco-r3-sq-outline-f12-c200-v1 216 outline-v1 squared_gated \
      "H4 reserve: floor12 fold with both Christchurch sets from outline-v1 policy exports (unbacked small instances without an outline are background)" reserve
}

# --- main ----------------------------------------------------------------------------------------------------------
case "$PHASE" in phase1|phase2|reserve|all) ;; *) usage ;; esac
if [ "$DRY" = 1 ]; then
  echo "round3.sh DRY RUN: phase=$PHASE root=$R judge_mode=$JUDGE_MODE judge_t=$JUDGE_T postfilter=$POSTFILTER calibrate=$CALIBRATE (no run started)"
else
  if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "round3.sh already running (pid $(cat "$LOCK")); exiting" >&2; exit 2
  fi
  echo $$ > "$LOCK"
  trap 'rm -f "$LOCK"' EXIT INT TERM HUP
  log "round3.sh $PHASE start pid=$$ judge_mode=$JUDGE_MODE judge_t=$JUDGE_T postfilter=$POSTFILTER calibrate=$CALIBRATE mps_high=$PYTORCH_MPS_HIGH_WATERMARK_RATIO mps_low=$PYTORCH_MPS_LOW_WATERMARK_RATIO"
fi
case "$PHASE" in
  phase1) phase1 ;;
  phase2) phase2 ;;
  reserve) reserve ;;
  all) phase1; phase2 ;;
esac
log "round3.sh $PHASE done"
