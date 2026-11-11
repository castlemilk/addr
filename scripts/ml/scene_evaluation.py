"""Fixed-threshold outline diagnostics against checked scene references.

This is not COCO AP, a new-model benchmark, or 3D accuracy. It evaluates saved
pipeline snapshots using continuous polygon IoU, including interior gaps.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment
from shapely.errors import ShapelyError
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

IOU_THRESHOLD = .5


def match_instances(ious, threshold=IOU_THRESHOLD):
    """Maximise match count first, then total IoU; never reuse an instance."""
    ious = np.asarray(ious, dtype=float)
    if not ious.size:
        return []
    # One extra valid match outweighs every possible change in summed IoU.
    weights = np.where(ious >= threshold, min(ious.shape) + 1 + ious, 0)
    left, right = linear_sum_assignment(weights, maximize=True)
    return [(int(i), int(j)) for i, j in zip(left, right, strict=True) if ious[i, j] >= threshold]


def polygon(obj, *, reference=False):
    try:
        shape = Polygon(obj['ring'], obj.get('holes'))
        if shape.is_valid and not shape.is_empty and np.isfinite(shape.area) and shape.area > 0:
            return shape
    except (ValueError, TypeError, KeyError, ShapelyError):
        pass
    if reference:
        raise ValueError(f'Invalid evaluation reference {obj.get("id")}')
    return None


def ratio(a, b):
    return round(a / b, 6) if b else None


def evaluate_objects(predictions, references, *, threshold=IOU_THRESHOLD):
    predictions, references = [sorted(items, key=lambda o: o['id']) for items in (predictions, references)]
    proposed = [polygon(o) for o in predictions]
    checked = [polygon(o, reference=True) for o in references]
    ious = np.zeros((len(proposed), len(checked)))
    reference_index = STRtree(checked)
    for i, p in enumerate(proposed):
        if p is None:
            continue
        for j in reference_index.query(p, predicate='intersects'):
            r = checked[j]
            intersection = p.intersection(r).area
            ious[i, j] = intersection / (p.area + r.area - intersection)
    pairs = match_instances(ious, threshold)
    pids, rids = {i for i, _ in pairs}, {j for _, j in pairs}
    p_union, r_union = unary_union([p for p in proposed if p is not None]), unary_union(checked)
    intersection = p_union.intersection(r_union).area
    union = p_union.union(r_union).area
    matched = len(pairs)
    return {
        'predictions': len(proposed), 'references': len(checked), 'matched': matched,
        'extra': len(proposed) - matched, 'missed': len(checked) - matched,
        'invalid_predictions': sum(p is None for p in proposed),
        'precision': ratio(matched, len(proposed)), 'recall': ratio(matched, len(checked)),
        'outline_iou': ratio(intersection, union),
        'intersection_area_px': intersection, 'union_area_px': union,
        'matches': [{'prediction_id': predictions[i]['id'], 'reference_id': references[j]['id'],
                     'iou': round(float(ious[i, j]), 6)} for i, j in pairs],
        'extra_ids': [o['id'] for i, o in enumerate(predictions) if i not in pids],
        'missed_ids': [o['id'] for j, o in enumerate(references) if j not in rids],
    }


def summarize(layers):
    result = {key: sum(layer[key] for layer in layers) for key in
              ('predictions', 'references', 'matched', 'extra', 'missed', 'invalid_predictions')}
    measured = [layer['outline_iou'] for layer in layers if layer['outline_iou'] is not None]
    result.update(images=len(layers), groups=len({layer['spatial_group'] for layer in layers}),
                  empty_images=sum(layer['references'] == 0 for layer in layers),
                  precision=ratio(result['matched'], result['predictions']),
                  recall=ratio(result['matched'], result['references']),
                  mean_outline_iou=ratio(sum(measured), len(measured)))
    return result


def evaluate_dataset(encoded, coverage):
    rows = {row['scene_id']: row for row in map(json.loads, filter(str.strip, encoded.splitlines()))}
    classes, skipped = [], []
    for category in coverage['classes']:
        layers = []
        for layer in category['layers']:
            reasons = list(layer['blockers'])
            if layer['scan'] != 'complete':
                reasons.append('not_scanned')
            if reasons:
                skipped.append({'scene_id': layer['scene_id'], 'kind': category['kind'], 'reasons': reasons})
                continue
            row = rows[layer['scene_id']]
            prediction = [o for o in row['prediction'] if o['kind'] == category['kind']]
            reference = [o for o in row['reference'] if o['kind'] == category['kind']]
            metrics = evaluate_objects(prediction, reference)
            layers.append({**metrics, 'scene_id': layer['scene_id'], 'title': layer['title'],
                           'split': layer['split'], 'spatial_group': layer['spatial_group'],
                           'review_url': layer['review_url'], 'revision': layer['revision'],
                           'model_signature': row['model_signature'], 'image_sha256': row['image_sha256'],
                           'sources': sorted({o.get('source', 'unknown') for o in prediction})})
        classes.append({'kind': category['kind'], 'layers': layers,
                        'splits': {s: summarize([layer for layer in layers if layer['split'] == s])
                                   for s in ('train', 'validation', 'test')}})
    return {'schema_version': 1, 'dataset_version': coverage['version'], 'iou_threshold': IOU_THRESHOLD,
            'scope': 'Saved pipeline estimates against reviewed visible outlines; not independent ground truth or 3D accuracy.',
            'matching': 'Maximum one-to-one matches at polygon IoU >= 0.5, then maximum summed IoU. No confidence sweep or COCO AP.',
            'overlap': 'Mean per-image union outline IoU; empty/empty images are undefined and excluded from the mean.',
            'classes': classes, 'skipped': skipped}


def main():
    from .scene_training import collect_scenes
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    encoded, _, coverage = collect_scenes(args.root)
    print(json.dumps(evaluate_dataset(encoded, coverage), indent=2))


if __name__ == '__main__':
    main()
