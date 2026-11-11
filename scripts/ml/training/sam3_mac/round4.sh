#!/bin/sh
# SAM3 teacher-loop round 4 waiter (sam3-mac-round4-v1): copy of round3.sh with the round-4 arms. One fine-tune at a time,
# judged as soon as it finishes, one row per arm appended to data/reviews/eyes/training/sam3-mac/round4/results.jsonl.
#
#   nohup sh scripts/ml/training/sam3_mac/round4.sh all > data/reviews/eyes/training/sam3-mac/round4/round4.log 2>&1 &
#   sh scripts/ml/training/sam3_mac/round4.sh phase1 | phase2 | phase3
#   DRY_RUN=1 sh scripts/ml/training/sam3_mac/round4.sh phase1     # print the plan and the exact commands; start nothing
#
# Arms (pre-registered: .run/teacher-round3-spec-2026-09-29.md round-4 plan, final spec approved 2026-09-30), one fold
# coco-r4-combo-f12-ledger-mixv2-c200-v1, the round-3 train line unless the arm's one rule is a training flag:
#   phase1  r4-combo-seed1        round-3 line                       refs mixv2, floor12    verdict against ctrl (RULES)
#   phase2  r4-combo-posw3-seed1  + --pos-weight 3                   ref combo              verdict against combo, verdict_vs_ctrl kept
#           r4-combo-o2m0-seed1   + --o2m-weight 0                   ref combo              (pre-registered fallback --o2m-weight 1 as
#                                                                                           r4-combo-o2m1-seed1 only when the o2m0 run fails
#                                                                                           the training gate: a skipped batch or a non-finite
#                                                                                           loss inside the first 300 steps; never a test peek)
#   phase3  r4-<chosen>-seed2     the chosen arm's flags + --seed 2  ref twin [+ combo]     verdict against twin; chosen by
#                                 'r3_record select-replicate' over the three seed-1 rows (default r4-combo-seed1)
# Per arm: results-row check (skip when done) -> wait until no finetune_mps runs -> fold gates (0 train images over the
# 200-annotation cap, 0 zero-annotation images) -> reference checkpoints present -> train detached (nohup, bf16, MPS
# allocator HIGH 0.7 / LOW 0.5, log in the run folder; resumes from the newest trainable-step*.pt; skipped when
# trainable-final.pt exists) -> loader gate ('got loading error' count 0) -> judge at 0.2 (dataset-v2 test primary, dataset-v1
# test continuity, dataset-v2 val for calibration; base + ctrl + refs + tuned in one invocation, both footprint caches) ->
# r3_calibrate with RULES.json (every round-4 run trains on trainclean, so RULES lists every run under val_adjacent_to_train and
# t* is chosen on the 13 non-Mooroolbark val images) -> r3_postfilter 0.45 view of every block and matched view at per-arm t*
# (base 0.45, every other arm its own val t*) -> r3_record appends exactly one row (paired vs base, ctrl and every ref; verdict
# against the arm's pairing arm; verdict_vs_ctrl kept when that is not ctrl; objective from run.json).
# Interruption-safe as round3.sh: re-running a phase skips recorded arms and finished judge blocks, resumes an interrupted
# fine-tune from its last checkpoint, and refuses to start while another round4.sh holds the lock (exit 2) or any finetune_mps
# process runs. A failed gate or a failed train writes a <run>_error row and stops the sequence. Never 'next build'; never
# touches the web dev server on :3008.
# Wording everywhere: agreement, model estimate, reference. Nothing is promoted; production files are never written.
set -u
cd "$(dirname "$0")/../../../.." || exit 1
V=data/reviews/eyes/training
R3=${ROUND3_ROOT:-$V/sam3-mac/round3}
R=${ROUND4_ROOT:-$V/sam3-mac/round4}
PY=scripts/ml/.venv/bin/python
PY3=scripts/ml/training/.venv-sam3/bin/python
LOG=$R/round4.log
LOCK=$R/round4.lock
RULES=$R/RULES.json
PHASE=${1:-phase1}
DRY=${DRY_RUN:-0}
STEPS=3000; LR=8e-5; WARMUP=50; SAVE_EVERY=1500; LOG_EVERY=100; CAP=200
RAW_T=0.2                      # judge collects predictions down to 0.2; r3_postfilter cuts the 0.45 and matched views
EARLY_GATE_STEPS=300           # training gate window for the o2m0 fallback (training diagnostic only)
FP1=data/elevation/derived/ms-buildings/bbox.geojsonl.gz
FP2=data/elevation/derived/ms-buildings/bbox-inner-melb.geojsonl.gz
CTRL=r3-ctrl-sq-c200-seed1
CTRL_PT=$R3/r3-ctrl-sq-c200-seed1/trainable-final.pt                  # round-3 control joins every judge (continuity pairing)
MIXV2_PT=$R3/r3-mixv2-sq-c200-seed1/trainable-final.pt                # one-rule read for run 1 (adds floor12 + ledger)
FLOOR12_PT=$R3/r3-floor12-sq-ledger-c200-seed1/trainable-final.pt     # one-rule read for run 1 (adds mix)
FOLD=coco-r4-combo-f12-ledger-mixv2-c200-v1
COMBO=r4-combo-seed1; POSW3=r4-combo-posw3-seed1; O2M0=r4-combo-o2m0-seed1; O2M1=r4-combo-o2m1-seed1
LABEL="squared_gated + machine_pseudo (dataset-v2-trainclean)"
FLOOR_PX=216; POLICY=ledger-v1
FLOOR_FOR='{"distill-v1-squared": 216, "distill-linz-chc-2025-0075-squared-ledger": 216, "distill-linz-chc-2020-0075-squared": 216, "distill-act-2023-urban-75mm-squared": 216, "dataset-v2-trainclean": 16}'
HERE=scripts/ml/training/sam3_mac
export PYTORCH_ENABLE_MPS_FALLBACK=0
export PYTORCH_MPS_HIGH_WATERMARK_RATIO=${PYTORCH_MPS_HIGH_WATERMARK_RATIO:-0.7}   # cap the MPS allocator; the low mark must stay below it
export PYTORCH_MPS_LOW_WATERMARK_RATIO=${PYTORCH_MPS_LOW_WATERMARK_RATIO:-0.5}
MPSENV="PYTORCH_ENABLE_MPS_FALLBACK=0 PYTORCH_MPS_HIGH_WATERMARK_RATIO=$PYTORCH_MPS_HIGH_WATERMARK_RATIO PYTORCH_MPS_LOW_WATERMARK_RATIO=$PYTORCH_MPS_LOW_WATERMARK_RATIO"
# per-arm settings (sh has no arrays): each phase sets these before arm(); arm() and its helpers read them
SEED=1            # --seed (run 4 uses 2)
TRAIN_EXTRA=''    # train flags appended after --out (the arm's one rule when it is a training flag)
REFS=''           # "name=path ..." reference arms beside base and ctrl -> --arm name=path --pair tuned:name, --ref name
AGAINST=ctrl      # r3_record --against (ctrl | combo | twin)
ROW_EXTRA=''      # extra JSON members merged into the row after phase / judge_threshold_raw / train_gate
FALLBACK_RUN=''   # run name of the pre-registered fallback arm when the training gate fails inside the first $EARLY_GATE_STEPS steps
FALLBACK_EXTRA='' # its train flags

