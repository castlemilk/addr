"""Held-out boundary evaluation against human references, with SAM3's own outlines as the control.

For every boundary reference in the test split (human_T1 / human_T2; lines-only rows keep SAM3's outline and are
excluded) the best-IoU predicted instance is compared with the reference: mask IoU, boundary F-score at 0.5 m,
and corner error after polygonisation with segmentation.mask_to_polygons (Hausdorff and per-reference-vertex
distance, metres). The same matching and metrics run on two controls read from the dataset's sam3_control.json:
`sam3_production` (latest.json ring, after the roof-surface refiner), `sam3_raw` (the detector ring retained in
boundary_refinement.raw_ring) and `sam3_regularised` (production rings through common.orthogonalise, i.e. the
`--target regularised` training labels, unchanged where its gates refuse). Controls are rasterised and
re-polygonised exactly like model masks.

Strata are reported apart with their own denominators: same-image vs cross-image references, and references whose
outline a person changed vs outlines confirmed as SAM3 drew them (the control scores ~0 there by construction).
Every number is reference agreement with a few human outlines, never accuracy.

Torch-free: `--onnx` evaluates an exported model with onnxruntime (main ML venv or training venv); train.py passes
its own torch predictor to evaluate_references().
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import box as shapely_box

from . import common, metrics

MATCH_MIN_IOU = .1
CONTROLS = ('sam3_production', 'sam3_raw', 'sam3_regularised')


def load_references(dataset, boundary_only=True):
    refs = common.read_json(Path(dataset) / 'references.json')['references']
    return [r for r in refs if r['boundary_reference'] or not boundary_only]


def control_predictions(dataset):
    """scene_id -> {control name: [(polygon, score)]} from sam3_control.json."""
    control = common.read_json(Path(dataset) / 'sam3_control.json')['images']
    out = {}
    for scene_id, rows in control.items():
        out[scene_id] = {
            'sam3_production': [(common.coco_to_polygon(r['production']), r.get('score')) for r in rows],
            'sam3_raw': [(common.coco_to_polygon(r['raw']), r.get('score')) for r in rows]}
    return out


def regularised_control(production, px_m):
    """The `--target regularised` labels applied to SAM3's production rings (unchanged where the gates refuse)."""
    return [(common.orthogonalise(p, px_m) or p, score) for p, score in production if p is not None]


def _crop_box(polygons, width, height, margin):
    minx = min(p.bounds[0] for p in polygons)
    miny = min(p.bounds[1] for p in polygons)
    maxx = max(p.bounds[2] for p in polygons)
    maxy = max(p.bounds[3] for p in polygons)
    x0, y0 = max(int(minx - margin), 0), max(int(miny - margin), 0)
    x1, y1 = min(int(maxx + margin) + 1, width), min(int(maxy + margin) + 1, height)
    return x0, y0, x1, y1


def _shift(polygon, dx, dy):
    from shapely import affinity
    return affinity.translate(polygon, -dx, -dy)


def match_and_score(reference, candidates, width, height, px_m):
    """Best-IoU candidate for one reference. candidates: list of (mask HxW bool | None, polygon | None, score)."""
    ref_polygon = common.coco_to_polygon(reference)
    margin = 4. / px_m + 4
    ref_box = shapely_box(*ref_polygon.bounds).buffer(margin)
    best, best_iou = None, 0.
    for mask, polygon, score in candidates:
        bounds = polygon.bounds if polygon is not None else _mask_bounds(mask)
        if bounds is None or not ref_box.intersects(shapely_box(*bounds)):
            continue
        region = [ref_polygon] + ([polygon] if polygon is not None else [shapely_box(*bounds)])
        x0, y0, x1, y1 = _crop_box(region, width, height, margin)
        size = (x1 - x0, y1 - y0)
        ref_mask = common.polygon_mask(_shift(ref_polygon, x0, y0), *size)
        pred = mask[y0:y1, x0:x1] if mask is not None else common.polygon_mask(_shift(polygon, x0, y0), *size)
        iou = metrics.mask_iou(pred, ref_mask) or 0.
        if iou > best_iou:
            best, best_iou = (pred, ref_mask, _shift(ref_polygon, x0, y0), score), iou
    if best is None or best_iou < MATCH_MIN_IOU:
        return {'matched': False, 'iou': None, 'score': None}
    pred, ref_mask, ref_local, score = best
    return {'matched': True, 'score': None if score is None else float(score), **metrics.evaluate_mask(pred, ref_mask, ref_local, px_m)}


