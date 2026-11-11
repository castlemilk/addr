"""Judge SAM3 checkpoints against the human roof references of a dataset split (sam3-mac-reference-eval-v2).

Runs the PyTorch SAM3 (base checkpoint, optionally with a fine-tuned trainable-*.pt applied) on every judged image of
--dataset/--split with the production prompt and reports, per human boundary reference: the best IoU among predicted
masks, whether it is matched at IoU >= 0.5, and the mean boundary distance of the matched prediction in metres.
Arms are compared pairwise (--pair second:first) with a multiplicity-weighted image bootstrap. Agreement with the
listed human outlines, not calibrated accuracy; predictions are model estimates; footprint backing is agreement with
another model (Microsoft footprints are ML outputs); pseudo labels are production SAM3 outputs.

Round 3 shape: every prediction above --threshold is written to predictions.jsonl with its score, area, kind (human /
pseudo_only / novel), footprint backing and its IoU against every overlapping reference and pseudo label, so any
higher threshold can be re-read from the rows without inference (r3_postfilter.py). summary.json is the view at the
run threshold; the waiter derives the 0.45 and matched-density views from the rows. A split with no human outline
(dataset-v2 val) is judged on every image for predictions, pseudo agreement and footprints only.

  scripts/ml/training/.venv-sam3/bin/python -m scripts.ml.training.sam3_mac.reference_eval \
      --dataset data/reviews/eyes/training/dataset-v2 --split test --arm base \
      --arm tuned=data/reviews/eyes/training/sam3-mac/round3/r3-ctrl-sq-c200-seed1/trainable-final.pt \
      --pair tuned:base --threshold 0.2 --footprints data/elevation/derived/ms-buildings/bbox.geojsonl.gz
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC
from pathlib import Path

import numpy as np
from PIL import Image

from . import smoke_infer as smoke

VERSION = 'sam3-mac-reference-eval-v2'
REPO = smoke.REPO
DEFAULT_DATASET = REPO / 'data/reviews/eyes/training/dataset-v1'
OUT = REPO / 'data/reviews/eyes/training/sam3-mac/reference-eval'
FOOTPRINTS = REPO / 'data/elevation/derived/ms-buildings/bbox.geojsonl.gz'   # Microsoft footprints, CDLA-Permissive-2.0, ML outputs
SCENES = REPO / 'data/scenes'
EDIT_DIFFS = REPO / 'data/reviews/eyes/edit-diffs/2026-09-29/rows.jsonl'
MATCH_IOU = .5
NOVEL_IOU = .3     # a prediction overlapping no human outline and no pseudo label at this IoU is a novel detection
OVERLAP_MIN = .05  # per-prediction overlaps below this IoU are not written to predictions.jsonl
PRODUCTION_CUT = .45
BOOT_SEED, BOOT_DRAWS = 20260928, 2000
EDIT_MOVE_M = .45
AREA_BANDS = (('lt_25', 0., 25.), ('25_50', 25., 50.), ('50_150', 50., 150.), ('150_300', 150., 300.), ('ge_300', 300., math.inf))
PIXEL_BANDS = (('le_0_08', 0., .08), ('0_08_0_15', .08, .15), ('0_15_0_23', .15, .23), ('0_236', .23, math.inf))
NOVEL_AREA_BINS = (('lt_12', 0., 12.), ('12_25', 12., 25.), ('25_50', 25., 50.), ('50_150', 50., 150.), ('ge_150', 150., math.inf))
THRESHOLD_NOTE = ('the judge threshold cuts the fork\'s presence-weighted score (sigmoid(logit) x sigmoid(presence)); production ONNX '
                  'cuts the sigmoid alone at 0.45, so no threshold value transfers 1:1 between the two')


def now_iso():
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec='seconds')


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def band_of(value, bands):
    if value is None:
        return None
    for name, low, high in bands:
        if low <= value < high:
            return name
    return bands[-1][0]


def _median(values, digits=4):
    values = [v for v in values if v is not None and np.isfinite(v)]
    return round(float(np.median(values)), digits) if values else None


# --- dataset -----------------------------------------------------------------------------------------------------------

def load_split(dataset, split):
    with open(Path(dataset) / f'{split}.json') as handle:
        return json.load(handle)


def human_references(dataset=DEFAULT_DATASET, split='test', all_images=None):
    """[(image row, [reference annotations], [machine pseudo labels])] for the judged images of a split.

    Judged images are those carrying a human boundary reference; when the split carries none (dataset-v2 val) every
    image is judged with zero references. The pseudo labels are the production SAM3's own outputs on the same tiles;
    they are not references, but a prediction that matches neither a human outline nor a pseudo label is a novel
    detection worth counting."""
    coco = load_split(dataset, split)
    images = {img['id']: img for img in coco['images']}
    refs, pseudo = {}, {}
    for ann in coco['annotations']:
        if str(ann.get('provenance', '')).startswith('human') and ann.get('boundary_reference', True):
            refs.setdefault(ann['image_id'], []).append(ann)
        elif ann.get('provenance') == 'machine_pseudo':
            pseudo.setdefault(ann['image_id'], []).append(ann)
    if all_images is None:
        all_images = not refs
    ids = sorted(images) if all_images else sorted(refs)
    return [(images[i], refs.get(i, []), pseudo.get(i, [])) for i in ids]


def ref_key(image_row, ref):
    return f"{image_row['id']}:{ref['id']}"


def polygon_mask(segmentation, width, height):
    from PIL import ImageDraw
    canvas = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(canvas)
    for ring in segmentation:
        pts = list(zip(ring[0::2], ring[1::2], strict=True))
        if len(pts) >= 3:
            draw.polygon(pts, fill=1)
    return np.asarray(canvas, dtype=bool)


def mask_box(mask):
    ys, xs = np.nonzero(mask.any(axis=1))[0], np.nonzero(mask.any(axis=0))[0]
    if not len(ys):
        return None
    return (int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1)


def boxes_touch(a, b):
    return a is not None and b is not None and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def iou_boxed(mask_a, box_a, mask_b, box_b):
    if not boxes_touch(box_a, box_b):
        return 0.
    x0, y0 = min(box_a[0], box_b[0]), min(box_a[1], box_b[1])
    x1, y1 = max(box_a[2], box_b[2]), max(box_a[3], box_b[3])
    a, b = mask_a[y0:y1, x0:x1], mask_b[y0:y1, x0:x1]
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter / union) if union else 0.


def edit_diffs(path=EDIT_DIFFS):
    """{(scene_id, object_id): row} of the reviewer's building edits (human vs model outline agreement)."""
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for line in open(path):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get('kind') == 'building' and row.get('scene_id') and row.get('object_id'):
            out[(row['scene_id'], row['object_id'])] = row
    return out