usage() { echo "usage: sh $0 phase1|phase2|phase3|all   (DRY_RUN=1 prints the plan; ROUND4_ROOT overrides $R; ROUND3_ROOT overrides $R3)" >&2; exit 2; }
log() {
  if [ "$DRY" = 1 ]; then echo "$*"; else echo "$(date -u +%FT%TZ) $*" >> "$LOG"; fi
}
mkdir -p "$R"
[ -x "$PY" ] || { echo "missing $PY" >&2; exit 1; }
[ -x "$PY3" ] || { echo "missing $PY3" >&2; exit 1; }

# --- capabilities: round 4 has no legacy path; the full judge, the loss flags, calibrate / post-filter and RULES.json are required
HELP=$($PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval --help 2>&1)
has_flag() { printf '%s\n' "$HELP" | grep -Eq -- "^ *$1( |$|,)"; }
for f in --dataset --split --pair --footprints; do has_flag "$f" || { echo "reference_eval lacks $f: round 4 needs the full judge" >&2; exit 1; }; done
HELP=$($PY3 -W ignore -m scripts.ml.training.sam3_mac.finetune_mps --help 2>&1)
for f in --pos-weight --o2m-weight --presence-weight; do has_flag "$f" || { echo "finetune_mps lacks $f: land the round-4 loss flags (STEP 1a) first" >&2; exit 1; }; done
for f in r3_calibrate.py r3_postfilter.py r3_record.py; do [ -f "$HERE/$f" ] || { echo "missing $HERE/$f" >&2; exit 1; }; done
if [ ! -f "$RULES" ]; then
  if [ "$DRY" = 1 ]; then echo "note: $RULES absent; write the round-4 RULES.json before run 1's fold gate (STEP 1e)" >&2
  else echo "missing $RULES: write the round-4 RULES.json before any gate or run (STEP 1e); its sha goes into every row" >&2; exit 1; fi
