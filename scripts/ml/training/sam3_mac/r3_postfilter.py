"""Rebuild a reference_eval summary at another score cut from its predictions.jsonl (sam3-mac-r3-postfilter-v1).

reference_eval collects every prediction above a low threshold (round 3: 0.2) with its overlaps against the references
and pseudo labels of its tile. A summary at any higher cut is then a pure re-read of the rows: the same strata, pairs,
negatives and shape agreement, no inference. context.json beside the rows carries the judged images and references.

  scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.r3_postfilter \
      --predictions <judge folder>/predictions.jsonl --threshold 0.45 --out <judge folder>-t045

Matched-density view (per-arm cuts, RULES.json 'calibration'): --calibration <run>/calibration.json cuts the base arm at
the rule's reference cut (0.45) and every other arm at its own val t* (--threshold must equal the primary arm's t*);
--arm-threshold arm=score sets the cuts by hand. When --out ends in '-matched' and neither is given, <run>/calibration.json
beside the judge folder is used (round3.sh passes the single t*). Rows below an arm's cut are dropped before the summary,
so pairs compare each arm at its own cut; the payload records arm_thresholds and their source, and its 'threshold' is the
lowest cut. A -matched summary without arm_thresholds was built before per-arm cuts existed: all arms at tuned's t*.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import reference_eval as ev

VERSION = 'sam3-mac-r3-postfilter-v1'


def read_jsonl(path):
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_folder(predictions_path):
    """(context, predictions, timings, source summary or None) from a judge folder."""
    predictions_path = Path(predictions_path)
    folder = predictions_path.parent
    with open(folder / 'context.json') as handle:
        context = json.load(handle)
    predictions = read_jsonl(predictions_path)
    timings = read_jsonl(folder / 'timings.jsonl') if (folder / 'timings.jsonl').exists() else []
    source = json.load(open(folder / 'summary.json')) if (folder / 'summary.json').exists() else None
    return context, predictions, timings, source


def parse_arm_thresholds(specs):
    out = {}
    for spec in specs or ():
        arm, _, value = spec.partition('=')
        try:
            out[arm] = float(value)
        except ValueError as error:
            raise SystemExit(f'--arm-threshold {spec!r}: expected arm=score') from error
    return out


def thresholds_from_calibration(calibration_path, arms, threshold):
    """Base at the rule's reference cut, every other arm at its own val t*; the primary arm's t* must be --threshold."""
    cal = json.load(open(calibration_path))
    rule = cal.get('rule') or {}
    base, ref_cut = rule.get('base_arm', 'base'), rule.get('reference_cut', .45)
    per_arm = cal.get('arms') or {}
    cuts, notes = {}, []
    for arm in arms:
        if arm == base:
            cuts[arm] = ref_cut
        elif (per_arm.get(arm) or {}).get('t_star') is not None:
            cuts[arm] = per_arm[arm]['t_star']
        else:
            cuts[arm] = threshold
            notes.append(f'{arm}: no t* in {Path(calibration_path).name}; cut at the view threshold {threshold}')
    primary = cal.get('primary_arm')
    if primary in cuts and abs(cuts[primary] - threshold) > 1e-9:
        raise SystemExit(f'--threshold {threshold} is not the calibration t* {cuts[primary]} of the primary arm {primary!r} ({calibration_path})')
    return cuts, notes


def postfilter(predictions_path, threshold, out=None, arm_thresholds=None, calibration=None):
    context, predictions, timings, source = load_folder(predictions_path)
    arms = context['arms']
    pairs = [tuple(p.split(':')) for p in context.get('pairs', [])]
    collected = (context.get('meta') or {}).get('rows_collected_at_threshold')
    meta = {**(context.get('meta') or {}), 'view_threshold': threshold, 'postfiltered_from': str(predictions_path),
            'postfilter_version': VERSION}
    source_note = None
    if not arm_thresholds and calibration is None and out is not None and Path(out).name.endswith('-matched'):
        candidate = Path(predictions_path).resolve().parents[1] / 'calibration.json'
        if candidate.exists():
            calibration, source_note = candidate, f'{candidate} (auto-discovered for the -matched view)'
    if calibration is not None:
        arm_thresholds, notes = thresholds_from_calibration(calibration, arms, threshold)
        meta.update({'arm_thresholds_source': source_note or str(calibration), 'arm_threshold_notes': notes})
    elif arm_thresholds:
        meta['arm_thresholds_source'] = '--arm-threshold'
    if arm_thresholds:
        unknown = sorted(set(arm_thresholds) - set(arms))
        if unknown:
            raise SystemExit(f'--arm-threshold names arms not in this judge folder: {unknown} (arms: {arms})')
        cuts = {arm: float(arm_thresholds.get(arm, threshold)) for arm in arms}
        threshold = min(cuts.values())
        meta.update({'view': 'matched_density', 'arm_thresholds': cuts, 'view_threshold': threshold,
                     'view_note': 'per-arm cuts: rows below an arm\'s cut were dropped before the summary, so pairs compare each arm at '
                                  'its own cut (base at its reference cut); "threshold" is the lowest cut'})
    if collected is not None and threshold < collected - 1e-9:
        raise SystemExit(f'threshold {threshold} is below the collection threshold {collected}: rows below it were never written')
    if arm_thresholds:
        predictions = [p for p in predictions if p['score'] > cuts.get(p['arm'], threshold)]
    payload = ev.payload_for(context, predictions, timings, arms, context.get('arm_specs', arms), pairs, threshold, meta)
    if source is not None:
        payload['source_version'] = source.get('version')
        payload['source_computed_at'] = source.get('computed_at')
    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / 'summary.json', 'w') as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--predictions', required=True, help='predictions.jsonl written by reference_eval (context.json beside it)')
    parser.add_argument('--threshold', type=float, required=True)
    parser.add_argument('--out', required=True, help='folder for the rebuilt summary.json')
    parser.add_argument('--arm-threshold', action='append', default=None, metavar='ARM=SCORE',
                        help='per-arm cut (repeatable); arms not named are cut at --threshold')
    parser.add_argument('--calibration', default=None,
                        help='<run>/calibration.json: base at its reference cut, other arms at their own t* (default for an --out ending in -matched)')
    args = parser.parse_args(argv)
    payload = postfilter(args.predictions, args.threshold, args.out, parse_arm_thresholds(args.arm_threshold), args.calibration)
    brief = {'version': payload['version'], 'threshold': payload['threshold'], 'arm_thresholds': payload.get('arm_thresholds'),
             'images': payload['images'], 'references': payload['references'],
             'arms': {a: {k: payload['summary'][a][k] for k in ('matched_at_iou_0_5', 'best_iou_median', 'boundary_distance_m_median_matched',
                                                                  'predictions_per_image_median')} for a in payload['arms']},
             'negatives': {a: {k: payload['summary']['negatives'][a][k] for k in ('predictions', 'novel', 'novel_no_footprint_share_of_predictions',
                                                                                   'predictions_per_ha', 'novel_per_ha')} for a in payload['arms']}}
    json.dump(brief, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