def edit_diff_for(ref, diffs):
    if not diffs:
        return None
    oid = str(ref.get('object_id') or '')
    for candidate in (oid, oid.removeprefix('ref-'), str(ref.get('ref_id') or '')):
        row = diffs.get((ref.get('scene_id'), candidate)) if candidate else None
        if row is not None:
            return {'hausdorff_m': row.get('hausdorff_m'), 'iou': row.get('iou'), 'boundary_distance_median_m': row.get('boundary_distance_median_m'),
                    'human_vertex_count': row.get('human_vertex_count'), 'model_vertex_count': row.get('model_vertex_count'),
                    'stratum': ('moved_ge_0_45' if (row.get('hausdorff_m') or 0.) >= EDIT_MOVE_M else 'moved_lt_0_45')}
    return None


# --- geometry helpers ----------------------------------------------------------------------------------------------------

def boundary_distance_m(poly_a, poly_b, px_m):
    """Symmetric mean distance between the two polygons' exterior rings, metres (None when either is missing)."""
    sys.path.insert(0, str(REPO))
    from shapely import affinity

    from scripts.ml.training.hires_teacher import mean_boundary_distance
    if poly_a is None or poly_b is None:
        return None
    a = affinity.affine_transform(poly_a, [px_m, 0., 0., px_m, 0., 0.])
    b = affinity.affine_transform(poly_b, [px_m, 0., 0., px_m, 0., 0.])
    return mean_boundary_distance(a, b)


def _mask_polygon(mask, simplify_px=.9, box=None):
    """Largest polygon of a mask after the production 0.9 px simplification (scene_extract vectoriser).

    Vectorised on the mask's bounding box (1 px margin) and translated back, which is the same polygon as on the
    full canvas but far cheaper at a low collection threshold (about 100 predictions per 1152 px tile)."""
    sys.path.insert(0, str(REPO))
    from shapely import affinity

    from scripts.ml.segmentation import mask_to_polygons
    box = box or mask_box(mask)
    if box is None:
        return None
    h, w = mask.shape
    x0, y0 = max(0, box[0] - 1), max(0, box[1] - 1)
    x1, y1 = min(w, box[2] + 1), min(h, box[3] + 1)
    crop = np.ascontiguousarray(mask[y0:y1, x0:x1]).astype(np.uint8)
    polys = mask_to_polygons(crop, min_area_px=1., simplify_px=simplify_px)
    if not polys:
        return None
    return affinity.translate(max(polys, key=lambda p: p.area), x0, y0)


def _ref_polygon(ref):
    from shapely.geometry import Polygon
    ring = ref['segmentation'][0]
    pts = list(zip(ring[0::2], ring[1::2], strict=True))
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    if poly.geom_type != 'Polygon':
        poly = max(poly.geoms, key=lambda p: p.area)
    return poly


def shape_of(polygon):
    """Vertex count and right-angle share (corners turning >= 30 degrees within 10 of 90) of a pixel polygon."""
    if polygon is None:
        return {'vertices': None, 'corners': None, 'right_corners': None, 'right_angle_share': None}
    sys.path.insert(0, str(REPO))
    from scripts.ml.training.hires_teacher import corner_stats
    stats = corner_stats(polygon)
    return {'vertices': len(polygon.exterior.coords) - 1, 'corners': stats['corners'], 'right_corners': stats['right'],
            'right_angle_share': round(stats['right'] / stats['corners'], 4) if stats['corners'] else None}


# --- footprints ----------------------------------------------------------------------------------------------------------

def load_footprints(paths=(FOOTPRINTS,)):
    """(footprint polygons in lon/lat, {path: sha256}) from the cached Microsoft files (union); absent files recorded as None."""
    import gzip

    from shapely.geometry import shape
    polys, shas = [], {}
    for path in paths:
        path = Path(path)
        if not path.exists():
            shas[str(path)] = None
            continue
        shas[str(path)] = sha256_file(path)
        with gzip.open(path, 'rt') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                feature = json.loads(line)
                geometry = feature.get('geometry') or feature
                try:
                    geom = shape(geometry)
                except Exception:  # noqa: BLE001 - skip malformed rows
                    continue
                if not geom.is_empty:
                    polys.append(geom)
    return polys, shas