fi
JUDGE_MODE=full
JUDGE_T=$RAW_T

# --- gates ---------------------------------------------------------------------------------------------------------
wait_idle() {   # never start a train or a judge beside another finetune_mps
  if pgrep -f finetune_mps >/dev/null; then log "waiting: finetune_mps running (pid $(pgrep -f finetune_mps | tr '\n' ' '))"; fi
  until ! pgrep -f finetune_mps >/dev/null; do sleep 60; done
}
gate_fold() {   # fold -> 0 when trainable: 0 train images over the cap AND 0 zero-annotation images (presence target 1 everywhere)
  $PY -m scripts.ml.training.sam3_mac.r3_record gate --coco "$1" --cap "$CAP" --no-zero-annotation-images
}
loader_gate() {   # run -> 0 when the train log holds no 'got loading error'
  [ -f "$R/$1.train.log" ] || return 1
  n=$(grep -c 'got loading error' "$R/$1.train.log" 2>/dev/null); n=${n:-0}
  [ "$n" -eq 0 ]
}
train_gate() {   # run -> JSON on stdout; 0 when the first $EARLY_GATE_STEPS logged steps hold no skipped batch and no non-finite loss
  $PY - "$R/$1/steps.jsonl" "$EARLY_GATE_STEPS" <<'PYEOF'
import json, math, sys
path, limit = sys.argv[1], int(sys.argv[2])
skipped, nonfinite, seen = 0, 0, 0
try:
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        step = int(row.get('step', 0) or 0)
        if step > limit:
            break
        seen = max(seen, step)
        if 'skipped_batch' in row:
            skipped += 1
        elif 'loss' in row and not math.isfinite(float(row['loss'])):
            nonfinite += 1
except FileNotFoundError:
    print(json.dumps({'ok': False, 'reason': 'steps.jsonl missing', 'limit': limit}))
    sys.exit(1)
ok = skipped == 0 and nonfinite == 0
print(json.dumps({'ok': ok, 'steps_seen': seen, 'limit': limit, 'skipped_batches': skipped, 'non_finite_losses': nonfinite,
                  'note': 'training diagnostic (first steps of steps.jsonl); never a test read'}))
sys.exit(0 if ok else 1)
PYEOF
}
is_done() { $PY -m scripts.ml.training.sam3_mac.r3_record done --root "$R" --run "$1"; }
fail() {   # run reason fold -> error row, stop the sequence
  log "abort $1: $2"
  $PY -m scripts.ml.training.sam3_mac.r3_record error --root "$R" --run "$1" --reason "$2" --coco "$3" >> "$LOG" 2>&1
  exit 1
}
require_files() {   # path... -> 0 when every file exists (DRY: report only)
  missing=0
  for p in "$@"; do [ -f "$p" ] || { echo "missing checkpoint: $p" >&2; missing=1; }; done
  [ "$DRY" = 1 ] && return 0
  return $missing
}
o2m_arm() {   # the H7 arm actually run: the fallback name once its folder exists (o2m0 failed the training gate), else o2m0
  if [ -d "$R/$O2M1" ]; then echo "$O2M1"; else echo "$O2M0"; fi
}