def _mask_bounds(mask):
    if mask is None or not mask.any():
        return None
    ys, xs = np.nonzero(mask)
    return float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)


def evaluate_references(dataset, predict=None, *, name='model', include_controls=True, scene_ids=None, max_images=None):
    """Rows per (reference, method). predict(image PIL, reference_rows) -> list of (mask bool HxW, score)."""
    dataset = Path(dataset)
    refs = load_references(dataset)
    by_scene = {}
    for ref in refs:
        by_scene.setdefault(ref['scene_id'], []).append(ref)
    scenes = sorted(by_scene)
    if scene_ids:
        scenes = [s for s in scenes if s in set(scene_ids)]
    if max_images:
        scenes = scenes[:max_images]
    controls = control_predictions(dataset) if include_controls else {}
    rows, timings = [], []
    for scene_id in scenes:
        scene_refs = by_scene[scene_id]
        first = scene_refs[0]
        width, height, px_m = first['width'], first['height'], first['pixel_size_m']
        methods = {}
        if predict is not None:
            image = Image.open(dataset / first['file_name']).convert('RGB')
            started = time.monotonic()
            predictions = predict(image, scene_refs)
            timings.append(time.monotonic() - started)
            methods[name] = [(mask, None, score) for mask, score in predictions]
        if include_controls:
            scene_controls = dict(controls.get(scene_id, {}))
            scene_controls['sam3_regularised'] = regularised_control(scene_controls.get('sam3_production', []), px_m)
            for control in CONTROLS:
                methods[control] = [(None, polygon, score) for polygon, score in scene_controls.get(control, [])
                                    if polygon is not None]
        for ref in scene_refs:
            for method, candidates in methods.items():
                result = match_and_score(ref, candidates, width, height, px_m)
                rows.append({'method': method, 'scene_id': scene_id, 'object_id': ref['object_id'], 'ref_id': ref.get('ref_id'),
                             'provenance': ref['provenance'], 'outline_edited': ref['outline_edited'],
                             'image_identity': ref['image_identity'], 'pixel_size_m': px_m,
                             'model_hausdorff_m': ref.get('model_hausdorff_m'), 'edit_kind': ref.get('edit_kind'),
                             **result})
    return rows, {'n_images': len(scenes), 'predict_seconds_median': float(np.median(timings)) if timings else None}


STRATA = {
    'same_image_all': lambda r: r['image_identity'] == 'same_image',
    'same_image_outline_changed_by_human': lambda r: r['image_identity'] == 'same_image' and r['outline_edited'],
    'same_image_confirmed_as_drawn': lambda r: r['image_identity'] == 'same_image' and not r['outline_edited'],
    # How far the reviewer moved the outline from the model estimate (export_dataset model_hausdorff_m), so a score
    # on outlines the model already had right is reported apart from one on outlines a person moved.
    'same_image_outline_moved_ge_0_45_m': lambda r: r['image_identity'] == 'same_image'
    and r.get('model_hausdorff_m') is not None and r['model_hausdorff_m'] >= metrics.VERTEX_TOLERANCE_M,
    'same_image_outline_moved_lt_0_45_m': lambda r: r['image_identity'] == 'same_image'
    and r.get('model_hausdorff_m') is not None and r['model_hausdorff_m'] < metrics.VERTEX_TOLERANCE_M,
    'same_image_manual_outline': lambda r: r['image_identity'] == 'same_image' and r.get('edit_kind') == 'manual',
    'cross_image': lambda r: r['image_identity'] != 'same_image',
}