def footprints_in_tile(footprints, scene_id, scenes=SCENES):
    """Footprints projected into a saved tile's pixel frame through the scene's own Mercator georeference."""
    from shapely.geometry import Polygon
    from shapely.geometry import box as shapely_box
    from shapely.strtree import STRtree
    sys.path.insert(0, str(REPO))
    from scripts.ml.training import common
    path = Path(scenes) / str(scene_id) / 'latest.json'
    if not scene_id or not path.exists() or not footprints:
        return [], None
    scene = json.load(open(path))
    width, height = scene.get('image_width'), scene.get('image_height')
    bounds = (scene.get('region') or {}).get('bounds')
    tile = Polygon([(0, 0), (width, 0), (width, height), (0, height)])
    candidates = footprints
    if bounds:
        pad = .001
        region = shapely_box(bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)
        candidates = [fp for fp in footprints if fp.intersects(region)]
    out = []
    for fp in candidates:
        rings = [fp.exterior] if fp.geom_type == 'Polygon' else [g.exterior for g in getattr(fp, 'geoms', [])]
        for ring in rings:
            px = common.lonlat_to_pixel(list(ring.coords), scene)
            poly = Polygon(px)
            if poly.is_valid and not poly.is_empty and poly.intersects(tile):
                out.append(poly)
    return out, (STRtree(out) if out else None)


def footprint_backed(poly, fp_polys, tree, min_overlap=.5):
    """True when at least min_overlap of the prediction polygon lies inside some footprint; None without footprints."""
    if tree is None or poly is None or poly.area <= 0:
        return None
    for i in tree.query(poly):
        fp = fp_polys[i]
        if poly.intersection(fp).area / poly.area >= min_overlap:
            return True
    return False


# --- inference per arm -----------------------------------------------------------------------------------------------------

def image_hectares(image_row, px_m):
    return image_row['width'] * px_m * image_row['height'] * px_m / 10000.


def evaluate_arm(name, weights, trainable, device, dtype, rows_images, prompt, footprints=None, threshold=None, dataset=DEFAULT_DATASET,
                 fp_cache=None, ref_polys=None, scenes=SCENES):
    """Run one arm over the judged images. Returns (prediction rows, per-image timing rows, model load seconds).

    Every prediction above the processor threshold becomes one row with its overlaps against the image's references
    and pseudo labels, so summaries at any higher threshold are a pure re-read of the rows."""
    model, processor, load_s = smoke.load_torch_model(weights, device, dtype, trainable)
    if threshold is not None:
        processor.confidence_threshold = float(threshold)      # presence-weighted score cut; production uses 0.45 on the sigmoid alone
    fp_cache = {} if fp_cache is None else fp_cache
    ref_polys = {} if ref_polys is None else ref_polys
    predictions, timings = [], []
    for image_row, refs, pseudo in rows_images:
        image = Image.open(Path(dataset) / image_row['file_name']).convert('RGB')
        masks, scores, timing = smoke.run_torch(processor, image, prompt, device, dtype)
        px_m = image_row.get('pixel_size_m') or .236
        ha = image_hectares(image_row, px_m)
        width, height = image_row['width'], image_row['height']
        ref_masks = [(ref_key(image_row, r), polygon_mask(r['segmentation'], width, height)) for r in refs]
        ref_masks = [(k, m, mask_box(m)) for k, m in ref_masks]
        for r in refs:
            ref_polys.setdefault(ref_key(image_row, r), _ref_polygon(r))
        pseudo_masks = [(a['id'], polygon_mask(a['segmentation'], width, height)) for a in pseudo]
        pseudo_masks = [(i, m, mask_box(m)) for i, m in pseudo_masks]
        scene_id = image_row.get('scene_id')
        if footprints is not None and scene_id not in fp_cache:
            fp_cache[scene_id] = footprints_in_tile(footprints, scene_id, scenes)
        fp_polys, fp_tree = fp_cache.get(scene_id, ([], None))
        for k, (mask, score) in enumerate(zip(masks, scores, strict=True)):
            box = mask_box(mask)
            if box is None:
                continue
            area_px = int(mask.sum())
            poly = _mask_polygon(mask, box=box)
            shape = shape_of(poly)
            overlaps = []
            for key, rmask, rbox in ref_masks:
                v = iou_boxed(mask, box, rmask, rbox)
                if v >= OVERLAP_MIN:
                    entry = {'ref': key, 'iou': round(v, 4)}
                    if v >= MATCH_IOU:
                        rpoly = ref_polys.get(key)
                        dist = boundary_distance_m(rpoly, poly, px_m)
                        entry['boundary_m'] = None if dist is None else round(dist, 3)
                        entry['area_ratio'] = round(poly.area / rpoly.area, 4) if (poly is not None and rpoly is not None and rpoly.area) else None
                    overlaps.append(entry)
            pseudo_overlaps = [{'id': pid, 'iou': round(v, 4)} for pid, pmask, pbox in pseudo_masks
                               for v in [iou_boxed(mask, box, pmask, pbox)] if v >= OVERLAP_MIN]
            if any(o['iou'] >= NOVEL_IOU for o in overlaps):
                kind = 'human'
            elif any(o['iou'] >= NOVEL_IOU for o in pseudo_overlaps):
                kind = 'pseudo_only'
            else:
                kind = 'novel'
            best = max(overlaps, key=lambda o: o['iou']) if overlaps else None
            predictions.append({'arm': name, 'image_id': image_row['id'], 'scene_id': scene_id, 'index': k, 'pixel_size_m': round(px_m, 6),
                                'image_ha': round(ha, 4), 'score': round(float(score), 4), 'area_px': area_px,
                                'area_m2': round(area_px * px_m * px_m, 2), 'kind': kind,
                                'footprint_backed': footprint_backed(poly, fp_polys, fp_tree) if footprints is not None else None,
                                'matched_ref_id': best['ref'] if best and best['iou'] >= MATCH_IOU else None,
                                'best_iou': best['iou'] if best else 0., 'ref_overlaps': overlaps, 'pseudo_overlaps': pseudo_overlaps,
                                **shape})
        timings.append({'arm': name, 'image_id': image_row['id'], 'predictions': len(masks), 'seconds': timing['total_s'],
                        'footprints_in_tile': len(fp_polys)})
    return predictions, timings, load_s