# --- train ---------------------------------------------------------------------------------------------------------
watch_early() {   # pid run -> 0 once $EARLY_GATE_STEPS steps are logged or the process exits; 3 when the gate fails inside the window (process stopped)
  pid=$1; run=$2
  while kill -0 "$pid" 2>/dev/null; do
    if [ -f "$R/$run/steps.jsonl" ]; then
      if ! verdict=$(train_gate "$run" 2>/dev/null); then
        log "train $run: training gate failed inside the first $EARLY_GATE_STEPS steps: $verdict; stopping pid $pid for the pre-registered fallback"
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
        return 3
      fi
      last=$(tail -1 "$R/$run/steps.jsonl" 2>/dev/null | $PY -c 'import json,sys; l=sys.stdin.read().strip(); print(int(json.loads(l).get("step", 0) or 0) if l else 0)' 2>/dev/null)
      [ "${last:-0}" -ge "$EARLY_GATE_STEPS" ] && return 0
    fi
    sleep 30
  done
  return 0
}
train() {   # run fold   (reads SEED, TRAIN_EXTRA, FALLBACK_RUN)
  run=$1; fold=$2; out=$R/$run
  if [ -f "$out/trainable-final.pt" ]; then log "train $run: trainable-final.pt exists, skipping"; return 0; fi
  resume=$($PY -m scripts.ml.training.sam3_mac.r3_record latest --run-dir "$out")
  set -- --coco "$fold" --steps "$STEPS" --dtype bf16 --lr "$LR" --seed "$SEED" --warmup "$WARMUP" --save-every "$SAVE_EVERY" --log-every "$LOG_EVERY" --out "$out"
  # shellcheck disable=SC2086
  [ -n "$TRAIN_EXTRA" ] && set -- "$@" $TRAIN_EXTRA
  [ -n "$resume" ] && set -- "$@" --resume "$resume"
  if [ "$DRY" = 1 ]; then
    echo "  would run: nohup env $MPSENV $PY3 -W ignore -m scripts.ml.training.sam3_mac.finetune_mps $* >> $out.train.log 2>&1 &"
    return 0
  fi
  wait_idle
  t0=$(date +%s)
  [ -f "$out.train.log" ] && echo "==== $(date -u +%FT%TZ) round4.sh restart${resume:+, resume from $resume} ====" >> "$out.train.log"
  log "train $run start${resume:+ (resume from $resume)}${TRAIN_EXTRA:+ flags: $TRAIN_EXTRA}"
  # shellcheck disable=SC2086
  nohup env $MPSENV "$PY3" -W ignore -m scripts.ml.training.sam3_mac.finetune_mps "$@" >> "$out.train.log" 2>&1 &
  pid=$!
  echo "$pid" > "$out.train.pid"
  status=0
  if [ -n "$FALLBACK_RUN" ]; then watch_early "$pid" "$run"; status=$?; fi
  if [ "$status" != 3 ]; then wait "$pid"; status=$?; fi
  wall=$(( $(date +%s) - t0 ))
  echo "$wall" >> "$out.train.wall"
  log "train $run exit=$status wall=${wall}s${resume:+ (resumed from $resume)}"
  return $status
}