def summarise(rows):
    """Per method x stratum: denominators, match counts, metric medians; paired deltas vs each control."""
    methods = sorted({r['method'] for r in rows})
    out = {'note': common.NOTE, 'match_rule': f'best mask IoU per reference, IoU >= {MATCH_MIN_IOU}',
           'boundary_tolerance_m': metrics.BOUNDARY_TOLERANCE_M, 'vertex_tolerance_m': metrics.VERTEX_TOLERANCE_M,
           'polygoniser': f'segmentation.mask_to_polygons(simplify_px={metrics.SIMPLIFY_PX})', 'strata': {}}
    for stratum, keep in STRATA.items():
        chosen = [r for r in rows if keep(r)]
        block = {}
        for method in methods:
            mine = [r for r in chosen if r['method'] == method]
            matched = [r for r in mine if r['matched']]
            block[method] = {'n_references': len(mine), 'n_matched': len(matched), **metrics.summarise(matched)}
        for method in methods:
            baselines = [c for c in CONTROLS if c in methods and c != method]
            if method in CONTROLS:
                baselines = ['sam3_production'] if method != 'sam3_production' else []
            for control in baselines:
                pairs = _pairs(chosen, method, control)
                block[f'{method}_minus_{control}'] = {
                    key: metrics.paired_delta([a for a, _ in pairs], [b for _, b in pairs], key=key)
                    for key in ('hausdorff_m', 'vertex_distance_median_m', 'boundary_f', 'iou')}
        out['strata'][stratum] = block
    return out


def _pairs(rows, method, control):
    key = lambda r: (r['scene_id'], r['object_id'])  # noqa: E731
    left = {key(r): r for r in rows if r['method'] == method}
    right = {key(r): r for r in rows if r['method'] == control}
    return [(left[k], right[k]) for k in sorted(left) if k in right]


def write_report(out_dir, rows, summary, extra=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'test_rows.jsonl').write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    common.write_json_atomic(out_dir / 'test_summary.json', {**summary, **(extra or {})})
    return out_dir / 'test_summary.json'


def onnx_predictor(model_path, threshold=.45, max_proposals=200):
    from .infer import RoofOutlineSegmenter
    segmenter = RoofOutlineSegmenter(model_path)

    def predict(image, _refs):
        features = segmenter.encode_image(image)
        return [(p.mask, p.score) for p in segmenter.proposals(features, 'building roof', image.size,
                                                               threshold=threshold, max_proposals=max_proposals)]
    return predict, segmenter.model_identity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dataset', default=str(common.DEFAULT_OUT / 'dataset-v1'))
    parser.add_argument('--onnx', help='Exported model; omit for the SAM3 controls only.')
    parser.add_argument('--threshold', type=float, default=.45)
    parser.add_argument('--out', help='Folder for test_rows.jsonl and test_summary.json.')
    parser.add_argument('--max-images', type=int)
    args = parser.parse_args(argv)
    predict, identity = (onnx_predictor(args.onnx, args.threshold) if args.onnx else (None, None))
    rows, timing = evaluate_references(args.dataset, predict, max_images=args.max_images)
    summary = summarise(rows)
    extra = {'dataset': args.dataset, 'model_identity': identity, 'timing': timing, 'threshold': args.threshold,
             'dataset_manifest_sha256': common.sha256_file(Path(args.dataset) / 'manifest.json')}
    if args.out:
        write_report(args.out, rows, summary, extra)
    brief = {s: {m: {k: v for k, v in block[m].items() if k in ('n_references', 'n_matched', 'hausdorff_m', 'boundary_f',
                                                                  'n_hausdorff_le_0_45_m', 'n_hausdorff')}
                 for m in block if 'minus' not in m} for s, block in summary['strata'].items()}
    json.dump({'strata': brief, **extra}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