def build_context(dataset, split, rows_images, fp_cache, ref_polys, diffs):
    """Everything a post-filter needs beside the prediction rows: judged images, references and their strata."""
    images, refs = {}, {}
    for image_row, ref_rows, pseudo in rows_images:
        px_m = image_row.get('pixel_size_m') or .236
        fp_polys, _ = fp_cache.get(image_row.get('scene_id'), ([], None))
        images[str(image_row['id'])] = {'scene_id': image_row.get('scene_id'), 'pixel_size_m': round(px_m, 6), 'width': image_row['width'],
                                        'height': image_row['height'], 'hectares': round(image_hectares(image_row, px_m), 4),
                                        'pixel_band': band_of(px_m, PIXEL_BANDS), 'pseudo_ids': [a['id'] for a in pseudo],
                                        'footprints_in_tile': len(fp_polys), 'references': len(ref_rows), 'tile_group': image_row.get('tile_group')}
        for ref in ref_rows:
            key = ref_key(image_row, ref)
            poly = ref_polys.get(key)
            refs[key] = {'image_id': image_row['id'], 'scene_id': ref.get('scene_id') or image_row.get('scene_id'), 'ann_id': ref['id'],
                         'ref_id': ref.get('ref_id'), 'object_id': ref.get('object_id'), 'tier': ref.get('tier'), 'provenance': ref.get('provenance'),
                         'area_m2': ref.get('area_m2'), 'area_band': band_of(ref.get('area_m2'), AREA_BANDS),
                         'image_identity': ref.get('image_identity') or 'same_image', 'pixel_band': band_of(px_m, PIXEL_BANDS),
                         'segmentation_sha256': hashlib.sha256(json.dumps(ref['segmentation']).encode()).hexdigest(),
                         'vertices': len(ref['segmentation'][0]) // 2 if ref.get('segmentation') else None,
                         'right_angle_share': shape_of(poly)['right_angle_share'] if poly is not None else None,
                         'edit_diff': edit_diff_for(ref, diffs)}
    return {'version': VERSION, 'dataset': str(dataset), 'split': split, 'images': images, 'refs': refs}


# --- summaries from rows (pure functions of context + predictions + threshold) ---------------------------------------------

def select(predictions, threshold):
    """Rows above the cut; the processor keeps score > threshold, so the same strict test is used here."""
    return [p for p in predictions if p['score'] > threshold]


def per_reference(context, preds_by_arm, arms):
    """{arm: {ref key: row}} best-IoU per reference from the selected rows."""
    out = {}
    for arm in arms:
        best = {key: {'best_iou': 0., 'boundary_distance_m': None, 'area_ratio': None, 'vertices': None, 'right_angle_share': None, 'pred_index': None}
                for key in context['refs']}
        for p in preds_by_arm.get(arm, []):
            for o in p['ref_overlaps']:
                row = best.get(o['ref'])
                if row is None or o['iou'] <= row['best_iou']:
                    continue
                row.update({'best_iou': o['iou'], 'boundary_distance_m': o.get('boundary_m'), 'area_ratio': o.get('area_ratio'),
                            'vertices': p.get('vertices'), 'right_angle_share': p.get('right_angle_share'), 'pred_index': p['index'],
                            'pred_image_id': p['image_id']})
        for row in best.values():
            row['matched'] = row['best_iou'] >= MATCH_IOU
            if not row['matched']:
                row['boundary_distance_m'] = row['area_ratio'] = None
        out[arm] = best
    return out


def image_bootstrap(diffs_by_image, seed=BOOT_SEED, draws=BOOT_DRAWS):
    """95 percent interval of the mean paired difference under resampling of images with replacement; every image
    is weighted by how many times it was drawn (np.unique counts), so multiplicity is kept."""
    images = sorted(diffs_by_image)
    if not images:
        return None
    sums = np.array([float(sum(diffs_by_image[i])) for i in images])
    ns = np.array([float(len(diffs_by_image[i])) for i in images])
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(draws):
        pick = rng.choice(len(images), len(images), replace=True)
        idx, counts = np.unique(pick, return_counts=True)
        den = float((ns[idx] * counts).sum())
        boots.append(float((sums[idx] * counts).sum() / den) if den else np.nan)
    boots = np.array([v for v in boots if np.isfinite(v)])
    if not boots.size:
        return None
    return [round(float(np.percentile(boots, 2.5)), 4), round(float(np.percentile(boots, 97.5)), 4)]