# --- judge ---------------------------------------------------------------------------------------------------------
judge_block() {   # run dataset split name   (one invocation: base + ctrl + every REF + tuned; pairs tuned:base tuned:ctrl tuned:<ref> ctrl:base [combo:ctrl])
  run=$1; ds=$2; split=$3; name=$4; out=$R/$run/reference-eval-$name
  if [ -f "$out/summary.json" ]; then log "judge $run $name: summary.json exists, skipping"; return 0; fi
  set -- --dataset "$V/$ds" --split "$split" --arm base --arm "ctrl=$CTRL_PT"
  for ref in $REFS; do set -- "$@" --arm "$ref"; done
  set -- "$@" --arm "tuned=$R/$run/trainable-final.pt" --pair tuned:base --pair tuned:ctrl
  for ref in $REFS; do set -- "$@" --pair "tuned:${ref%%=*}"; done
  set -- "$@" --pair ctrl:base
  case " $REFS" in *" combo="*) set -- "$@" --pair combo:ctrl ;; esac
  set -- "$@" --dtype fp32 --threshold "$JUDGE_T"
  for fp in "$FP1" "$FP2"; do [ -f "$fp" ] && set -- "$@" --footprints "$fp"; done
  set -- "$@" --out "$out"
  if [ "$DRY" = 1 ]; then echo "  would judge: $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval $*"; return 0; fi
  wait_idle
  $PY3 -W ignore -m scripts.ml.training.sam3_mac.reference_eval "$@" >> "$R/$run.eval.log" 2>&1
  log "judge $run $name exit=$? threshold=$JUDGE_T"
}
judge_all() {   # run
  judge_block "$1" dataset-v1 test v1
  judge_block "$1" dataset-v2 test v2
  judge_block "$1" dataset-v2 val v2val
}
calibrate() {   # run -> calibration.json from the val block (val only; never a human reference; subset t* per RULES val_adjacent_to_train)
  run=$1
  [ -f "$R/$run/calibration.json" ] && return 0
  if [ "$DRY" = 1 ]; then echo "  would calibrate: $PY3 -m scripts.ml.training.sam3_mac.r3_calibrate --rule $RULES --val $R/$run/reference-eval-v2val/predictions.jsonl --out $R/$run/calibration.json"; return 0; fi
  [ -f "$R/$run/reference-eval-v2val/predictions.jsonl" ] || { log "calibrate $run: no val predictions.jsonl"; return 0; }
  $PY3 -m scripts.ml.training.sam3_mac.r3_calibrate --rule "$RULES" --val "$R/$run/reference-eval-v2val/predictions.jsonl" --out "$R/$run/calibration.json" >> "$R/$run.eval.log" 2>&1
  log "calibrate $run exit=$?"
}
postfilter() {   # run block threshold suffix -> reference-eval-<block><suffix>/summary.json from the raw rows (a -matched out auto-reads calibration.json: per-arm cuts)
  run=$1; block=$2; t=$3; suffix=$4
  src=$R/$run/reference-eval-$block/predictions.jsonl; out=$R/$run/reference-eval-$block$suffix
  [ -f "$out/summary.json" ] && return 0
  if [ "$DRY" = 1 ]; then echo "  would post-filter: $PY3 -m scripts.ml.training.sam3_mac.r3_postfilter --predictions $src --threshold $t --out $out"; return 0; fi
  [ -f "$src" ] || { log "postfilter $run $block$suffix: no predictions.jsonl"; return 0; }
  $PY3 -m scripts.ml.training.sam3_mac.r3_postfilter --predictions "$src" --threshold "$t" --out "$out" >> "$R/$run.eval.log" 2>&1
  log "postfilter $run $block$suffix t=$t exit=$?"
}
views() {   # run -> 0.45 view of every block, matched view at per-arm t* (base 0.45, others their own val t*) when calibration produced one
  run=$1
  for block in v1 v2 v2val; do postfilter "$run" "$block" 0.45 -t045; done
  tstar=''
  [ -f "$R/$run/calibration.json" ] && tstar=$($PY -c 'import json,sys; v=json.load(open(sys.argv[1])).get("t_star"); print("" if v is None else v)' "$R/$run/calibration.json")
  if [ -n "$tstar" ]; then for block in v1 v2; do postfilter "$run" "$block" "$tstar" -matched; done
  elif [ "$DRY" = 1 ]; then echo "  would post-filter: v1 / v2 -matched at t* from $R/$run/calibration.json (per-arm cuts)"
  else log "views $run: no t_star, matched views not built"; fi
}
record() {   # run fold floor policy label_target rule phase   (reads REFS, AGAINST, ROW_EXTRA, SEED)
  run=$1
  tg=null
  [ "$DRY" = 1 ] || { tg=$(train_gate "$run" 2>/dev/null); [ -n "$tg" ] || tg=null; }
  extra="{\"phase\": \"$7\", \"judge_threshold_raw\": $JUDGE_T, \"train_gate_first_${EARLY_GATE_STEPS}\": $tg${ROW_EXTRA:+, $ROW_EXTRA}}"
  set -- --root "$R" --run "$1" --coco "$2" --floor-px "$3" --policy "$4" --label-target "$5" --arm-rule "$6" --cap "$CAP" --steps "$STEPS" --lr "$LR" --seed "$SEED" --save-every "$SAVE_EVERY" --judge-mode "$JUDGE_MODE" --ctrl "$CTRL" --against "$AGAINST" --extra "$extra"
  for ref in $REFS; do set -- "$@" --ref "${ref%%=*}"; done
  if [ "$DRY" = 1 ]; then echo "  would record: $PY -m scripts.ml.training.sam3_mac.r3_record record $*"; return 0; fi
  $PY -m scripts.ml.training.sam3_mac.r3_record record "$@" >> "$LOG" 2>&1
  log "record $run exit=$?"
}

