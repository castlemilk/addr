"""Boundary metrics for one predicted mask against one reference outline, in the scene pixel frame.

All distances are converted to metres with the image's pixel size. The polygonisation uses the repo's
segmentation.mask_to_polygons (the same vectoriser the SAM3 consumers use), so a corner the model
draws but the vectoriser rounds is counted as rounded. Numbers are reference agreement, not accuracy.
"""
from __future__ import annotations

import math
import statistics

import numpy as np
from scipy import ndimage
from shapely.geometry import Point

from ..segmentation import mask_to_polygons

BOUNDARY_TOLERANCE_M = .5
VERTEX_TOLERANCE_M = .45  # the repo's Hausdorff threshold for "agrees with the human outline"
SIMPLIFY_PX = .9          # scene_extract.extract_objects vectorises with simplify_px=.9


def mask_iou(pred, target):
    pred, target = np.asarray(pred, dtype=bool), np.asarray(target, dtype=bool)
    union = np.logical_or(pred, target).sum()
    return float(np.logical_and(pred, target).sum() / union) if union else None


def _boundary(mask):
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return np.zeros_like(mask)
    eroded = ndimage.binary_erosion(mask, border_value=0)
    return mask & ~eroded


def boundary_f_score(pred, target, pixel_size_m, tolerance_m=BOUNDARY_TOLERANCE_M):
    """Boundary precision/recall/F within `tolerance_m` of the other boundary (distance transform)."""
    pred_b, target_b = _boundary(pred), _boundary(target)
    if not pred_b.any() or not target_b.any():
        return {'boundary_precision': None, 'boundary_recall': None, 'boundary_f': None}
    tolerance_px = tolerance_m / pixel_size_m
    to_target = ndimage.distance_transform_edt(~target_b)
    to_pred = ndimage.distance_transform_edt(~pred_b)
    precision = float((to_target[pred_b] <= tolerance_px).mean())
    recall = float((to_pred[target_b] <= tolerance_px).mean())
    f = 2 * precision * recall / (precision + recall) if precision + recall else 0.
    return {'boundary_precision': precision, 'boundary_recall': recall, 'boundary_f': f}


def polygonise(mask, min_area_px=5., simplify_px=SIMPLIFY_PX):
    """Largest polygon of the mask through the repo's vectoriser, or None."""
    polygons = mask_to_polygons(np.asarray(mask, dtype=bool), min_area_px=min_area_px, simplify_px=simplify_px)
    return max(polygons, key=lambda p: p.area) if polygons else None


def corner_metrics(pred_polygon, reference_polygon, pixel_size_m, tolerance_m=VERTEX_TOLERANCE_M):
    """Hausdorff between boundaries and per-reference-vertex distance to the predicted boundary (metres)."""
    if pred_polygon is None or reference_polygon is None or pred_polygon.is_empty or reference_polygon.is_empty:
        return {'hausdorff_m': None, 'vertex_distance_mean_m': None, 'vertex_distance_median_m': None,
                'vertex_within_tolerance': None, 'n_reference_vertices': None, 'n_pred_vertices': None}
    boundary = pred_polygon.boundary
    distances = [Point(x, y).distance(boundary) * pixel_size_m for x, y in reference_polygon.exterior.coords[:-1]]
    return {'hausdorff_m': float(reference_polygon.boundary.hausdorff_distance(boundary) * pixel_size_m),
            'vertex_distance_mean_m': float(np.mean(distances)) if distances else None,
            'vertex_distance_median_m': float(np.median(distances)) if distances else None,
            'vertex_within_tolerance': float(np.mean([d <= tolerance_m for d in distances])) if distances else None,
            'n_reference_vertices': len(distances), 'n_pred_vertices': len(pred_polygon.exterior.coords) - 1}


OUTLINE_PAIR_KEYS = ('iou', 'hausdorff_m', 'human_vertex_to_model_median_m', 'human_vertex_within_0_45_m',
                     'model_vertex_to_human_median_m', 'centroid_shift_m', 'human_area_m2', 'model_area_m2',
                     'area_delta_m2', 'n_human_vertices', 'n_model_vertices', 'vertex_delta')


def outline_pair(human_polygon, model_polygon, pixel_size_m):
    """Agreement between a reviewer outline and the model estimate it replaced; not accuracy.

    Both polygons in the scene pixel frame; distances and areas in metres. Every value is None when either
    polygon is missing or empty. `vertex_delta` is human minus model vertex count.
    """
    if (human_polygon is None or model_polygon is None or human_polygon.is_empty or model_polygon.is_empty
            or human_polygon.area <= 0 or model_polygon.area <= 0):
        return dict.fromkeys(OUTLINE_PAIR_KEYS)
    human_vertices = corner_metrics(model_polygon, human_polygon, pixel_size_m)   # per human vertex -> model boundary
    model_vertices = corner_metrics(human_polygon, model_polygon, pixel_size_m)   # per model vertex -> human boundary
    union = human_polygon.union(model_polygon).area
    return {'iou': float(human_polygon.intersection(model_polygon).area / union) if union > 0 else None,
            'hausdorff_m': human_vertices['hausdorff_m'],
            'human_vertex_to_model_median_m': human_vertices['vertex_distance_median_m'],
            'human_vertex_within_0_45_m': human_vertices['vertex_within_tolerance'],
            'model_vertex_to_human_median_m': model_vertices['vertex_distance_median_m'],
            'centroid_shift_m': float(human_polygon.centroid.distance(model_polygon.centroid) * pixel_size_m),
            'human_area_m2': float(human_polygon.area) * pixel_size_m ** 2,
            'model_area_m2': float(model_polygon.area) * pixel_size_m ** 2,
            'area_delta_m2': float(human_polygon.area - model_polygon.area) * pixel_size_m ** 2,
            'n_human_vertices': human_vertices['n_reference_vertices'],
            'n_model_vertices': human_vertices['n_pred_vertices'],
            'vertex_delta': human_vertices['n_reference_vertices'] - human_vertices['n_pred_vertices']}