def paired_block(context, refs_a, refs_b, second, first, keys=None):
    """Paired comparison second - first over the given reference keys (default all)."""
    keys = sorted(keys if keys is not None else context['refs'])
    diffs = {k: refs_a[k]['best_iou'] - refs_b[k]['best_iou'] for k in keys}
    by_image = defaultdict(list)
    for k, d in diffs.items():
        by_image[context['refs'][k]['image_id']].append(d)
    values = list(diffs.values())
    matched_a = sum(refs_a[k]['matched'] for k in keys)
    matched_b = sum(refs_b[k]['matched'] for k in keys)
    joint = [k for k in keys if refs_a[k]['matched'] and refs_b[k]['matched']
             and refs_a[k]['boundary_distance_m'] is not None and refs_b[k]['boundary_distance_m'] is not None]
    bdiffs = [refs_a[k]['boundary_distance_m'] - refs_b[k]['boundary_distance_m'] for k in joint]
    block = {'second_minus_first': f'{second} - {first}', 'second': second, 'first': first, 'references': len(keys),
             'best_iou_delta_mean': round(float(np.mean(values)), 4) if values else None,
             'best_iou_delta_median': _median(values),
             'best_iou_delta_ci95_image_bootstrap': image_bootstrap(by_image) if values else None,
             'per_reference': {'higher': sum(d > 1e-9 for d in values), 'lower': sum(d < -1e-9 for d in values), 'equal': sum(abs(d) <= 1e-9 for d in values)},
             'images': len(by_image), 'matched_second': matched_a, 'matched_first': matched_b, 'matched_delta': matched_a - matched_b,
             'recall_delta': round((matched_a - matched_b) / len(keys), 4) if keys else None,
             'jointly_matched': len(joint),
             'boundary_distance_delta_median_m': _median(bdiffs, 3),
             'boundary_distance_delta_mean_m': round(float(np.mean(bdiffs)), 3) if bdiffs else None,
             'boundary_worse': sum(d > 1e-9 for d in bdiffs), 'boundary_better': sum(d < -1e-9 for d in bdiffs),
             'boundary_equal': sum(abs(d) <= 1e-9 for d in bdiffs)}
    return block


def _strata(context, refs_by_arm, arms, field, bands):
    out = {}
    for name in bands:
        keys = [k for k, r in context['refs'].items() if r.get(field) == name]
        out[name] = {'references': len(keys), **{arm: {'matched': sum(refs_by_arm[arm][k]['matched'] for k in keys),
                                                        'best_iou_median': _median([refs_by_arm[arm][k]['best_iou'] for k in keys])}
                                                  for arm in arms}}
    return out


def pair_strata(context, refs_by_arm, pairs, field, bands):
    out = {}
    for name in bands:
        keys = [k for k, r in context['refs'].items() if r.get(field) == name]
        out[name] = {'references': len(keys)}
        for second, first in pairs:
            if keys:
                b = paired_block(context, refs_by_arm[second], refs_by_arm[first], second, first, keys)
                out[name][f'{second}:{first}'] = {k: b[k] for k in ('best_iou_delta_mean', 'best_iou_delta_ci95_image_bootstrap', 'per_reference',
                                                                    'matched_second', 'matched_first', 'matched_delta',
                                                                    'boundary_distance_delta_median_m', 'boundary_worse', 'boundary_better')}
            else:
                out[name][f'{second}:{first}'] = None
    return out


def negatives_for(context, preds, timings=None):
    """Prediction-side proxies for one arm at the cut: novel share, footprint backing (agreement with another model),
    per-hectare rates, novel area histogram and pseudo-label recall. Human rejections are never counted here."""
    images = context['images']
    total = len(preds)
    kinds = Counter(p['kind'] for p in preds)
    novel = [p for p in preds if p['kind'] == 'novel']
    backed = sum(1 for p in novel if p['footprint_backed'] is True)
    unbacked = sum(1 for p in novel if p['footprint_backed'] is False)
    ha_total = sum(i['hectares'] for i in images.values())
    covered = {iid for iid, i in images.items() if i['footprints_in_tile'] > 0}
    preds_covered = [p for p in preds if str(p['image_id']) in covered]
    novel_covered = [p for p in preds_covered if p['kind'] == 'novel']
    per_image = Counter(str(p['image_id']) for p in preds)
    per_tile = [per_image.get(iid, 0) for iid in images]
    per_ha = [per_image.get(iid, 0) / i['hectares'] for iid, i in images.items() if i['hectares']]
    hist = Counter(band_of(p['area_m2'], NOVEL_AREA_BINS) for p in novel)
    pseudo_total = sum(len(i['pseudo_ids']) for i in images.values())
    pseudo_hit = set()
    for p in preds:
        for o in p['pseudo_overlaps']:
            if o['iou'] >= MATCH_IOU:
                pseudo_hit.add((p['image_id'], o['id']))
    novel_areas = [p['area_m2'] for p in novel]
    return {'tiles': len(images), 'predictions': total, 'predictions_per_tile_median': _median(per_tile, 1),
            'predictions_per_ha': round(total / ha_total, 3) if ha_total else None, 'predictions_per_ha_median_tile': _median(per_ha, 2),
            'hectares': round(ha_total, 3),
            'human_matched': kinds.get('human', 0), 'pseudo_only': kinds.get('pseudo_only', 0), 'novel': len(novel),
            'novel_share': round(len(novel) / total, 4) if total else None,
            'novel_per_ha': round(len(novel) / ha_total, 3) if ha_total else None,
            'novel_footprint_backed': backed, 'novel_no_footprint': unbacked,
            'novel_footprint_unknown': len(novel) - backed - unbacked,
            'novel_no_footprint_share_of_predictions': round(unbacked / total, 4) if total else None,
            'novel_area_m2_median': _median(novel_areas, 1),
            'novel_area_histogram': {name: hist.get(name, 0) for name, _, _ in NOVEL_AREA_BINS},
            'novel_share_under_12m2': round(hist.get('lt_12', 0) / len(novel), 4) if novel else None,
            'footprint_covered_tiles': len(covered),
            'covered_tiles': {'predictions': len(preds_covered), 'novel': len(novel_covered),
                              'novel_no_footprint': sum(1 for p in novel_covered if p['footprint_backed'] is False),
                              'novel_no_footprint_share_of_predictions': (round(sum(1 for p in novel_covered if p['footprint_backed'] is False) / len(preds_covered), 4)
                                                                          if preds_covered else None)},
            'pseudo_labels': pseudo_total, 'pseudo_recall_at_0_5': round(len(pseudo_hit) / pseudo_total, 4) if pseudo_total else None,
            'seconds_per_image_median': _median([t['seconds'] for t in timings], 3) if timings else None}