# --- per-arm driver ------------------------------------------------------------------------------------------------
arm() {   # run fold_name floor_px policy label_target "rule" phase   (reads SEED TRAIN_EXTRA REFS AGAINST ROW_EXTRA FALLBACK_RUN FALLBACK_EXTRA)
  run=$1; fold=$R/$2; floor=$3; policy=$4; target=$5; rule=$6; phase=$7
  if is_done "$run"; then log "skip $run: results row exists"; return 0; fi
  if [ "$DRY" = 1 ]; then
    echo "arm $run  fold=$fold  seed=$SEED train_extra='${TRAIN_EXTRA:-none}' refs='${REFS:-none}' against=$AGAINST floor_px=$floor policy=$policy label_target=$target"
    echo "  fold present: $([ -f "$fold/train/_annotations.coco.json" ] && echo yes || echo no)   final.pt: $([ -f "$R/$run/trainable-final.pt" ] && echo yes || echo no)   resume candidate: $($PY -m scripts.ml.training.sam3_mac.r3_record latest --run-dir "$R/$run")"
    echo "  fold gates: $PY -m scripts.ml.training.sam3_mac.r3_record gate --coco $fold --cap $CAP --no-zero-annotation-images"
    echo "  reference checkpoints: ctrl=$CTRL_PT ${REFS:-}"
    [ -n "$FALLBACK_RUN" ] && echo "  training gate: the first $EARLY_GATE_STEPS steps must log no skipped batch and no non-finite loss; else stop and run $FALLBACK_RUN with '$FALLBACK_EXTRA'"
    train "$run" "$fold"; judge_all "$run"; calibrate "$run"; views "$run"; record "$run" "$fold" "$floor" "$policy" "$target" "$rule" "$phase"
    return 0
  fi
  log "== arm $run fold=$fold seed=$SEED train_extra='${TRAIN_EXTRA:-none}' refs='${REFS:-none}' against=$AGAINST floor_px=$floor policy=$policy label_target=$target judge_t=$JUDGE_T rules=$RULES"
  wait_idle
  gate_fold "$fold" >> "$LOG" 2>&1 || fail "$run" fold_gate_failed "$fold"
  [ -f "$CTRL_PT" ] || fail "$run" "ref_missing:ctrl" "$fold"
  for ref in $REFS; do [ -f "${ref#*=}" ] || fail "$run" "ref_missing:${ref%%=*}" "$fold"; done
  train "$run" "$fold"; st=$?
  if [ "$st" = 3 ] && [ -n "$FALLBACK_RUN" ]; then
    tg=$(train_gate "$run" 2>/dev/null); [ -n "$tg" ] || tg=null
    log "abort $run: train_gate_failed (first $EARLY_GATE_STEPS steps); running the pre-registered fallback $FALLBACK_RUN with '$FALLBACK_EXTRA'"
    $PY -m scripts.ml.training.sam3_mac.r3_record error --root "$R" --run "$run" --reason "train_gate_failed:first_${EARLY_GATE_STEPS}_steps" --coco "$fold" \
        --extra "{\"phase\": \"$phase\", \"fallback\": \"$FALLBACK_RUN\", \"train_gate_first_${EARLY_GATE_STEPS}\": $tg}" >> "$LOG" 2>&1
    fb_run=$FALLBACK_RUN; fb_extra=$FALLBACK_EXTRA
    FALLBACK_RUN=''; FALLBACK_EXTRA=''; TRAIN_EXTRA=$fb_extra
    ROW_EXTRA="\"arm_flag\": \"$fb_extra\", \"fallback_for\": \"$run\""
    arm "$fb_run" "$2" "$floor" "$policy" "$target" "$rule (fallback $fb_extra: the $run run failed the training gate inside the first $EARLY_GATE_STEPS steps)" "$phase"
    return $?
  fi
  [ "$st" = 0 ] || fail "$run" "train_exit_$st" "$fold"
  loader_gate "$run" || fail "$run" loader_errors "$fold"
  judge_all "$run"
  calibrate "$run"
  views "$run"
  record "$run" "$fold" "$floor" "$policy" "$target" "$rule" "$phase"
}
preflight() {   # fold... -> every fold of the phase must exist before anything starts (no error rows for a missing fold)
  missing=0
  for f in "$@"; do [ -f "$R/$f/train/_annotations.coco.json" ] || { echo "fold missing: $R/$f (build it with prepare_coco first; STEP 2)" >&2; missing=1; }; done
  [ "$DRY" = 1 ] && return 0
  [ "$missing" = 0 ] || exit 1
}
wait_for_rows() {   # run... -> returns when every run has a complete results row (DRY: print only)
  if [ "$DRY" = 1 ]; then echo "would wait until results rows exist for: $*"; return 0; fi
  for r in "$@"; do
    is_done "$r" || log "waiting for results row: $r"
    until is_done "$r"; do sleep 60; done
  done
}
cumulative_wall() {   # run... -> seconds summed over the runs' .train.wall files
  cat "$@" 2>/dev/null | awk '{s+=$1} END {print s+0}'
}
reset_arm() { SEED=1; TRAIN_EXTRA=''; REFS=''; AGAINST=ctrl; ROW_EXTRA=''; FALLBACK_RUN=''; FALLBACK_EXTRA=''; }

