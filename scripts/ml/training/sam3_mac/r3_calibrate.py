"""Matched-density threshold t* for a tuned arm from the VALIDATION block only (sam3-mac-r3-calibrate-v1).

Pre-registered rule (RULES.json 'calibration'): for each non-base arm choose the smallest t in the grid such that the
arm's predictions per hectare on val at t are at most density_ratio_max x base's predictions per hectare at the
reference cut (0.45), AND the arm's pseudo-label recall at IoU 0.5 on val at t is at least base's at the reference cut
minus recall_slack. If no t qualifies, t* is the top of the grid, flagged 'density_unmatched'. Inputs are prediction
counts and pseudo-label agreement (production SAM3 outputs); no human reference is touched. The single t* is then
applied to the test blocks by r3_postfilter. t* is a fairness device for comparing arms inside the fork's
presence-weighted judge; production ONNX cuts the sigmoid at 0.45; no production claim follows.

RULES.json 'calibration.val_adjacent_to_train' names runs whose training tiles neighbour val tiles (mixv2: dataset-v2 val is
the spatial buffer of the tile_group split, so pseudo-label recall on the Mooroolbark val tiles is agreement with labels the
arm trained on next door). For such a run t* is chosen on the val images outside the excluded scene prefix (the 13
non-Mooroolbark images), flagged val_adjacent_to_train / calibrated_on_val_subset, and the full-val table is kept as full_val.
The run is read from the --val path (<run>/reference-eval-v2val/predictions.jsonl) or --run. No inference is involved.

  scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.r3_calibrate \
      --rule <round3>/RULES.json --val <run>/reference-eval-v2val/predictions.jsonl --out <run>/calibration.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import reference_eval as ev

VERSION = 'sam3-mac-r3-calibrate-v1'
DEFAULT_RULE = {'grid': [round(.45 + .05 * k, 2) for k in range(8)], 'reference_cut': .45, 'density_ratio_max': 1.10,
                'recall_slack': .02, 'fallback': 'top_of_grid', 'flag': 'density_unmatched', 'base_arm': 'base',
                'inputs': ['predictions per hectare', 'pseudo-label recall at IoU 0.5'], 'human_references_used': False,
                'val_adjacent_to_train': {}}


def read_jsonl(path):
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def density_and_recall(context, predictions, arm, threshold):
    preds = [p for p in ev.select(predictions, threshold) if p['arm'] == arm]
    negatives = ev.negatives_for(context, preds)
    return {'threshold': threshold, 'predictions': negatives['predictions'], 'predictions_per_ha': negatives['predictions_per_ha'],
            'pseudo_recall_at_0_5': negatives['pseudo_recall_at_0_5'], 'novel_per_ha': negatives['novel_per_ha'],
            'novel_no_footprint_share_of_predictions': negatives['novel_no_footprint_share_of_predictions'],
            'covered_tiles_unbacked_share': negatives['covered_tiles']['novel_no_footprint_share_of_predictions']}


def calibrate_arm(context, predictions, arm, base_row, rule):
    table, chosen, flags = [], None, []
    for t in rule['grid']:
        row = density_and_recall(context, predictions, arm, t)
        density_ok = (row['predictions_per_ha'] is not None and base_row['predictions_per_ha'] is not None
                      and row['predictions_per_ha'] <= rule['density_ratio_max'] * base_row['predictions_per_ha'] + 1e-12)
        recall_ok = (row['pseudo_recall_at_0_5'] is None or base_row['pseudo_recall_at_0_5'] is None
                     or row['pseudo_recall_at_0_5'] >= base_row['pseudo_recall_at_0_5'] - rule['recall_slack'] - 1e-12)
        row.update({'density_ok': density_ok, 'recall_ok': recall_ok, 'qualifies': density_ok and recall_ok})
        table.append(row)
        if chosen is None and row['qualifies']:
            chosen = t
    if chosen is None:
        chosen = rule['grid'][-1]
        flags.append(rule['flag'])
    if base_row['pseudo_recall_at_0_5'] is None:
        flags.append('no_pseudo_labels_on_val')
    return {'t_star': chosen, 'flags': flags, 'table': table}


def run_name_for(val_predictions, run=None):
    """The run folder owning <run>/reference-eval-v2val/predictions.jsonl, or the explicit --run."""
    if run:
        return run
    path = Path(val_predictions).resolve()
    return path.parents[1].name if len(path.parents) > 1 else None


def restrict(context, predictions, exclude_prefix):
    """The val block without the images whose scene_id starts with exclude_prefix (a pure re-read of the rows)."""
    keep = {iid for iid, image in context['images'].items() if not str(image.get('scene_id', '')).startswith(exclude_prefix)}
    sub = {**context, 'images': {iid: context['images'][iid] for iid in keep},
           'refs': {k: r for k, r in (context.get('refs') or {}).items() if str(r['image_id']) in keep}}
    return sub, [p for p in predictions if str(p['image_id']) in keep]


def calibrate_block(context, predictions, arms, rule):
    base = rule['base_arm']
    base_row = density_and_recall(context, predictions, base, rule['reference_cut'])
    per_arm = {arm: calibrate_arm(context, predictions, arm, base_row, rule) for arm in arms if arm != base}
    primary = 'tuned' if 'tuned' in per_arm else (next(iter(per_arm)) if per_arm else None)
    return {'images': len(context['images']), 'pseudo_labels': sum(len(i['pseudo_ids']) for i in context['images'].values()),
            'human_references_on_val': len(context['refs']), 'base_reference': base_row, 'arms': per_arm, 'primary_arm': primary,
            't_star': per_arm[primary]['t_star'] if primary else None, 'flags': per_arm[primary]['flags'] if primary else ['no_tuned_arm']}


def calibrate(rule_path, val_predictions, out=None, run=None):
    rule_path = Path(rule_path) if rule_path else None
    rules = json.load(open(rule_path)) if rule_path and rule_path.exists() else {}
    rule = {**DEFAULT_RULE, **(rules.get('calibration') or {})}
    context, predictions, _, _ = _load(val_predictions)
    arms = context['arms']
    base = rule['base_arm']
    if base not in arms:
        raise SystemExit(f'base arm {base!r} not in the val block arms {arms}')
    full = calibrate_block(context, predictions, arms, rule)
    payload = {'version': VERSION, 'computed_at': ev.now_iso(), 'rule': rule,
               'rules_sha256': hashlib.sha256(rule_path.read_bytes()).hexdigest() if rule_path and rule_path.exists() else None,
               'val_predictions': str(val_predictions), 'dataset': context['dataset'], 'split': context['split'], **full,
               'note': ('val only; inputs are prediction counts and pseudo-label agreement (model outputs), no human reference; t* is a fairness '
                        'device inside the fork\'s presence-weighted judge, production ONNX cuts the sigmoid at 0.45, no production claim follows; '
                        'unbacked novel shares are descriptive, not rule inputs')}
    run_name = run_name_for(val_predictions, run)
    adjacency = (rule.get('val_adjacent_to_train') or {}).get(run_name) if run_name else None
    if adjacency:   # this run trained next door to part of val: choose t* on the val images outside the excluded prefix
        prefix = adjacency.get('exclude_scene_prefix', 'mooroolbark-')
        sub_context, sub_predictions = restrict(context, predictions, prefix)
        subset = calibrate_block(sub_context, sub_predictions, arms, rule)
        payload['full_val'] = {**full, 'note': 'all val images, for reference only; the Mooroolbark val tiles neighbour this arm\'s training tiles'}
        payload.update(subset)
        payload['flags'] = subset['flags'] + [adjacency.get('flag', 'val_adjacent_to_train'), 'calibrated_on_val_subset']
        payload['val_subset'] = {'run': run_name, 'excluded_scene_prefix': prefix, 'images_used': len(sub_context['images']),
                                 'images_excluded': len(context['images']) - len(sub_context['images']), 'reason': adjacency.get('reason'),
                                 'scene_ids_used': sorted(str(i.get('scene_id')) for i in sub_context['images'].values())}
    if context['split'] != 'val':
        payload['flags'] = payload['flags'] + ['calibrated_on_non_val_split']
    if out is not None:
        with open(out, 'w') as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
    return payload


def _load(predictions_path):
    from .r3_postfilter import load_folder
    return load_folder(predictions_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rule', required=True, help='RULES.json (its "calibration" block; built-in defaults when absent)')
    parser.add_argument('--val', required=True, help='predictions.jsonl of the dataset-v2 VAL judge block')
    parser.add_argument('--out', required=True)
    parser.add_argument('--run', default=None, help='run name for the val_adjacent_to_train rule (default: the folder owning --val)')
    args = parser.parse_args(argv)
    payload = calibrate(args.rule, args.val, args.out, args.run)
    json.dump({k: payload[k] for k in ('version', 't_star', 'flags', 'primary_arm', 'base_reference')}, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