def shape_agreement(context, refs_by_arm, arm):
    """On the arm's matched references: vertex count of the prediction (0.9 px simplification), right-angle share,
    area ratio prediction / reference, against the same numbers on the reference polygons."""
    rows = [(k, r) for k, r in refs_by_arm[arm].items() if r['matched']]
    refs = context['refs']
    out = {'matched': len(rows),
           'prediction_vertices_median': _median([r['vertices'] for _, r in rows], 1),
           'reference_vertices_median': _median([refs[k]['vertices'] for k, _ in rows], 1),
           'prediction_right_angle_share_median': _median([r['right_angle_share'] for _, r in rows]),
           'reference_right_angle_share_median': _median([refs[k]['right_angle_share'] for k, _ in rows]),
           'area_ratio_median': _median([r['area_ratio'] for _, r in rows]),
           'area_ratio_over_1_05': sum(1 for _, r in rows if (r['area_ratio'] or 0) > 1.05),
           'area_ratio_under_0_95': sum(1 for _, r in rows if r['area_ratio'] is not None and r['area_ratio'] < .95)}
    strata = {}
    for name in ('moved_ge_0_45', 'moved_lt_0_45'):
        keys = [k for k, r in refs.items() if (r.get('edit_diff') or {}).get('stratum') == name]
        strata[name] = {'references': len(keys), 'matched': sum(refs_by_arm[arm][k]['matched'] for k in keys),
                        'best_iou_median': _median([refs_by_arm[arm][k]['best_iou'] for k in keys]),
                        'boundary_distance_m_median': _median([refs_by_arm[arm][k]['boundary_distance_m'] for k in keys], 3)}
    out['edit_diff_strata'] = {**strata, 'joined_references': sum(1 for r in refs.values() if r.get('edit_diff')),
                               'key': f'edit-diff hausdorff_m >= {EDIT_MOVE_M} m (human outline vs the model outline it replaced)'}
    return out