# --- arms (order pre-registered in the round-4 final spec) --------------------------------------------------------
phase1() {   # run 1: H5 additivity (floor12 + ledger on the teacher sets, trainclean once); paired with base, ctrl, mixv2, floor12
  preflight "$FOLD"
  require_files "$CTRL_PT" "$MIXV2_PT" "$FLOOR12_PT" || exit 1
  reset_arm
  REFS="mixv2=$MIXV2_PT floor12=$FLOOR12_PT"; AGAINST=ctrl
  ROW_EXTRA="\"floor_scope\": \"teacher sets only (--min-area-px-for)\", \"floor_px_for\": $FLOOR_FOR"
  arm "$COMBO" "$FOLD" "$FLOOR_PX" "$POLICY" "$LABEL" \
      "H5 combined: floor12 + ledger on the four teacher sets only (216 px; 2025 from ledger-v1) + dataset-v2-trainclean once at 16 px; squared; cap 200" phase1
}
phase2() {   # runs 2-3: objective knobs on the combined fold, one flag each; paired with combo (one flag), base, ctrl
  preflight "$FOLD"
  wait_for_rows "$COMBO"
  [ "$DRY" = 1 ] || [ -f "$R/$COMBO/trainable-final.pt" ] || { log "phase2: $R/$COMBO/trainable-final.pt missing; stopping"; exit 1; }
  reset_arm
  TRAIN_EXTRA='--pos-weight 3'; REFS="combo=$R/$COMBO/trainable-final.pt"; AGAINST=combo
  ROW_EXTRA='"arm_flag": "--pos-weight 3"'
  arm "$POSW3" "$FOLD" "$FLOOR_PX" "$POLICY" "$LABEL" \
      "H6 positive/negative balance: r4-combo fold and line with IABCEMdetr pos_weight 10 -> 3 (one flag; lowers total positive gradient as well as the balance)" phase2
  reset_arm
  o2m=$(o2m_arm)
  REFS="combo=$R/$COMBO/trainable-final.pt"; AGAINST=combo
  if [ "$o2m" = "$O2M1" ]; then
    TRAIN_EXTRA='--o2m-weight 1'; ROW_EXTRA="\"arm_flag\": \"--o2m-weight 1\", \"fallback_for\": \"$O2M0\""
    h7="H7 assignment (pre-registered fallback): Sam3LossWrapper o2m_weight 2 -> 1 because the o2m0 run failed the training gate inside the first $EARLY_GATE_STEPS steps"
  else
    TRAIN_EXTRA='--o2m-weight 0'; ROW_EXTRA='"arm_flag": "--o2m-weight 0"'; FALLBACK_RUN=$O2M1; FALLBACK_EXTRA='--o2m-weight 1'
    h7="H7 assignment: r4-combo fold and line with Sam3LossWrapper o2m_weight 2 -> 0 (one flag; removes the auxiliary o2m box / mask / classification supervision; o2m queries are still emitted)"
  fi
  arm "$o2m" "$FOLD" "$FLOOR_PX" "$POLICY" "$LABEL" "$h7" phase2
}
phase3() {   # run 4: seed-2 replicate of the arm chosen by the pre-registered rule; paired with twin (one flag), base, ctrl [+ combo]
  preflight "$FOLD"
  if [ "$DRY" = 1 ]; then echo "would wait until results rows exist for: $COMBO $POSW3 $(o2m_arm)"; else
    wait_for_rows "$COMBO" "$POSW3"
    is_done "$(o2m_arm)" || log "waiting for results row: $(o2m_arm)"
    until is_done "$(o2m_arm)"; do sleep 60; done       # re-read each minute: the o2m0 arm may hand over to its fallback
  fi
  o2m=$(o2m_arm)
  sel=$($PY -m scripts.ml.training.sam3_mac.r3_record select-replicate --root "$R" --candidate "$COMBO" --candidate "$POSW3" --candidate "$o2m" --default "$COMBO")
  [ -n "$sel" ] || { log "phase3: select-replicate produced no output; stopping"; exit 1; }
  log "replicate selection: $sel"
  chosen=$(printf '%s' "$sel" | $PY -c 'import json,sys; print(json.load(sys.stdin)["chosen"])')
  case "$chosen" in
    "$COMBO") flags='' ;;
    "$POSW3") flags='--pos-weight 3' ;;
    "$O2M0") flags='--o2m-weight 0' ;;
    "$O2M1") flags='--o2m-weight 1' ;;
    *) log "phase3: unknown replicate choice '$chosen'; stopping"; exit 1 ;;
  esac
  run=$(printf '%s' "$chosen" | sed 's/-seed1$/-seed2/')
  [ "$DRY" = 1 ] || [ -f "$R/$chosen/trainable-final.pt" ] || { log "phase3: $R/$chosen/trainable-final.pt missing; stopping"; exit 1; }
  reset_arm
  SEED=2; TRAIN_EXTRA=$flags; REFS="twin=$R/$chosen/trainable-final.pt"; AGAINST=twin
  [ "$chosen" = "$COMBO" ] || REFS="$REFS combo=$R/$COMBO/trainable-final.pt"
  ROW_EXTRA="\"arm_flag\": \"--seed 2\", \"twin\": \"$chosen\", \"replicate_selection\": $sel"
  arm "$run" "$FOLD" "$FLOOR_PX" "$POLICY" "$LABEL" \
      "H8 seed noise: replicate of $chosen with --seed 2 (one rule: seed; train flags '${flags:-round-3 line}'); read against the stage-2 seed spread (0.008 share, 0.013 m)" phase3
  total=$(cumulative_wall "$R/$COMBO.train.wall" "$R/$POSW3.train.wall" "$R/$o2m.train.wall" "$R/$run.train.wall")
  if [ "$total" -gt 0 ] && [ "$total" -lt 21600 ]; then
    log "f16 decision: cumulative train wall of runs 1-4 = ${total}s (< 6 h): the uncommitted r4-combo-f16 arm (--min-area-px-for <teacher>=287, paired with r4-combo) may be built and started by hand when the host is idle; not launched here"
  else
    log "f16 decision: cumulative train wall of runs 1-4 = ${total}s (>= 6 h or unknown): r4-combo-f16 is queued for round 5"
  fi
}

