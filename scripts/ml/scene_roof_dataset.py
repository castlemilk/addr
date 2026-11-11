"""Read explicit roof-family observations from a verified frozen scene export."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon, box

from .scene_roof_feedback import roof_targets
from .scene_split import image_split_audit
from .scene_training import normalized_split
from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inverse_frame(frame):
    matrix = np.asarray(frame, dtype=float)
    if matrix.shape != (3, 2) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix[:2])) < 1e-12:
        raise ValueError('Invalid roof image transform')
    return np.linalg.inv(np.c_[matrix, [0, 0, 1]])[:, :2].tolist()


def same_reference(first, second):
    """Affine inversion may differ by roundoff; all other provenance is exact."""
    a, b = dict(first), dict(second)
    la, lb = a.pop('linework', None), b.pop('linework', None)
    if a != b or bool(la) != bool(lb):
        return False
    if not la:
        return True
    la, lb = dict(la), dict(lb)
    va, vb = np.asarray(la.pop('vertices')), np.asarray(lb.pop('vertices'))
    return la == lb and va.shape == vb.shape and np.allclose(va, vb, rtol=0, atol=1e-6)


def frozen_roofs(dataset):
    """Recheck snapshot approval/splits; do not read current mutable reviews.

    The small roof sidecar must match the content-addressed full snapshot.
    Metadata from every split blocks leakage. Imagery is opened later, only
    for the split required by fitting or a frozen candidate's evaluation.
    """
    dataset = Path(dataset)
    manifest = json.loads((dataset/'manifest.json').read_text())
    path = dataset/'dataset.jsonl'
    version = digest(path)[:16]
    if manifest['version'] != version:
        raise ValueError('Frozen scene dataset differs from its version')
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if len({r['scene_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate scene identities in roof dataset')
    groups, images = defaultdict(set), defaultdict(set)
    for row in rows:
        if row.get('export_schema_version', 0) < 6:
            raise ValueError('Export schema 6 or later is required for explicit roof feedback')
        split = normalized_split(row['split'])
        if split != 'unassigned':
            groups[row.get('spatial_group')].add(split)
            images[row['image_sha256']].add(split)
    expected_sidecar, cases, ignored = [], [], Counter()
    for row in rows:
        review, split = row.get('review'), normalized_split(row['split'])
        geography = image_split_audit(row, split)
        blockers = []
        if geography['status'] != 'verified' or row.get('image_geography') != geography:
            blockers.append('image_geography')
        if row.get('stale') or (review and review['model_signature'] != row['model_signature']):
            blockers.append('stale')
        if split == 'unassigned':
            blockers.append('unassigned')
        if not row.get('spatial_group'):
            blockers.append('missing_group')
        if len(groups[row.get('spatial_group')]) > 1 or len(images[row['image_sha256']]) > 1:
            blockers.append('split_conflict')
        expected = []
        frame = None
        for obj in review['objects'] if review else []:
            if obj['kind'] != 'building' or not obj.get('roof_feedback') or obj['status'] == 'superseded':
                continue
            frame = frame or inverse_frame(row['pixel_to_meters'])
            ref, _, _ = roof_targets(obj, {**row, 'meters_to_pixel': frame}, blockers)
            if ref:
                expected.append({'id': obj['id'], 'ring': obj['ring'], 'holes': obj.get('holes', []), **ref,
                                 'supervision': 'reviewed-image-observation', 'height_supervision': 'none'})
        actual = row.get('roof_reference', [])
        if (len({r['id'] for r in actual}) != len(actual) or len(expected) != len(actual)
                or not all(same_reference(a, b) for a, b in zip(expected, actual, strict=True))):
            raise ValueError('Roof targets do not match the explicit frozen review checks')
        for ref in actual:
            expected_sidecar.append({'schema_version': 1, 'dataset_version': version,
                **{k: row[k] for k in ('scene_id', 'image_path', 'image_sha256', 'image_width', 'image_height',
                                      'model_signature', 'split', 'spatial_group')},
                'revision': review['revision'], 'reference': ref})
            if 'family' not in ref:
                ignored['linework_without_family'] += 1
                continue
            polygon = Polygon(ref['ring'], ref.get('holes'))
            if (not polygon.is_valid or polygon.area <= 0
                    or not box(0, 0, row['image_width'], row['image_height']).covers(polygon)):
                raise ValueError('Roof family target has an invalid image outline')
            if row['spatial_group'] not in {g['group'] for g in geography['groups']}:
                raise ValueError('Roof area group is outside the verified image frame')
            cases.append({**{k: row[k] for k in ('scene_id', 'image_path', 'image_sha256', 'image_width', 'image_height',
                                               'model_signature', 'spatial_group', 'pixel_to_meters', 'centroid')},
                          'split': split, 'revision': review['revision'], 'object_id': ref['id'],
                          'family': ref['family'], 'ring': ref['ring'], 'holes': ref.get('holes', [])})
    sidecar = [json.loads(line) for line in (dataset/'roof-feedback.jsonl').read_text().splitlines() if line.strip()]
    if sidecar != expected_sidecar or manifest['roof_feedback']['objects'] != len(sidecar):
        raise ValueError('Roof feedback sidecar differs from the frozen scene snapshot')
    return version, cases, dict(ignored)


def world_polygon(case):
    """One global Mercator frame for repeated captures of the same roof."""
    center = case['centroid']
    origin = np.asarray(lonlat_to_tile_xy(center['lon'], center['lat'], 17))
    scale = WORLD_SPAN_M * math.cos(math.radians(center['lat']))

    def ring(points):
        local = np.c_[points, np.ones(len(points))] @ np.asarray(case['pixel_to_meters'])
        return origin + local * [1, -1] * 2**17 / scale

    return Polygon(ring(case['ring']), [ring(h) for h in case['holes']])


def distinct_roofs(cases):
    """Count a physical roof once, even across scenes or duplicate object IDs.

    Overlap is an exclusion rule, never an inferred label. Disagreement on a
    repeated roof withholds the whole cluster. A finer image resolution wins
    otherwise; identity breaks ties without examining model scores.
    """
    parents = list(range(len(cases)))

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    polygons = [world_polygon(c) for c in cases]
    for i, a in enumerate(polygons):
        for j in range(i):
            b = polygons[j]
            overlap = a.intersection(b).area
            if overlap and (overlap / a.union(b).area >= .5 or overlap / min(a.area, b.area) >= .8):
                if cases[i]['split'] != cases[j]['split']:
                    raise ValueError('A physical roof appears in multiple dataset splits')
                parents[find(i)] = find(j)
    clusters = defaultdict(list)
    for i, case in enumerate(cases):
        clusters[find(i)].append(case)
    selected, excluded = [], []
    for cluster in clusters.values():
        def key(c):
            return abs(np.linalg.det(np.asarray(c['pixel_to_meters'])[:2])), c['scene_id'], c['object_id']
        cluster = sorted(cluster, key=key)
        conflict = len({c['family'] for c in cluster}) > 1
        if not conflict:
            selected.append(cluster[0])
        for case in cluster if conflict else cluster[1:]:
            excluded.append({'scene_id': case['scene_id'], 'object_id': case['object_id'],
                             'reason': 'conflicting_family' if conflict else 'repeated_roof'})
    return sorted(selected, key=lambda c: (c['split'], c['scene_id'], c['object_id'])), excluded
