"""Round-3 ledger helper for the SAM3 teacher-loop waiter (sam3-mac-round3-record-v1).

round3.sh calls this between fine-tunes; nothing here runs a model. Modes:

  gate    --coco FOLD [--cap 200]        exit 0 when the fold's train split has images and none carries more than
                                          --cap annotations (SAM3's FilterFindQueriesWithTooManyOut drops such
                                          images silently; stage 2 lost 60 of 151 windows that way)
  done    --root R --run NAME            exit 0 when results.jsonl already holds a complete row for NAME
  latest  --run-dir DIR                  print the newest trainable-step*.pt (resume candidate) or nothing
  record  --root R --run NAME --coco FOLD ...   append exactly one row to R/results.jsonl; in full judge mode any judge gap (a block's
                                          raw summary.json, its 0.45 view, the v1 / v2 matched view or calibration.json missing)
                                          writes a NAME_error row (judge_missing:<names>) and exits 1 instead, so the next waiter
                                          run rebuilds what is missing from the existing trainable-final.pt; a complete row therefore
                                          always reads its verdict on dataset-v2 (never the dataset-v1 continuity block)
  error   --root R --run NAME --reason TEXT     append one NAME_error row (consumers dedupe by run and skip *_error)
  select-replicate --root R --candidate NAME... --default NAME
                                          round 4: print JSON {chosen, reason, candidates} for the seed-2 replicate: the first
                                          candidate (in the given order) whose complete dataset-v2 row (primary_block v2) has
                                          verdict.removes_over_detection == true AND verdict.keeps_matched not false; else
                                          --default. Complete rows only (error rows ignored); reads the 0.45 verdict; nothing is run.

Round-4 additions (rows keep the round-3 shape and add keys): --ref NAME (repeatable) copies the judge's NAME arm summary
and its tuned:NAME pair into every block and the row (row[NAME], row['paired_vs_NAME']); --against NAME (default ctrl)
reads keeps_matched / keeps_boundary / agreement against NAME while removes_over_detection stays base-relative; a row
judged against another arm keeps 'verdict' (against that arm) and adds 'verdict_vs_ctrl'; 'objective' =
{presence_weight, pos_weight, o2m_weight} from <run>/run.json args (defaults 20 / 10 / 2 when the flags are absent, as
in every round 1-3 run; single source, never passed through --extra). gate --no-zero-annotation-images adds the
round-4 zero-annotation gate beside the cap gate.

Row wording: agreement, model estimate, reference. Footprint backing is agreement with another model (Microsoft
footprints are ML outputs). Nothing recorded here promotes anything; production latest.json / review.json /
confidence are never written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import UTC
from pathlib import Path

VERSION = 'sam3-mac-round3-record-v2'   # v2 (round 4): --ref / --against, objective, select-replicate; the row is a superset of v1
REPO = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
CODE_FILES = ('finetune_mps.py', 'prepare_coco.py', 'reference_eval.py', 'smoke_infer.py', 'round3.sh', 'round4.sh', 'r3_record.py',
              'r3_calibrate.py', 'r3_postfilter.py', 'round3_report.py')
FOOTPRINT_CACHES = ('data/elevation/derived/ms-buildings/bbox.geojsonl.gz',
                    'data/elevation/derived/ms-buildings/bbox-inner-melb.geojsonl.gz')
BACKGROUND_DROPS = ('ann_tiny', 'ann_over_cap', 'ann_low_weight', 'ann_no_segmentation', 'ann_policy_ignored')
BLOCKS = ('v1', 'v2', 'v2val')
MATCHED_BLOCKS = ('v1', 'v2')   # r3_postfilter builds the -matched view of these two; v2val only supplies t* (calibration)
LOADER_ERROR = 'got loading error'
# STEP 7 decision rules; RULES.json overrides these when it carries the same keys
RULES_DEFAULT = {'unbacked_share_margin': .05, 'novel_per_ha_ratio_max': 1.5, 'matched_slack': 1,
                 'boundary_delta_max_m': .02, 'boundary_worse_share_max': .6, 'agreement_lower_bound_min': -.01}
# finetune_mps.build_loss defaults (LOSS_DEFAULTS there): a run.json without these keys predates the flags and trained with them
OBJECTIVE_DEFAULTS = {'presence_weight': 20., 'pos_weight': 10., 'o2m_weight': 2.}
FIXED_ARMS = ('base', 'ctrl', 'tuned')
NOTE = ('agreement with human boundary references (model estimates against references, never a calibrated error rate); footprint backing is '
        'agreement with another model (Microsoft footprints, ML outputs); human rejections are never counted as roof false '
        'positives; nothing is promoted by this row')


def now_iso():
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec='seconds')


def sha256_file(path):
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def code_shas(folder=HERE, names=CODE_FILES):
    """sha256 of every orchestration / judge / trainer script that exists; git-free provenance."""
    return {name: sha for name in names if (sha := sha256_file(Path(folder) / name))}


def read_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in open(path):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def is_error_row(row):
    return bool(row.get('error')) or str(row.get('run', '')).endswith('_error')


def completed_runs(rows):
    return {r['run'] for r in rows if 'run' in r and not is_error_row(r)}


def has_row(results_path, run):
    return run in completed_runs(read_rows(results_path))


def append_row(results_path, row):
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, 'a') as handle:
        handle.write(json.dumps(row, sort_keys=True) + '\n')


# --- fold ---------------------------------------------------------------------------------------------------------

def _train_json(coco):
    path = Path(coco) / 'train' / '_annotations.coco.json'
    return json.load(open(path)) if path.exists() else None


def fold_gate(coco, cap=200, require_annotations=False):
    """Trainable when the train split has images and no image exceeds the cap (0 images > cap is the round-3 rule).
    require_annotations (round 4) also refuses a fold with zero-annotation train images: the presence target is 1 on
    every image only when none is empty (FilterEmptyTargets drops zero-area objects only); the count is always reported."""
    out = {'coco': str(coco), 'cap': cap, 'ok': False, 'reasons': [], 'manifest': (Path(coco) / 'manifest.json').exists(),
           'zero_annotation_gate': bool(require_annotations)}
    train = _train_json(coco)
    if train is None:
        out['reasons'].append('train/_annotations.coco.json missing')
        return out
    per_image = Counter(a['image_id'] for a in train['annotations'])
    out.update({'images': len(train['images']), 'annotations': len(train['annotations']),
                'max_annotations_per_image': max(per_image.values()) if per_image else 0,
                'images_over_cap': sum(v > cap for v in per_image.values()),
                'images_zero_annotations': sum(1 for i in train['images'] if per_image.get(i['id'], 0) == 0)})
    if not train['images']:
        out['reasons'].append('no train images')
    if out['images_over_cap']:
        out['reasons'].append(f"{out['images_over_cap']} train images carry more than {cap} annotations")
    if require_annotations and out['images_zero_annotations']:
        out['reasons'].append(f"{out['images_zero_annotations']} train images carry no annotation (zero-annotation gate)")
    if not out['manifest']:
        out['reasons'].append('manifest.json missing (sha cannot be recorded)')
    out['ok'] = not out['reasons']
    return out


def fold_stats(coco, cap=200):
    """Row block for the fold: manifest counts plus images_over_cap recomputed from the COCO json."""
    coco = Path(coco)
    stats = {'path': str(coco), 'cap': cap}
    manifest_path = coco / 'manifest.json'
    manifest = json.load(open(manifest_path)) if manifest_path.exists() else {}
    train = (manifest.get('splits') or {}).get('train') or {}
    drops = train.get('dropped_or_notes') or {}
    stats.update({'images': train.get('images'), 'annotations': train.get('annotations'),
                  'annotations_per_image_median': train.get('annotations_per_image_median'),
                  'ann_over_cap': drops.get('ann_over_cap', 0), 'ann_tiny': drops.get('ann_tiny', 0),
                  'ann_low_weight': drops.get('ann_low_weight', 0),
                  'flipped_to_background': train.get('flipped_to_background', sum(drops.get(k, 0) for k in BACKGROUND_DROPS)),
                  'flipped_to_background_components': {k: drops[k] for k in BACKGROUND_DROPS if k in drops},
                  'per_source': {k[len('prov:'):]: v for k, v in drops.items() if k.startswith('prov:')},
                  'sources': manifest.get('sources'),
                  # prepare_coco writes these under splits.train (density_per_ha_median, distance_check, per_source)
                  'density_median_per_ha': train.get('density_per_ha_median', train.get('density_median_per_ha')),
                  'distance_check': train.get('distance_check', manifest.get('distance_check')),
                  'per_source_provenance': train.get('per_source', manifest.get('per_source'))})
    options = manifest.get('options') or {}
    stats['min_area_px'] = options.get('min_area_px')
    stats['min_area_px_for'] = options.get('min_area_px_for')   # round 4: per-source floors (prepare_coco --min-area-px-for), else None / {}
    gate = fold_gate(coco, cap)
    stats['images_over_200' if cap == 200 else f'images_over_{cap}'] = gate.get('images_over_cap')
    stats['max_annotations_per_image'] = gate.get('max_annotations_per_image')
    stats['images_zero_annotations'] = gate.get('images_zero_annotations')
    if stats['images'] is None:
        stats['images'], stats['annotations'] = gate.get('images'), gate.get('annotations')
    return stats


# --- training -----------------------------------------------------------------------------------------------------

STEP_RE = re.compile(r'trainable-step(\d+)\.pt$')


def latest_checkpoint(run_dir):
    """Newest trainable-step*.pt by step number, or None (trainable-final.pt is the caller's business)."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return None
    found = [(int(m.group(1)), p) for p in run_dir.glob('trainable-step*.pt') if (m := STEP_RE.search(p.name))]
    return str(max(found)[1]) if found else None


def loader_errors(log_path):
    log_path = Path(log_path)
    if not log_path.exists():
        return None
    return sum(LOADER_ERROR in line for line in open(log_path, errors='replace'))


def _wall_seconds(root, run, summary):
    wall_file = Path(root) / f'{run}.train.wall'
    segments = []
    if wall_file.exists():
        segments = [int(float(x)) for x in wall_file.read_text().split() if x.strip()]
    if segments:
        return sum(segments), segments
    try:
        from datetime import datetime
        a, b = datetime.fromisoformat(summary['started_at']), datetime.fromisoformat(summary['finished_at'])
        return int((b - a).total_seconds()), []
    except (KeyError, TypeError, ValueError):
        return None, []


def objective(root, run):
    """{presence_weight, pos_weight, o2m_weight} of the run from <run>/run.json args (finetune_mps records vars(args));
    OBJECTIVE_DEFAULTS when a key is absent (every round 1-3 run trained with them). Single source; never read from --extra."""
    path = Path(root) / run / 'run.json'
    args = {}
    if path.exists():
        try:
            args = json.load(open(path)).get('args') or {}
        except (json.JSONDecodeError, AttributeError):
            args = {}
    out = {key: (args[key] if args.get(key) is not None else default) for key, default in OBJECTIVE_DEFAULTS.items()}
    recorded = sorted(key for key in OBJECTIVE_DEFAULTS if args.get(key) is not None)
    out['source'] = ('run.json args' if recorded else ('defaults (run.json lacks the loss flags: round 1-3 line)' if path.exists()
                                                       else 'defaults (run.json absent)'))
    out['recorded_keys'] = recorded
    out['note'] = ('IABCEMdetr weight_dict loss_ce 20 / presence_loss presence_weight, pos_weight; Sam3LossWrapper o2m_weight; '
                   'the presence target is 1 on every training image of a fold without zero-annotation images')
    return out


def train_stats(root, run):
    run_dir = Path(root) / run
    out = {'summary_present': (run_dir / 'summary.json').exists(), 'final_checkpoint_present': (run_dir / 'trainable-final.pt').exists()}
    summary = json.load(open(run_dir / 'summary.json')) if out['summary_present'] else {}
    run_meta = json.load(open(run_dir / 'run.json')) if (run_dir / 'run.json').exists() else {}
    wall, segments = _wall_seconds(root, run, summary)
    out.update({'skipped_batches': summary.get('skipped_batches'), 'loss_first': summary.get('loss_first'),
                'loss_median_last_10': summary.get('loss_median_last_10'), 'seconds_per_step_median': summary.get('seconds_per_step_median'),
                'steps_completed': summary.get('steps'), 'epochs': summary.get('epochs'), 'train_images': summary.get('train_images'),
                'wall_seconds': wall, 'wall_segments': segments, 'memory_last': summary.get('memory_last'),
                'resumed_from': (run_meta.get('args') or {}).get('resume'),
                'started_at': summary.get('started_at'), 'finished_at': summary.get('finished_at'),
                'note': 'losses are training diagnostics; a resumed run restarts the data order from the seed and is not '
                        'step-identical to an uninterrupted one' if (run_meta.get('args') or {}).get('resume') else
                        'losses are training diagnostics'})
    return out


# --- judge blocks -------------------------------------------------------------------------------------------------

def load_judge(folder):
    path = Path(folder) / 'summary.json'
    return json.load(open(path)) if path.exists() else None


def find_pair(summary, second, first):
    """Paired block '<second> - <first>' in any of the judge shapes: legacy single 'paired', or 'pairs'/'paired' keyed
    'second:first' or listed with second_minus_first."""
    label = f'{second} - {first}'
    keys = (f'{second}:{first}', f'{second}-{first}', label)
    for holder in (summary.get('pairs'), summary.get('paired')):
        if holder is None:
            continue
        if isinstance(holder, dict):
            if holder.get('second_minus_first') == label:
                return holder
            for key in keys:
                if isinstance(holder.get(key), dict):
                    return holder[key]
        elif isinstance(holder, list):
            for entry in holder:
                if isinstance(entry, dict) and (entry.get('second_minus_first') == label
                                                or (entry.get('second'), entry.get('first')) == (second, first)):
                    return entry
    return None


EXTRA_KEYS = ('by_area_band', 'by_tier', 'by_pixel_band', 'same_image_only', 'different_image', 'shape_agreement', 'strata',
              'pseudo_recall_at_0_5', 'per_hectare', 'novel_area_histogram', 'edit_diff_strata', 'rejected_overlap', 'footprint_caches',
              'view', 'arm_thresholds', 'arm_thresholds_source', 'arm_threshold_notes')


def block_from_judge(payload, ctrl_payload=None, refs=()):
    """Compact view of one judge folder; ctrl_payload adds the legacy two-arm ctrl-vs-tuned invocation when the judge
    could not take three arms at once. refs (round 4: mixv2 / floor12 / combo / twin) copies each reference arm's summary
    and its tuned:<ref> pair (block['paired_vs_<ref>']); combo_vs_ctrl is added when the judge paired combo:ctrl."""
    if payload is None:
        return None
    s = payload.get('summary') or {}
    negatives = {k: v for k, v in (s.get('negatives') or {}).items() if k != 'note'}
    block = {'threshold': payload.get('threshold'), 'judge_version': payload.get('version'), 'images': payload.get('images'),
             'computed_at': payload.get('computed_at'), 'arms': sorted((payload.get('arms') or {}).keys()),
             'base': s.get('base'), 'ctrl': s.get('ctrl'), 'tuned': s.get('tuned'),
             'paired_vs_base': find_pair(s, 'tuned', 'base'), 'paired_vs_ctrl': find_pair(s, 'tuned', 'ctrl'),
             'ctrl_vs_base': find_pair(s, 'ctrl', 'base'), 'negatives': negatives}
    for ref in refs:
        if ref in FIXED_ARMS:
            continue
        block[ref] = s.get(ref)
        block[f'paired_vs_{ref}'] = find_pair(s, 'tuned', ref)
    combo_vs_ctrl = find_pair(s, 'combo', 'ctrl')
    if combo_vs_ctrl is not None:
        block['combo_vs_ctrl'] = combo_vs_ctrl
    for key in EXTRA_KEYS:
        if key in s:
            block[key] = s[key]
        elif key in payload:
            block[key] = payload[key]
    if ctrl_payload is not None:
        s2 = ctrl_payload.get('summary') or {}
        block['ctrl'] = block['ctrl'] or s2.get('ctrl')
        block['paired_vs_ctrl'] = block['paired_vs_ctrl'] or find_pair(s2, 'tuned', 'ctrl')
        for arm in ('ctrl',):
            if arm in (s2.get('negatives') or {}):
                block['negatives'].setdefault(arm, s2['negatives'][arm])
        block['ctrl_invocation'] = 'separate two-arm judge (legacy reference_eval)'
    return block


def judge_blocks(root, run, ctrl_run=None, judge_mode='full', refs=()):
    """blocks[v1|v2|v2val] = {view_raw, view_045, view_matched} from the run's reference-eval folders; every view may be
    None; each missing block says why. refs are copied into every view (round 4)."""
    run_dir = Path(root) / run
    blocks = {}
    if judge_mode == 'legacy':
        primary = load_judge(run_dir / 'reference-eval-v1')
        ctrl = load_judge(run_dir / 'reference-eval-v1-vs-ctrl')
        blocks['v1'] = {'view_raw': None, 'view_045': block_from_judge(primary, ctrl), 'view_matched': None,
                        'dataset': 'dataset-v1', 'split': 'test'}
        for name, ds, split in (('v2', 'dataset-v2', 'test'), ('v2val', 'dataset-v2', 'val')):
            blocks[name] = {'status': 'not_run', 'reason': 'reference_eval lacks --dataset/--split (legacy judge)', 'dataset': ds, 'split': split}
        return blocks
    for name, ds, split in (('v1', 'dataset-v1', 'test'), ('v2', 'dataset-v2', 'test'), ('v2val', 'dataset-v2', 'val')):
        base = run_dir / f'reference-eval-{name}'
        raw = load_judge(base)
        t045 = load_judge(Path(str(base) + '-t045'))
        matched = load_judge(Path(str(base) + '-matched'))
        if raw is None:
            blocks[name] = {'status': 'not_run', 'reason': f'{base.name}/summary.json missing', 'dataset': ds, 'split': split}
            continue
        raw_block = block_from_judge(raw, refs=refs)
        view_045 = block_from_judge(t045, refs=refs) if t045 is not None else (raw_block if _close(raw.get('threshold'), .45) else None)
        blocks[name] = {'dataset': ds, 'split': split, 'view_raw': raw_block, 'view_045': view_045,
                        'view_matched': block_from_judge(matched, refs=refs) if matched is not None else None,
                        'view_045_source': 'r3_postfilter' if t045 is not None else ('direct judge at 0.45' if view_045 else 'absent')}
    return blocks


def judge_gaps(blocks, calibration=None, judge_mode='full'):
    """{name: reason} for what the full judge chain (reference_eval -> r3_calibrate -> r3_postfilter) should have produced but did
    not: a block's raw summary (status not_run), its 0.45 view, its matched view (v1 / v2 only) or calibration.json itself
    (status not_run). Any gap means the arm is not finalised: record writes an error row instead of a row whose verdict is empty
    or silently read from the dataset-v1 continuity block. Legacy mode lacks v2 / v2val and the views by design."""
    if judge_mode != 'full':
        return {}
    gaps = {}
    for name in BLOCKS:
        block = blocks.get(name) or {}
        if block.get('status') == 'not_run':
            gaps[name] = block.get('reason') or f'reference-eval-{name}/summary.json missing'
        elif not block.get('view_045'):
            gaps[name] = f'reference-eval-{name}-t045/summary.json missing and the raw judge is not at 0.45 (0.45 view absent; r3_postfilter)'
        elif name in MATCHED_BLOCKS and not block.get('view_matched'):
            gaps[name] = f'reference-eval-{name}-matched/summary.json missing (matched view at t* absent; r3_postfilter)'
    if calibration is not None and (calibration or {}).get('status') == 'not_run':
        gaps['calibration'] = 'calibration.json missing (r3_calibrate did not run or failed; no t*)'
    return gaps


def missing_judge_blocks(blocks, judge_mode='full', calibration=None):
    """Names with a judge gap (see judge_gaps); [] in legacy mode."""
    return list(judge_gaps(blocks, calibration, judge_mode))


def _close(a, b, tol=1e-6):
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def primary_view(blocks):
    """The headline view: dataset-v2 test at 0.45, else dataset-v1 test at 0.45; (name, view) or (None, None)."""
    for name in ('v2', 'v1'):
        view = (blocks.get(name) or {}).get('view_045')
        if view:
            return name, view
    return None, None


# --- verdict (STEP 7 rules, read on the primary 0.45 view) --------------------------------------------------------

def _get(d, *keys):
    for key in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _ci(pair):
    if not isinstance(pair, dict):
        return None
    for key in ('best_iou_delta_ci95_image_bootstrap', 'ci95', 'best_iou_delta_ci95'):
        ci = pair.get(key)
        if isinstance(ci, (list, tuple)) and len(ci) == 2 and all(isinstance(x, (int, float)) for x in ci):
            return ci
    return None


def verdict(view, rules=None, against='ctrl'):
    """Decision flags against the pairing arm `against` (ctrl by default; round 4 reads the objective arms against combo and
    the replicate against its twin). removes_over_detection is base-relative in every row; keeps_matched, keeps_boundary
    and agreement are read against `against`. Every flag is None with a reason when its inputs are not in the judge
    output; nothing here promotes anything."""
    rules = {**RULES_DEFAULT, **{k: v for k, v in (rules or {}).items() if k in RULES_DEFAULT}}
    out = {'rules': rules, 'against': None, 'against_requested': against, 'removes_over_detection': None, 'keeps_matched': None,
           'keeps_boundary': None, 'agreement': None, 'notes': []}
    if not view:
        out['notes'].append('no 0.45 view available')
        return out
    neg = view.get('negatives') or {}
    base_neg, tuned_neg = neg.get('base') or {}, neg.get('tuned') or {}
    share_b, share_t = base_neg.get('novel_no_footprint_share_of_predictions'), tuned_neg.get('novel_no_footprint_share_of_predictions')
    ha_b, ha_t = base_neg.get('novel_per_ha'), tuned_neg.get('novel_per_ha')
    if share_b is not None and share_t is not None and ha_b is not None and ha_t is not None:
        out['removes_over_detection'] = share_t <= share_b + rules['unbacked_share_margin'] and ha_t <= rules['novel_per_ha_ratio_max'] * ha_b
        out['over_detection_inputs'] = {'unbacked_share_base': share_b, 'unbacked_share_tuned': share_t, 'novel_per_ha_base': ha_b, 'novel_per_ha_tuned': ha_t}
    elif share_b is not None and share_t is not None:
        out['over_detection_inputs'] = {'unbacked_share_base': share_b, 'unbacked_share_tuned': share_t}
        out['notes'].append('novel_per_ha not reported by this judge; over-detection rule not evaluated (unbacked share shown)')
    else:
        out['notes'].append('unbacked novel share not reported')
    ref = against if view.get(against) else None
    out['against'] = ref
    if ref is None:
        out['notes'].append(f'{against} arm absent from this judge folder: matched / boundary / agreement rules need {against}')
        return out
    pairing, tuned = view[ref], view.get('tuned') or {}
    if pairing.get('matched_at_iou_0_5') is not None and tuned.get('matched_at_iou_0_5') is not None:
        out['keeps_matched'] = tuned['matched_at_iou_0_5'] >= pairing['matched_at_iou_0_5'] - rules['matched_slack']
    pair = view.get(f'paired_vs_{ref}') or {}
    b_delta = pair.get('boundary_distance_delta_median_m', pair.get('boundary_delta_median_m'))
    worse, better = pair.get('boundary_worse'), pair.get('boundary_better')
    if b_delta is not None:
        share_worse = worse / (worse + better) if worse is not None and better is not None and (worse + better) else None
        out['keeps_boundary'] = b_delta <= rules['boundary_delta_max_m'] and (share_worse is None or share_worse <= rules['boundary_worse_share_max'])
        out['boundary_method'] = 'paired on jointly matched references'
    elif pairing.get('boundary_distance_m_median_matched') is not None and tuned.get('boundary_distance_m_median_matched') is not None:
        out['keeps_boundary'] = (tuned['boundary_distance_m_median_matched'] - pairing['boundary_distance_m_median_matched']) <= rules['boundary_delta_max_m']
        out['boundary_method'] = 'unpaired medians (judge reported no paired boundary delta); worse/better share not evaluated'
    same = _get(view, 'same_image_only', f'paired_vs_{ref}') or pair
    ci = _ci(same)
    if ci is not None:
        lower, upper = ci
        if lower > 0:
            out['agreement'] = 'improves'
        elif lower > rules['agreement_lower_bound_min']:
            out['agreement'] = 'non_inferior'
        elif upper < 0:
            out['agreement'] = 'lower'
        else:
            out['agreement'] = 'inconclusive'
        out['agreement_ci'] = ci
        out['agreement_scope'] = 'same_image refs' if same is not pair else 'all refs (judge reported no same_image split)'
    return out


# --- row ----------------------------------------------------------------------------------------------------------

def load_rules(root):
    path = Path(root) / 'RULES.json'
    if not path.exists():
        return None, None
    try:
        return json.load(open(path)), sha256_file(path)
    except json.JSONDecodeError:
        return None, sha256_file(path)


def build_row(root, run, coco, arm_rule='', label_target=None, policy=None, floor_px=None, cap=200, steps=3000, lr=8e-5, seed=1,
              save_every=1500, judge_mode='full', ctrl_run=None, extra=None, refs=(), against='ctrl'):
    root = Path(root)
    refs = tuple(r for r in refs if r not in FIXED_ARMS)
    rules, rules_sha = load_rules(root)
    blocks = judge_blocks(root, run, ctrl_run, judge_mode, refs)
    name, view = primary_view(blocks)
    calibration_path = root / run / 'calibration.json'
    calibration = json.load(open(calibration_path)) if calibration_path.exists() else {'status': 'not_run'}
    row = {'version': VERSION, 'recorded_at': now_iso(), 'run': run, 'arm_rule': arm_rule, 'coco': str(coco),
           'coco_manifest_sha': sha256_file(Path(coco) / 'manifest.json'), 'fold': fold_stats(coco, cap),
           'label_target': label_target, 'policy': policy, 'floor_px': floor_px, 'cap': cap, 'steps': steps, 'lr': lr, 'seed': seed,
           'save_every': save_every, 'dtype': 'bf16', 'warmup': 50, 'train': train_stats(root, run),
           'loader_errors': loader_errors(root / f'{run}.train.log'), 'judge_mode': judge_mode, 'ctrl_run': ctrl_run,
           'judge_version': next((b.get('view_raw') or b.get('view_045') or {}).get('judge_version') for b in blocks.values()
                                 if (b.get('view_raw') or b.get('view_045'))) if any((b.get('view_raw') or b.get('view_045')) for b in blocks.values()) else None,
           'judge_version_sha': sha256_file(HERE / 'reference_eval.py'), 'code_sha256': code_shas(),
           'rules_sha': rules_sha, 'rules_source': 'RULES.json' if rules is not None else 'built-in defaults (RULES.json absent or unreadable)',
           'footprint_cache_shas': {p: sha for p in FOOTPRINT_CACHES if (sha := sha256_file(REPO / p))},
           'calibration': calibration, 'blocks': blocks, 'primary_block': name,
           'base': (view or {}).get('base'), 'tuned': (view or {}).get('tuned'), 'ctrl': (view or {}).get('ctrl'),
           'paired': (view or {}).get('paired_vs_base'), 'paired_vs_ctrl': (view or {}).get('paired_vs_ctrl'),
           'negatives': (view or {}).get('negatives'), 'verdict': verdict(view, rules, against), 'against': against, 'refs': list(refs),
           'objective': objective(root, run), 'note': NOTE}
    for ref in refs:   # round 4: reference arms beside ctrl (mixv2 / floor12 for run 1, combo for runs 2-3, twin [+ combo] for run 4)
        row[ref] = (view or {}).get(ref)
        row[f'paired_vs_{ref}'] = (view or {}).get(f'paired_vs_{ref}')
    if (view or {}).get('combo_vs_ctrl') is not None:
        row['combo_vs_ctrl'] = view['combo_vs_ctrl']
    if against != 'ctrl':   # the ctrl read is kept beside the arm's one-rule verdict
        row['verdict_vs_ctrl'] = verdict(view, rules, 'ctrl')
    if extra:
        row.update(extra)
    return row


def error_row(root, run, reason, coco=None, extra=None):
    row = {'version': VERSION, 'recorded_at': now_iso(), 'run': f'{run}_error', 'arm': run, 'error': reason, 'coco': coco,
           'loader_errors': loader_errors(Path(root) / f'{run}.train.log'), 'train': train_stats(root, run) if (Path(root) / run).exists() else None,
           'code_sha256': code_shas()}
    if extra:
        row.update(extra)
    return row


def select_replicate(rows, candidates, default):
    """Round-4 Run 4 rule (RULES.json replicate_selection): the first candidate, in the given order, whose complete row was read on
    dataset-v2 (primary_block == 'v2'; a row whose verdict fell back to the dataset-v1 continuity block never clears) and has
    verdict.removes_over_detection == True (dataset-v2 test 0.45: unbacked share <= base + 0.05 AND novel/ha <= 1.5 x base;
    base-relative in every row) AND verdict.keeps_matched is not False; else `default`. Complete rows only (error rows are
    ignored; the last complete row of a run wins). Reads the 0.45 verdict; never the matched view; selects only the seed-2
    replicate arm; nothing is promoted."""
    latest = {}
    for row in rows:
        if 'run' in row and not is_error_row(row):
            latest[row['run']] = row
    report, chosen, reason = [], None, None
    for name in candidates:
        row = latest.get(name)
        v = (row or {}).get('verdict') or {}
        entry = {'run': name, 'row_present': row is not None, 'primary_block': (row or {}).get('primary_block'),
                 'removes_over_detection': v.get('removes_over_detection'), 'keeps_matched': v.get('keeps_matched'), 'verdict_against': v.get('against')}
        entry['clears'] = (row is not None and row.get('primary_block') == 'v2' and v.get('removes_over_detection') is True
                           and v.get('keeps_matched') is not False)
        report.append(entry)
        if chosen is None and entry['clears']:
            chosen = name
            reason = f'first candidate whose dataset-v2 row (primary_block v2) has removes_over_detection true and keeps_matched not false: {name}'
    if chosen is None:
        chosen = default
        if not any(e['row_present'] for e in report):
            reason = f'no complete results row for any candidate; default {default}'
        else:
            reason = (f'no candidate clears the density rule on a dataset-v2 row (primary_block v2, removes_over_detection true and '
                      f'keeps_matched not false); default {default}')
    return {'chosen': chosen, 'reason': reason, 'default': default, 'candidates': report,
            'rule': ('first candidate (in order) whose row has primary_block == v2 AND verdict.removes_over_detection == true AND '
                     'verdict.keeps_matched is not false; else default'),
            'read_on': 'dataset-v2 test at 0.45 (row verdict): removes_over_detection is base-relative, keeps_matched is against the arm\'s pairing arm',
            'note': 'selects only the seed-2 replicate arm; nothing is promoted'}


def headline(row):
    keys = ('run', 'primary_block', 'judge_mode', 'loader_errors', 'against')
    out = {k: row.get(k) for k in keys}
    arms = ('base', 'ctrl', 'tuned', *(row.get('refs') or ()))
    out['matched'] = {arm: _get(row, arm, 'matched_at_iou_0_5') for arm in arms if row.get(arm)}
    out['paired_vs_base'] = _get(row, 'paired', 'best_iou_delta_mean')
    out['paired_vs_ctrl'] = _get(row, 'paired_vs_ctrl', 'best_iou_delta_mean')
    against = row.get('against') or 'ctrl'
    if against != 'ctrl':
        out[f'paired_vs_{against}'] = _get(row, f'paired_vs_{against}', 'best_iou_delta_mean')
        out['verdict_vs_ctrl'] = {k: (row.get('verdict_vs_ctrl') or {}).get(k)
                                  for k in ('removes_over_detection', 'keeps_matched', 'keeps_boundary', 'agreement')}
    if row.get('objective'):
        out['objective'] = {k: row['objective'].get(k) for k in OBJECTIVE_DEFAULTS}
    out['unbacked_share'] = {arm: _get(row, 'negatives', arm, 'novel_no_footprint_share_of_predictions') for arm in ('base', 'ctrl', 'tuned')
                             if _get(row, 'negatives', arm)}
    out['t_star'] = _get(row, 'calibration', 't_star')
    out['verdict'] = {k: row['verdict'].get(k) for k in ('removes_over_detection', 'keeps_matched', 'keeps_boundary', 'agreement')}
    return out


def _parse_extra(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise SystemExit(f'--extra must be a JSON object: {error}') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='mode', required=True)
    g = sub.add_parser('gate')
    g.add_argument('--coco', required=True)
    g.add_argument('--cap', type=int, default=200)
    g.add_argument('--no-zero-annotation-images', action='store_true',
                   help='round 4: also fail when a train image carries no annotation (presence target must be 1 on every image)')
    d = sub.add_parser('done')
    d.add_argument('--root', required=True)
    d.add_argument('--run', required=True)
    latest = sub.add_parser('latest')
    latest.add_argument('--run-dir', required=True)
    r = sub.add_parser('record')
    r.add_argument('--root', required=True)
    r.add_argument('--run', required=True)
    r.add_argument('--coco', required=True)
    r.add_argument('--arm-rule', default='')
    r.add_argument('--label-target')
    r.add_argument('--policy')
    r.add_argument('--floor-px', type=int)
    r.add_argument('--cap', type=int, default=200)
    r.add_argument('--steps', type=int, default=3000)
    r.add_argument('--lr', type=float, default=8e-5)
    r.add_argument('--seed', type=int, default=1)
    r.add_argument('--save-every', type=int, default=1500)
    r.add_argument('--judge-mode', choices=('full', 'legacy'), default='full')
    r.add_argument('--ctrl')
    r.add_argument('--ref', action='append', default=[], metavar='NAME',
                   help='reference arm judged beside base / ctrl (repeatable; round 4: mixv2 floor12 combo twin); row gets NAME and paired_vs_NAME')
    r.add_argument('--against', default='ctrl', metavar='NAME',
                   help='arm the verdict is read against (default ctrl; when another arm, verdict_vs_ctrl keeps the ctrl read)')
    r.add_argument('--extra', help='JSON object merged into the row (e.g. {"phase": "phase1"})')
    sel = sub.add_parser('select-replicate')
    sel.add_argument('--root', required=True)
    sel.add_argument('--candidate', action='append', required=True, metavar='NAME', help='seed-1 arms in pre-registered order')
    sel.add_argument('--default', required=True, metavar='NAME')
    e = sub.add_parser('error')
    e.add_argument('--root', required=True)
    e.add_argument('--run', required=True)
    e.add_argument('--reason', required=True)
    e.add_argument('--coco')
    e.add_argument('--extra')
    args = parser.parse_args(argv)
    if args.mode == 'gate':
        result = fold_gate(args.coco, args.cap, args.no_zero_annotation_images)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['ok'] else 1
    if args.mode == 'done':
        return 0 if has_row(Path(args.root) / 'results.jsonl', args.run) else 1
    if args.mode == 'latest':
        path = latest_checkpoint(args.run_dir)
        if path:
            print(path)
        return 0
    if args.mode == 'record':
        results = Path(args.root) / 'results.jsonl'
        if has_row(results, args.run):
            print(json.dumps({'run': args.run, 'skipped': 'results row exists'}))
            return 0
        extra = _parse_extra(args.extra)
        row = build_row(args.root, args.run, args.coco, args.arm_rule, args.label_target, args.policy, args.floor_px, args.cap, args.steps,
                        args.lr, args.seed, args.save_every, args.judge_mode, args.ctrl, extra, refs=tuple(args.ref), against=args.against)
        gaps = judge_gaps(row['blocks'], row['calibration'], args.judge_mode)
        if gaps:   # a failed judge, calibration or post-filter must not finalise the arm: error row, exit 1, the re-run rebuilds the gaps
            reason = 'judge_missing:' + ','.join(gaps)
            err = error_row(args.root, args.run, reason, args.coco, {**(extra or {}), 'judge_missing': gaps})
            append_row(results, err)
            print(json.dumps({'run': err['run'], 'error': reason,
                              'note': 'no complete row written; the next waiter run rebuilds the missing judge blocks / views / calibration '
                                      'from trainable-final.pt'}))
            return 1
        append_row(results, row)
        print(json.dumps(headline(row), sort_keys=True))
        return 0
    if args.mode == 'select-replicate':
        result = select_replicate(read_rows(Path(args.root) / 'results.jsonl'), args.candidate, args.default)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.mode == 'error':
        row = error_row(args.root, args.run, args.reason, args.coco, _parse_extra(args.extra))
        append_row(Path(args.root) / 'results.jsonl', row)
        print(json.dumps({'run': row['run'], 'error': args.reason}))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