def build_summary(context, predictions, arms, pairs, threshold, timings=None):
    """The whole judge summary at one score cut, from the rows alone (no inference, no image)."""
    selected = select(predictions, threshold)
    preds_by_arm = {arm: [p for p in selected if p['arm'] == arm] for arm in arms}
    timings_by_arm = {arm: [t for t in (timings or []) if t['arm'] == arm] for arm in arms}
    refs_by_arm = per_reference(context, preds_by_arm, arms)
    out = {}
    for arm in arms:
        rows = refs_by_arm[arm]
        matched = [r for r in rows.values() if r['matched']]
        per_image = Counter(str(p['image_id']) for p in preds_by_arm[arm])
        judged_images = sorted({str(r['image_id']) for r in context['refs'].values()}) or list(context['images'])
        out[arm] = {'references': len(rows), 'matched_at_iou_0_5': len(matched),
                    'recall_at_0_5': round(len(matched) / len(rows), 4) if rows else None,
                    'best_iou_median': _median([r['best_iou'] for r in rows.values()]),
                    'best_iou_mean': round(float(np.mean([r['best_iou'] for r in rows.values()])), 4) if rows else None,
                    'boundary_distance_m_median_matched': _median([r['boundary_distance_m'] for r in matched], 3),
                    'predictions_per_image_median': _median([per_image.get(i, 0) for i in judged_images], 1),
                    'seconds_per_image_median': _median([t['seconds'] for t in timings_by_arm[arm]], 3) if timings_by_arm[arm] else None,
                    'same_image': {'references': sum(1 for r in context['refs'].values() if r['image_identity'] == 'same_image'),
                                   'matched': sum(1 for k, r in rows.items() if r['matched'] and context['refs'][k]['image_identity'] == 'same_image'),
                                   'best_iou_median': _median([r['best_iou'] for k, r in rows.items() if context['refs'][k]['image_identity'] == 'same_image'])},
                    'different_image': {'references': sum(1 for r in context['refs'].values() if r['image_identity'] == 'different_image'),
                                        'matched': sum(1 for k, r in rows.items() if r['matched'] and context['refs'][k]['image_identity'] == 'different_image'),
                                        'best_iou_median': _median([r['best_iou'] for k, r in rows.items() if context['refs'][k]['image_identity'] == 'different_image'])}}
    out['by_area_band'] = _strata(context, refs_by_arm, arms, 'area_band', [b[0] for b in AREA_BANDS])
    out['by_tier'] = _strata(context, refs_by_arm, arms, 'tier', ['T1', 'T2'])
    out['by_pixel_band'] = _strata(context, refs_by_arm, arms, 'pixel_band', [b[0] for b in PIXEL_BANDS])
    out['by_image_identity'] = _strata(context, refs_by_arm, arms, 'image_identity', ['same_image', 'different_image'])
    out['pairs'] = {}
    same_keys = [k for k, r in context['refs'].items() if r['image_identity'] == 'same_image']
    diff_keys = [k for k, r in context['refs'].items() if r['image_identity'] == 'different_image']
    for second, first in pairs:
        block = paired_block(context, refs_by_arm[second], refs_by_arm[first], second, first)
        block['same_image_only'] = paired_block(context, refs_by_arm[second], refs_by_arm[first], second, first, same_keys) if same_keys else None
        block['different_image_only'] = paired_block(context, refs_by_arm[second], refs_by_arm[first], second, first, diff_keys) if diff_keys else None
        out['pairs'][f'{second}:{first}'] = block
    if pairs:
        out['paired'] = out['pairs'][f'{pairs[0][0]}:{pairs[0][1]}']        # legacy single-pair readers
        out['same_image_only'] = {f'{s}:{f}': out['pairs'][f'{s}:{f}']['same_image_only'] for s, f in pairs}
        for s, f in pairs:                                           # r3_record reads same_image_only.paired_vs_<first> for the tuned arm
            if s == 'tuned':
                out['same_image_only'][f'paired_vs_{f}'] = out['pairs'][f'{s}:{f}']['same_image_only']
        out['pairs_by_area_band'] = pair_strata(context, refs_by_arm, pairs, 'area_band', [b[0] for b in AREA_BANDS])
        out['pairs_by_tier'] = pair_strata(context, refs_by_arm, pairs, 'tier', ['T1', 'T2'])
        out['pairs_by_pixel_band'] = pair_strata(context, refs_by_arm, pairs, 'pixel_band', [b[0] for b in PIXEL_BANDS])
    out['negatives'] = {arm: negatives_for(context, preds_by_arm[arm], timings_by_arm[arm]) for arm in arms}
    out['negatives']['note'] = ('novel = prediction overlapping no human outline and no production pseudo label at IoU 0.3 on the same tile; '
                                'novel_footprint_backed = at least half of the prediction lies inside a Microsoft building footprint (CDLA, '
                                'ML output): agreement with another model, not a truth check; novel_no_footprint is the closest available '
                                'proxy for over-detection; pseudo_only and pseudo_recall are agreement with production SAM3, not truth; '
                                'human rejections are never counted as roof false positives')
    out['shape_agreement'] = {arm: shape_agreement(context, refs_by_arm, arm) for arm in arms}
    out['threshold'] = threshold
    out['reference_rows'] = {arm: [{'ref': k, **{kk: vv for kk, vv in r.items()}} for k, r in refs_by_arm[arm].items()] for arm in arms}
    out['note'] = (f"agreement with the listed human outlines ({Path(context['dataset']).name} {context['split']} split), not calibrated "
                   'accuracy; SAM3 outputs are model estimates; different_image references were drawn on another capture and carry '
                   'registration error by construction')
    return out


def legacy_rows(context, summary, arms):
    """Per-reference rows in the sweep-era shape (one row per arm per reference)."""
    rows = []
    for arm in arms:
        for row in summary['reference_rows'][arm]:
            ref = context['refs'][row['ref']]
            rows.append({'arm': arm, 'image_id': ref['image_id'], 'scene_id': ref['scene_id'], 'ref_id': ref['ref_id'] or ref['object_id'] or ref['ann_id'],
                         'ref_key': row['ref'], 'tier': ref['tier'], 'area_m2': ref['area_m2'], 'image_identity': ref['image_identity'],
                         'best_iou': row['best_iou'], 'matched': row['matched'], 'boundary_distance_m': row['boundary_distance_m'],
                         'area_ratio': row['area_ratio'], 'prediction_vertices': row['vertices']})
    return rows


def legacy_tiles(context, predictions, arms, threshold):
    selected = select(predictions, threshold)
    tiles = []
    for arm in arms:
        for iid, image in context['images'].items():
            preds = [p for p in selected if p['arm'] == arm and str(p['image_id']) == iid]
            novel = [p for p in preds if p['kind'] == 'novel']
            tiles.append({'arm': arm, 'image_id': int(iid) if iid.isdigit() else iid, 'scene_id': image['scene_id'], 'predictions': len(preds),
                          'predictions_per_ha': round(len(preds) / image['hectares'], 2) if image['hectares'] else None,
                          'human_refs': image['references'], 'pseudo': len(image['pseudo_ids']), 'footprints_in_tile': image['footprints_in_tile'],
                          'human': sum(p['kind'] == 'human' for p in preds), 'pseudo_only': sum(p['kind'] == 'pseudo_only' for p in preds),
                          'novel': len(novel), 'novel_footprint_backed': sum(p['footprint_backed'] is True for p in novel),
                          'novel_no_footprint': sum(p['footprint_backed'] is False for p in novel),
                          'novel_area_m2_median': _median([p['area_m2'] for p in novel], 1)})
    return tiles


def payload_for(context, predictions, timings, arms, arm_specs, pairs, threshold, meta):
    summary = build_summary(context, predictions, arms, pairs, threshold, timings)
    return {'version': VERSION, 'computed_at': now_iso(), 'threshold': threshold, 'images': len(context['images']),
            'references': len(context['refs']), 'arms': {a: {'spec': s} for a, s in zip(arms, arm_specs, strict=True)},
            'pairs': [f'{s}:{f}' for s, f in pairs], 'dataset': context['dataset'], 'split': context['split'],
            'threshold_note': THRESHOLD_NOTE, 'production_cut': PRODUCTION_CUT, **meta,
            'summary': summary, 'rows': legacy_rows(context, summary, arms), 'tiles': legacy_tiles(context, predictions, arms, threshold)}


