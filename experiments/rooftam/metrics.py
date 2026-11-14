"""Native-pixel roof-mask metrics. Not structural/instance-detection accuracy."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import binary_erosion, distance_transform_edt


def score_masks(pred, truth, gsd_m: float) -> dict:
    pred, truth = np.asarray(pred, bool), np.asarray(truth, bool)
    if pred.shape != truth.shape or pred.ndim != 2:
        raise ValueError('Metrics require matching native-resolution 2D masks')
    if not np.isfinite(gsd_m) or gsd_m <= 0:
        raise ValueError('Invalid ground sample distance')
    inter, union = np.logical_and(pred, truth).sum(), np.logical_or(pred, truth).sum()
    total = pred.sum() + truth.sum()
    result = {'iou': float(inter/union) if union else 1.0,
              'dice': float(2*inter/total) if total else 1.0,
              'area_error_m2': float((int(pred.sum())-int(truth.sum()))*gsd_m**2)}
    pb = pred & ~binary_erosion(pred, border_value=0)
    tb = truth & ~binary_erosion(truth, border_value=0)
    for tolerance in (.25, .5, 1.0):
        if not pb.any() or not tb.any():
            f1 = float(not pb.any() and not tb.any())
        else:
            precision = np.mean(distance_transform_edt(~tb)[pb]*gsd_m <= tolerance)
            recall = np.mean(distance_transform_edt(~pb)[tb]*gsd_m <= tolerance)
            f1 = 2*precision*recall/(precision+recall) if precision+recall else 0.0
        result[f'boundary_f1_{tolerance:g}m'] = float(f1)
    if pb.any() and tb.any():
        distances = np.r_[distance_transform_edt(~tb)[pb], distance_transform_edt(~pb)[tb]]*gsd_m
        result['boundary_distance_p95_m'] = float(np.percentile(distances, 95))
    else:
        result['boundary_distance_p95_m'] = None
    return result


def summarize(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError('Cannot summarize zero evaluated samples')
    keys = ('iou', 'dice', 'boundary_f1_0.25m', 'boundary_f1_0.5m', 'boundary_f1_1m')
    return dict(n=len(rows), **{k: float(np.mean([r[k] for r in rows])) for k in keys},
                mean_absolute_area_error_m2=float(np.mean([abs(r['area_error_m2']) for r in rows])))