def evaluate_mask(pred_mask, reference_mask, reference_polygon, pixel_size_m):
    """Every metric for one prediction; masks in the same pixel frame, polygon in pixels."""
    pred_mask = np.asarray(pred_mask, dtype=bool)
    result = {'iou': mask_iou(pred_mask, reference_mask), **boundary_f_score(pred_mask, reference_mask, pixel_size_m),
              **corner_metrics(polygonise(pred_mask), reference_polygon, pixel_size_m),
              'pred_area_m2': float(pred_mask.sum()) * pixel_size_m ** 2,
              'reference_area_m2': float(np.asarray(reference_mask, dtype=bool).sum()) * pixel_size_m ** 2}
    return result


def _finite(values):
    return [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]


def summarise(rows, keys=('iou', 'boundary_f', 'hausdorff_m', 'vertex_distance_median_m', 'vertex_within_tolerance')):
    """Median / mean per metric with explicit denominators; empty input yields nulls, never zeros."""
    summary = {'n': len(rows)}
    for key in keys:
        values = _finite([r.get(key) for r in rows])
        summary[key] = {'median': float(statistics.median(values)) if values else None,
                        'mean': float(np.mean(values)) if values else None, 'n': len(values)}
    hausdorff = _finite([r.get('hausdorff_m') for r in rows])
    summary['n_hausdorff_le_0_45_m'] = sum(h <= VERTEX_TOLERANCE_M for h in hausdorff)
    summary['n_hausdorff'] = len(hausdorff)
    return summary


HIGHER_IS_BETTER = ('iou', 'boundary_f', 'boundary_precision', 'boundary_recall', 'vertex_within_tolerance')


def paired_delta(candidate_rows, control_rows, key='hausdorff_m', seed=0, samples=1000):
    """Paired candidate-minus-control differences with a bootstrap 95% interval (None when unpaired).

    n_improved counts pairs where the candidate is better: lower for distances, higher for IoU and F-scores.
    """
    pairs = [(c.get(key), k.get(key)) for c, k in zip(candidate_rows, control_rows, strict=True)]
    deltas = np.array([c - k for c, k in pairs if c is not None and k is not None], dtype=float)
    if deltas.size == 0:
        return {'key': key, 'n': 0, 'median_delta': None, 'ci95': None}
    rng = np.random.default_rng(seed)
    medians = [float(np.median(rng.choice(deltas, deltas.size, replace=True))) for _ in range(samples)]
    better = deltas > 0 if key in HIGHER_IS_BETTER else deltas < 0
    worse = deltas < 0 if key in HIGHER_IS_BETTER else deltas > 0
    return {'key': key, 'n': int(deltas.size), 'median_delta': float(np.median(deltas)),
            'ci95': [float(np.percentile(medians, 2.5)), float(np.percentile(medians, 97.5))],
            'n_improved': int(better.sum()), 'n_worse': int(worse.sum()),
            'higher_is_better': key in HIGHER_IS_BETTER}


def label_boundary(labels):
    """Pixels of an instance label map whose 4-neighbour label differs (instance edges, incl. roof-to-roof)."""
    labels = np.asarray(labels)
    edge = np.zeros(labels.shape, dtype=bool)
    across = labels[:, 1:] != labels[:, :-1]
    down = labels[1:] != labels[:-1]
    edge[:, 1:] |= across
    edge[:, :-1] |= across
    edge[1:] |= down
    edge[:-1] |= down
    return edge & (labels > 0)


def label_boundary_f(pred_labels, target_labels, pixel_size_m, ignore=None, tolerance_m=BOUNDARY_TOLERANCE_M):
    """Image-level instance-boundary F within tolerance; boundary pixels within 2 px of IGNORE are not counted."""
    pred_b, target_b = label_boundary(pred_labels), label_boundary(target_labels)
    if ignore is not None and np.any(ignore):
        near = ndimage.binary_dilation(ignore, iterations=2)
        pred_b &= ~near
        target_b &= ~near
    if not pred_b.any() and not target_b.any():
        return {'boundary_precision': None, 'boundary_recall': None, 'boundary_f': None}
    if not pred_b.any() or not target_b.any():
        return {'boundary_precision': 0. if pred_b.any() else None, 'boundary_recall': 0. if target_b.any() else None,
                'boundary_f': 0.}
    tolerance_px = tolerance_m / pixel_size_m
    precision = float((ndimage.distance_transform_edt(~target_b)[pred_b] <= tolerance_px).mean())
    recall = float((ndimage.distance_transform_edt(~pred_b)[target_b] <= tolerance_px).mean())
    f = 2 * precision * recall / (precision + recall) if precision + recall else 0.
    return {'boundary_precision': precision, 'boundary_recall': recall, 'boundary_f': f}
