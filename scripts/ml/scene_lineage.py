"""Object lineage between a baseline and a candidate prediction of the same image.

Objects pair by identity first (ids hash the raw detector mask), then by a
one-to-one Hungarian assignment on raw-ring IoU >= 0.8 within each kind. The
remainder are gained/lost. Lineage supports paired comparison; it is a
diagnostic join, not labels/accuracy.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment
from shapely.errors import GEOSException
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from . import eyes_index

VERSION = 'scene-lineage-v1'
IOU_THRESHOLD = .8
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def raw_ring(obj):
    """The detector mask outline (pixels); refined rings would hide re-detections."""
    ring = (obj.get('boundary_refinement') or {}).get('raw_ring')
    return ring if ring and len(ring) >= 3 else obj['ring']


def shape(ring):
    try:
        polygon = Polygon(ring)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
    except (ValueError, TypeError, GEOSException):
        return None
    return polygon if not polygon.is_empty and polygon.area > 0 else None


def iou(a, b):
    overlap = a.intersection(b).area
    union = a.area + b.area - overlap
    return overlap / union if union > 0 else 0.


def current(scene):
    return {o['id']: o for o in scene.get('objects', []) if o.get('status') != 'superseded'}


def _row(before, after, match, value):
    source = before or after
    return {'baseline_object_id': before and before['id'], 'candidate_object_id': after and after['id'],
            'kind': source['kind'], 'raw_ring_iou': None if value is None else round(float(value), 4),
            'id_stable': match == 'id', 'match': match}


def link(baseline_scene, candidate_scene, *, threshold=IOU_THRESHOLD):
    before, after = current(baseline_scene), current(candidate_scene)
    shapes_before = {k: shape(raw_ring(o)) for k, o in before.items()}
    shapes_after = {k: shape(raw_ring(o)) for k, o in after.items()}
    rows, used_before, used_after = [], set(), set()
    for oid in sorted(before.keys() & after.keys()):
        if before[oid]['kind'] != after[oid]['kind']:
            continue
        a, b = shapes_before[oid], shapes_after[oid]
        rows.append(_row(before[oid], after[oid], 'id', iou(a, b) if a is not None and b is not None else None))
        used_before.add(oid)
        used_after.add(oid)
    for kind in sorted({o['kind'] for o in (*before.values(), *after.values())}):
        left = [k for k in sorted(before) if before[k]['kind'] == kind and k not in used_before and shapes_before[k] is not None]
        right = [k for k in sorted(after) if after[k]['kind'] == kind and k not in used_after and shapes_after[k] is not None]
        if not left or not right:
            continue
        tree = STRtree([shapes_after[k] for k in right])
        matrix = np.zeros((len(left), len(right)))
        for i, oid in enumerate(left):
            for j in tree.query(shapes_before[oid], predicate='intersects'):
                value = iou(shapes_before[oid], shapes_after[right[j]])
                if value >= threshold:
                    matrix[i, j] = value
        if not matrix.any():
            continue
        # Maximise total IoU so one greedy best pair cannot orphan two good pairs.
        for i, j in zip(*linear_sum_assignment(matrix, maximize=True), strict=True):
            if matrix[i, j] >= threshold:
                rows.append(_row(before[left[i]], after[right[j]], 'iou', matrix[i, j]))
                used_before.add(left[i])
                used_after.add(right[j])
    rows += [_row(before[k], None, 'lost', None) for k in sorted(before) if k not in used_before]
    rows += [_row(None, after[k], 'gained', None) for k in sorted(after) if k not in used_after]
    return rows


def _counts(rows):
    count = {m: sum(r['match'] == m for r in rows) for m in ('id', 'iou', 'gained', 'lost')}
    baseline = count['id'] + count['iou'] + count['lost']
    return {'paired': count['id'] + count['iou'], 'paired_by_id': count['id'], 'paired_by_iou': count['iou'],
            'gained': count['gained'], 'lost': count['lost'],
            'baseline_objects': baseline, 'candidate_objects': count['id'] + count['iou'] + count['gained'],
            'id_stability_rate': count['id'] / baseline if baseline else None,
            'id_stability_denominator': 'baseline objects (not superseded)'}


def stats(rows):
    return {**_counts(rows), 'by_kind': {kind: _counts([r for r in rows if r['kind'] == kind])
                                         for kind in sorted({r['kind'] for r in rows})}}


def _validate(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError(f'Invalid {name}')
    return value


def write(root, experiment_id, scene_id, rows, *, baseline_model_signature=None, candidate_model_signature=None):
    """Replace one experiment/scene's lineage rows in the eyes index.

    Missing sides are stored as '' in key columns (SQLite treats NULL keys as
    distinct, which would duplicate rows on re-write); payloads keep None.
    """
    _validate(experiment_id, 'experiment id')
    _validate(scene_id, 'scene id')
    stamp = datetime.now(UTC).isoformat()
    records = [{'experiment_id': experiment_id, 'scene_id': scene_id,
                'baseline_object_id': r['baseline_object_id'] or '', 'candidate_object_id': r['candidate_object_id'] or '',
                'version': VERSION,
                'payload': {**r, 'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': stamp,
                            'baseline_model_signature': baseline_model_signature,
                            'candidate_model_signature': candidate_model_signature}} for r in rows]
    with closing(eyes_index.connection(Path(root))) as db:
        eyes_index.ensure_schema(db)
        db.execute('DELETE FROM lineage WHERE experiment_id=? AND scene_id=?', (experiment_id, scene_id))
        return eyes_index.replace_rows(db, 'lineage', records)