def parse_pairs(specs, arms):
    pairs = []
    for spec in specs or ():
        second, _, first = spec.partition(':')
        if not first or second not in arms or first not in arms:
            raise SystemExit(f'--pair {spec!r}: both names must be arms ({", ".join(arms)})')
        pairs.append((second, first))
    if not specs and len(arms) == 2:
        pairs.append((arms[1], arms[0]))
    return pairs


def write_jsonl(path, rows):
    with open(path, 'w') as handle:
        for row in rows:
            handle.write(json.dumps(row) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dataset', default=str(DEFAULT_DATASET), help='dataset-v1 format folder (dataset-v2 is the round-3 primary)')
    parser.add_argument('--split', choices=('test', 'val'), default='test')
    parser.add_argument('--arm', action='append', required=True, help='name or name=trainable.pt (base checkpoint always loaded)')
    parser.add_argument('--pair', action='append', default=None, help='second:first paired block (repeatable); default second:first of two arms')
    parser.add_argument('--weights', default=str(smoke.WEIGHTS))
    parser.add_argument('--device', default='mps')
    parser.add_argument('--dtype', choices=('fp32', 'bf16'), default='fp32')
    parser.add_argument('--prompt', default=smoke.PROMPT)
    parser.add_argument('--limit', type=int, help='first N judged images')
    parser.add_argument('--all-images', action='store_true', help='judge every image of the split, not only those with a human outline')
    parser.add_argument('--out', default=str(OUT))
    parser.add_argument('--footprints', action='append', default=None, help='Microsoft footprint cache (repeatable, union); default the Mooroolbark cache')
    parser.add_argument('--no-footprints', action='store_true', help='skip the Microsoft footprint check')
    parser.add_argument('--threshold', type=float, default=None, help='processor confidence threshold for collecting rows (default 0.45; round 3 uses 0.2)')
    args = parser.parse_args(argv)
    dataset = Path(args.dataset)
    threshold = args.threshold if args.threshold is not None else smoke.THRESHOLD
    rows_images = human_references(dataset, args.split, True if args.all_images else None)[:args.limit or None]
    footprint_paths = [Path(p) for p in (args.footprints or [str(FOOTPRINTS)])]
    footprints, fp_shas = (None, {}) if args.no_footprints else load_footprints(footprint_paths)
    diffs = edit_diffs()
    arms, predictions, timings, loads = [], [], [], {}
    fp_cache, ref_polys = {}, {}
    for spec in args.arm:
        name, _, trainable = spec.partition('=')
        arms.append(name)
        arm_preds, arm_timings, load_s = evaluate_arm(name, args.weights, trainable or None, args.device, args.dtype, rows_images, args.prompt,
                                                      footprints, threshold, dataset, fp_cache, ref_polys)
        predictions.extend(arm_preds)
        timings.extend(arm_timings)
        loads[name] = load_s
        print(json.dumps({'arm': name, 'images': len(rows_images), 'predictions': len(arm_preds),
                          'matched_refs': len({p['matched_ref_id'] for p in arm_preds if p['matched_ref_id']}),
                          'novel_predictions': sum(p['kind'] == 'novel' for p in arm_preds)}), flush=True)
    pairs = parse_pairs(args.pair, arms)
    context = build_context(dataset, args.split, rows_images, fp_cache, ref_polys, diffs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = dataset / 'manifest.json'
    meta = {'device': args.device, 'dtype': args.dtype, 'prompt': args.prompt, 'model_load_s': loads,
            'dataset_manifest_sha256': sha256_file(manifest) if manifest.exists() else None,
            'split_file_sha256': sha256_file(dataset / f'{args.split}.json'),
            'footprint_caches': fp_shas, 'judge_version_sha256': sha256_file(Path(__file__)),
            'edit_diffs': str(EDIT_DIFFS) if diffs else None, 'limit': args.limit,
            'rows_collected_at_threshold': threshold, 'view_threshold': threshold}
    write_jsonl(out / 'predictions.jsonl', predictions)
    write_jsonl(out / 'timings.jsonl', timings)
    with open(out / 'context.json', 'w') as handle:
        json.dump({**context, 'meta': meta, 'arms': arms, 'arm_specs': args.arm, 'pairs': [f'{s}:{f}' for s, f in pairs]}, handle)
    payload = payload_for(context, predictions, timings, arms, args.arm, pairs, threshold, meta)
    with open(out / 'summary.json', 'w') as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
    brief = {k: payload[k] for k in ('version', 'dataset', 'split', 'threshold', 'images', 'references')}
    brief['summary'] = {a: payload['summary'][a] for a in arms}
    brief['pairs'] = {k: {kk: v[kk] for kk in ('best_iou_delta_mean', 'best_iou_delta_ci95_image_bootstrap', 'per_reference', 'matched_delta',
                                                'boundary_distance_delta_median_m', 'boundary_worse', 'boundary_better')}
                      for k, v in payload['summary']['pairs'].items()}
    brief['negatives'] = {a: {k: payload['summary']['negatives'][a][k] for k in ('predictions', 'predictions_per_ha', 'novel', 'novel_per_ha',
                                                                                 'novel_no_footprint_share_of_predictions', 'novel_share_under_12m2',
                                                                                 'pseudo_recall_at_0_5')} for a in arms}
    json.dump(brief, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