# --- main ----------------------------------------------------------------------------------------------------------
case "$PHASE" in phase1|phase2|phase3|all) ;; *) usage ;; esac
if [ "$DRY" = 1 ]; then
  echo "round4.sh DRY RUN: phase=$PHASE root=$R fold=$FOLD judge_mode=$JUDGE_MODE judge_t=$JUDGE_T rules=$([ -f "$RULES" ] && echo present || echo absent) ctrl=$CTRL_PT (no run started)"
else
  if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "round4.sh already running (pid $(cat "$LOCK")); exiting" >&2; exit 2
  fi
  echo $$ > "$LOCK"
  trap 'rm -f "$LOCK"' EXIT INT TERM HUP
  log "round4.sh $PHASE start pid=$$ fold=$FOLD judge_mode=$JUDGE_MODE judge_t=$JUDGE_T rules_sha=$(shasum -a 256 "$RULES" | cut -c1-16) mps_high=$PYTORCH_MPS_HIGH_WATERMARK_RATIO mps_low=$PYTORCH_MPS_LOW_WATERMARK_RATIO"
fi
case "$PHASE" in
  phase1) phase1 ;;
  phase2) phase2 ;;
  phase3) phase3 ;;
  all) phase1; phase2; phase3 ;;
esac
log "round4.sh $PHASE done"
