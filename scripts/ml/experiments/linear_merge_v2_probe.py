"""Read-only probe of scene_extract.merge_linear_fragments over the recapture windows (linear-continuity run-v2, 2026-10-03).

Applies the authorised merge ("yes to defaults", DECISION-CARD.md item 1-3) to the model layer (latest.json objects) and the
review layer (review.json objects) of every window and writes one JSON per scene plus a summary under
data/reviews/eyes/experiments/linear-continuity-2026-10-03/run-v2/core-probe/. Nothing under data/scenes is written, no model
is loaded, no latest.json / review.json / confidence.* is touched. Counts are counts, never accuracy; nothing is promoted.

Run from the repo root:
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_merge_v2_probe            # all win-* / lot-* windows
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_merge_v2_probe --scenes win-67c56bcab05af7a4
"""
from __future__ import annotations

import argparse
import json
import warnings
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..scene_extract import LINEAR_MERGE, _metric_shape, merge_linear_fragments

ROOT = Path(__file__).resolve().parents[3]
SCENES = ROOT/'data/scenes'
OUT = ROOT/'data/reviews/eyes/experiments/linear-continuity-2026-10-03/run-v2/core-probe'
MARKS_WINDOWS = ['win-150d924743036f24', 'win-17b5c9b30a5d3ff9', 'win-da0e5d5badc3b3ba', 'win-d24efc00bcd6a3e4',
                 'win-fa2e955f73294188', 'win-1dff35d6922a4132', 'win-ab373041656385bf', 'win-4cc07d6908cc688b',
                 'win-67c56bcab05af7a4']
REVIEWER_SPECKS = {'scene': 'win-67c56bcab05af7a4', 'body': '7d6ee158367da22c',
                   'specks': ['cd6bf182a02a9af7', 'df8c00068f4d7373', 'd04a29a9bd4a5c7f']}


def window_ids(scenes_root=SCENES):
    return sorted(p.name for p in [*scenes_root.glob('win-*'), *scenes_root.glob('lot-*')]
                  if (p/'latest.json').exists() and not p.name.startswith('qa_'))


def probe_scene(scene_id, scenes_root=SCENES):
    latest = json.loads((scenes_root/scene_id/'latest.json').read_text())
    review_path = scenes_root/scene_id/'review.json'
    review = json.loads(review_path.read_text()) if review_path.exists() else None
    to_meters = latest['pixel_to_meters']
    layers = {'model': latest['objects'], **({'review': review['objects']} if review else {})}
    result = {'scene': scene_id, 'image_width': latest['image_width'], 'image_height': latest['image_height'],
              'pixel_to_meters': to_meters, 'model_signature': latest.get('model_signature'),
              'review_revision': review.get('revision') if review else None, 'layers': {}}
    for name, objects in layers.items():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)          # shapely 2.1 oriented_envelope noise, same as polygon_details
        merged, report = merge_linear_fragments(objects, to_meters)
        linear = [o for o in merged if o.get('kind') in LINEAR_MERGE['kinds']]
        result['layers'][name] = {'objects_in': len(objects), 'objects_out': len(merged), 'report': report,
                                  'linear_objects': linear,
                                  'status_in': dict(Counter(o.get('status') for o in objects if o.get('kind') in LINEAR_MERGE['kinds']))}
    return result


def speck_measurement(scenes_root=SCENES):
    """The reviewer's win-67c driveway: gaps between the body and each speck, in metres, with holes (the body has one)."""
    latest = json.loads((scenes_root/REVIEWER_SPECKS['scene']/'latest.json').read_text())
    affine = np.asarray(latest['pixel_to_meters'], dtype=float)
    by_id = {o['id']: o for o in latest['objects']}
    body = _metric_shape(by_id[REVIEWER_SPECKS['body']], affine)
    rows = []
    for speck_id in REVIEWER_SPECKS['specks']:
        shape = _metric_shape(by_id[speck_id], affine)
        rows.append({'id': speck_id, 'area_m2': round(shape.area, 3), 'gap_to_body_m': round(body.distance(shape), 3),
                     'inside_body_hole': bool(body.convex_hull.contains(shape) and not body.contains(shape)),
                     'within_d_driveway': body.distance(shape) <= LINEAR_MERGE['d_m']['driveway']})
    return {'body_id': REVIEWER_SPECKS['body'], 'body_area_m2': round(body.area, 3), 'body_holes': len(by_id[REVIEWER_SPECKS['body']].get('holes') or []),
            'd_driveway_m': LINEAR_MERGE['d_m']['driveway'], 'specks': rows}


def summarise(results):
    summary = {'version': 'linear-merge-v2-probe', 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
               'design': {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v) for k, v in LINEAR_MERGE.items()},
               'scenes': len(results), 'layers': {}, 'marks_windows': {}}
    for layer in ('model', 'review'):
        per_kind = {}
        for kind in LINEAR_MERGE['kinds']:
            totals = Counter()
            refusals = Counter()
            gaps = []
            for r in results:
                k = r['layers'].get(layer, {}).get('report', {}).get('kinds', {}).get(kind)
                if not k:
                    continue
                for key in ('pieces_in', 'candidates', 'features_out', 'multi_piece_features', 'pieces_superseded'):
                    totals[key] += k[key]
                totals['merges_step1'] += k['merges']['step1']
                totals['merges_step2'] += k['merges']['step2']
                totals['bridge_area_m2'] += k['bridge_area_m2']
                refusals.update(k['refusals'])
                gaps += [m['gap_m'] for m in r['layers'][layer]['report']['merges'] if m['kind'] == kind]
            gaps.sort()
            per_kind[kind] = {**{k: round(v, 3) for k, v in totals.items()}, 'refusals': dict(refusals),
                              'gaps_closed_m': {'n': len(gaps), 'median': round(float(np.median(gaps)), 3) if gaps else None,
                                                'p90': round(float(np.percentile(gaps, 90)), 3) if gaps else None,
                                                'max': gaps[-1] if gaps else None}}
        summary['layers'][layer] = per_kind
    for r in results:
        if r['scene'] in MARKS_WINDOWS:
            summary['marks_windows'][r['scene']] = {layer: {'multi_piece_features': sum(k['multi_piece_features'] for k in d['report']['kinds'].values()),
                                                            'merges': sum(k['merges']['step1']+k['merges']['step2'] for k in d['report']['kinds'].values()),
                                                            'refusals': dict(sum((Counter(k['refusals']) for k in d['report']['kinds'].values()), Counter()))}
                                                    for layer, d in r['layers'].items()}
    summary['vertex_cap'] = {'features_over_200_vertices': sum(1 for r in results for d in r['layers'].values()
                                                               for f in d['report']['features'] if f['vertices'] > 200),
                             'max_vertices': max((f['vertices'] for r in results for d in r['layers'].values() for f in d['report']['features']), default=0)}
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--scenes', nargs='*', help='scene ids (default: every win-* / lot-* window with a latest.json)')
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    ids = args.scenes or window_ids()
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for scene_id in ids:
        result = probe_scene(scene_id)
        (args.out/f'{scene_id}.json').write_text(json.dumps(result, indent=1))
        results.append(result)
    summary = summarise(results)
    summary['reviewer_specks_win_67c'] = speck_measurement() if REVIEWER_SPECKS['scene'] in ids else None
    (args.out/'summary.json').write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k not in ('design',)}, indent=1))
    print('written:', args.out/'summary.json', f'+ {len(results)} scene files')


if __name__ == '__main__':
    main()
