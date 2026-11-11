"""Per-object diff between the model outline a reviewer saw and the outline the reviewer saved.

For every review.json object that reference_registry.review_tier grades as a human tier (T1 confirmed with
reviewed geometry, T2 human edit) and whose ring, or holes, differ from the same-id object in
models/<review.model_signature>.json (the model snapshot the reviewer was looking at), one row: vertex counts,
IoU, area before and after, symmetric boundary distances (median, p90, max) and Hausdorff in metres through the
scene's pixel_to_meters frame, plus the review revision at which the saved outline first appeared. Rejected
objects whose outline was edited before the reject are kept as rows too, flagged `rejected`, and objects drawn
by hand with no model counterpart are rows with null geometry metrics (`model_present` false); neither enters an
agreement median. Non-human tiers (machine labels, unverified models, candidates, superseded) are counted, not
emitted.

Read-only over data/scenes; writes only data/reviews/eyes/edit-diffs/<UTC date>/ (rows.jsonl, summary.json).
Every number is agreement between the model outline and the reviewer's outline, never accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import shapely
from shapely.errors import ShapelyError

from . import reference_registry as registry

VERSION = 'review-edit-diffs-v1'
DEFAULT_OUT_ROOT = Path('data/reviews/eyes/edit-diffs')
STEP_M = .1                    # boundary sampling step for the symmetric distances
HAUSDORFF_TOLERANCE_M = .45    # the repo's threshold for "agrees with the human outline" (training.metrics)
NOTE = ("Agreement between the model outline and the reviewer's outline, per object; how far the reviewer moved "
        'the outline, not accuracy. Medians are over human-tier rows (T1/T2) with a model counterpart and valid '
        'geometry; rejected and hand-drawn rows are listed but never enter a median.')
SKIPPABLE = (KeyError, TypeError, ValueError, IndexError, AttributeError, ShapelyError)
WORST_N = 10


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def vertex_count(ring):
    """Vertices of a ring given open or closed (a repeated closing point is not a vertex)."""
    if not isinstance(ring, list) or not ring:
        return 0
    return len(ring) - 1 if len(ring) > 1 and ring[0] == ring[-1] else len(ring)


def same_holes(a, b):
    return registry._same_holes(a, b)


def pixel_size_m(scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    return float(math.sqrt(abs(np.linalg.det(frame[:2]))))


def boundary_samples(polygon, step_m=STEP_M):
    """Points every `step_m` along the exterior and every hole (local metres)."""
    points = []
    for ring in (polygon.exterior, *polygon.interiors):
        if ring.length <= 0:
            continue
        n = max(int(math.ceil(ring.length / step_m)), 4)
        points.append(shapely.line_interpolate_point(ring, np.linspace(0., ring.length, n, endpoint=False)))
    return np.concatenate(points) if points else np.zeros(0, dtype=object)


def symmetric_boundary_distances(a, b, step_m=STEP_M):
    """Distances from samples on a's boundary to b's boundary and vice versa, pooled (metres)."""
    to_b = shapely.distance(boundary_samples(a, step_m), b.boundary)
    to_a = shapely.distance(boundary_samples(b, step_m), a.boundary)
    return np.concatenate([to_b, to_a]).astype(float)


def outline_diff(model_obj, human_obj, scene, step_m=STEP_M):
    """Geometry agreement between one model object and the reviewer's version of it, in local metres."""
    model, model_repair = registry.valid(registry.local_polygon(model_obj, scene))
    human, human_repair = registry.valid(registry.local_polygon(human_obj, scene))
    if model.is_empty or human.is_empty or model.area <= 0 or human.area <= 0:
        raise ValueError('empty outline')
    distances = symmetric_boundary_distances(model, human, step_m)
    union = model.union(human).area
    return {'iou': float(model.intersection(human).area / union) if union > 0 else None,
            'area_m2_model': float(model.area), 'area_m2_human': float(human.area),
            'area_delta_m2': float(human.area - model.area),
            'boundary_distance_median_m': float(np.median(distances)),
            'boundary_distance_p90_m': float(np.percentile(distances, 90)),
            'boundary_distance_max_m': float(distances.max()),
            'hausdorff_m': float(model.boundary.hausdorff_distance(human.boundary)),
            'centroid_shift_m': float(model.centroid.distance(human.centroid)),
            'n_boundary_samples': int(distances.size),
            'geometry_repaired': {'model': model_repair, 'human': human_repair}}


def first_revision_with_outline(review, obj):
    """(revision, updated_at, updated_by) of the oldest saved review whose same-id object already had this outline."""
    target = [obj.get('ring'), obj.get('holes') or []]
    history = [h for h in (review.get('history') or []) if isinstance(h, dict)]
    history.sort(key=lambda h: (h.get('revision') is None, h.get('revision') if isinstance(h.get('revision'), int | float) else 0))
    for entry in [*history, review]:
        objects = entry.get('objects')
        if not isinstance(objects, list):
            continue
        match = next((o for o in objects if isinstance(o, dict) and o.get('id') == obj.get('id')), None)
        if match is not None and registry._same_ring(match.get('ring'), target[0]) and same_holes(match.get('holes'), target[1]):
            return entry.get('revision'), entry.get('updated_at'), entry.get('updated_by')
    return None, None, None


def object_row(scene_id, scene, review, obj, model_obj, tier, provenance, step_m=STEP_M):
    model_ring = model_obj.get('ring') if isinstance(model_obj, dict) else None
    ring_changed = model_ring is None or not registry._same_ring(obj.get('ring'), model_ring)
    holes_changed = model_ring is not None and not same_holes(obj.get('holes'), model_obj.get('holes'))
    stamp = obj.get('provenance') if isinstance(obj.get('provenance'), dict) else {}
    revision, edited_at, edited_by = first_revision_with_outline(review, obj)
    row = {'scene_id': scene_id, 'object_id': obj.get('id'), 'kind': obj.get('kind'), 'label': obj.get('label'),
           'structure_kind': obj.get('structure_kind'), 'review_status': obj.get('status'),
           'geometry_reviewed': bool(obj.get('geometry_reviewed')), 'tier': tier, 'provenance': provenance,
           'provenance_source': stamp.get('source'), 'human_confirmed': bool(stamp.get('human_confirmed')),
           'rejected': obj.get('status') == 'rejected', 'model_present': model_ring is not None,
           'edit_kind': 'manual' if model_ring is None else 'ring' if ring_changed else 'holes_only',
           'ring_changed': ring_changed, 'holes_changed': holes_changed,
           'model_vertex_count': vertex_count(model_ring) if model_ring is not None else None,
           'human_vertex_count': vertex_count(obj.get('ring')),
           'model_hole_count': len(model_obj.get('holes') or []) if model_ring is not None else None,
           'human_hole_count': len(obj.get('holes') or []),
           'iou': None, 'area_m2_model': None, 'area_m2_human': None, 'area_delta_m2': None,
           'boundary_distance_median_m': None, 'boundary_distance_p90_m': None, 'boundary_distance_max_m': None,
           'hausdorff_m': None, 'centroid_shift_m': None, 'n_boundary_samples': None, 'geometry_repaired': None,
           'geometry': 'model_absent' if model_ring is None else 'ok',
           'pixel_size_m': pixel_size_m(scene), 'model_signature': review.get('model_signature'),
           'image_sha256': scene.get('image_sha256'),
           'edited_at_revision': revision, 'edited_at': edited_at, 'edited_by': edited_by,
           'review_revision': review.get('revision'), 'review_updated_at': review.get('updated_at'),
           'review_updated_by': review.get('updated_by'),
           'object_sha256': hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()}
    row['vertex_delta'] = row['human_vertex_count'] - row['model_vertex_count'] if model_ring is not None else None
    if model_ring is not None:
        try:
            row.update(outline_diff(model_obj, obj, scene, step_m))
        except SKIPPABLE:
            row['geometry'] = 'invalid'
    return row


def scene_rows(root, scene_id, counters, kinds=None, step_m=STEP_M, cache=None):
    """Rows for one scene; `counters` receives every exclusion and denominator count."""
    root = Path(root)
    review = registry._load(root / 'data/scenes' / scene_id / 'review.json')
    if not isinstance(review, dict):
        counters['scenes_review_unreadable'] += 1
        return []
    counters['scenes_with_review'] += 1
    objects = review.get('objects')
    if not isinstance(objects, list):
        counters['scenes_review_objects_not_list'] += 1
        return []
    scene = registry.scene_model(root, scene_id, review.get('model_signature'), cache)
    model_objects = scene.get('objects') if isinstance(scene, dict) else None
    if not isinstance(model_objects, list) or scene.get('model_signature') != review.get('model_signature'):
        # The snapshot the reviewer saw is gone and latest.json is another model: a differing ring proves nothing.
        counters['scenes_model_unverified'] += 1
        counters['excluded:model_unverified'] += sum(isinstance(o, dict) for o in objects)
        return []
    by_id = {o.get('id'): o for o in model_objects if isinstance(o, dict)}
    rows = []
    for obj in objects:
        if not isinstance(obj, dict):
            counters['excluded:object_not_dict'] += 1
            continue
        if kinds and obj.get('kind') not in kinds:
            counters['excluded:kind_filtered'] += 1
            continue
        counters['objects_seen'] += 1
        model_obj = by_id.get(obj.get('id'))
        model_ring = model_obj.get('ring') if isinstance(model_obj, dict) else None
        try:
            tier, provenance, _ = registry.review_tier(
                obj, model_ring, True, model_holes=(model_obj.get('holes') or []) if model_ring is not None else None)
        except SKIPPABLE:
            counters['excluded:tier_error'] += 1
            continue
        changed = (model_ring is None or not registry._same_ring(obj.get('ring'), model_ring)
                   or not same_holes(obj.get('holes'), model_obj.get('holes')))
        human = tier in registry.LABEL_TIERS
        rejected = obj.get('status') == 'rejected'
        if not changed:
            counters[f'unchanged:{tier or provenance}'] += 1
            continue
        if not human and not rejected:
            counters[f'excluded_changed:{provenance}'] += 1
            continue
        rows.append(object_row(scene_id, scene, review, obj, model_obj, tier, provenance, step_m))
    return rows


def list_scene_ids(root, scene_ids=None):
    if scene_ids:
        return [s for s in dict.fromkeys(scene_ids) if registry.SCENE_ID.fullmatch(s)]
    return sorted(p.parent.name for p in (Path(root) / 'data/scenes').glob('*/review.json')
                  if registry.SCENE_ID.fullmatch(p.parent.name) and not p.parent.name.startswith('qa_'))


def build(root=Path('.'), scene_ids=None, kinds=None, step_m=STEP_M):
    """(rows, counters) over every scene with a review.json (or the given ids)."""
    root, counters, cache, rows = Path(root), Counter(), {}, []
    for scene_id in list_scene_ids(root, scene_ids):
        counters['scenes_seen'] += 1
        rows.extend(scene_rows(root, scene_id, counters, kinds, step_m, cache))
    return rows, counters


def _median(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    return float(statistics.median(values)) if values else None


def agreement(rows):
    """Medians with explicit denominators over rows that carry geometry; empty sets are None, never 0."""
    rows = [r for r in rows if r['geometry'] == 'ok' and r['iou'] is not None]
    return {'n': len(rows), 'iou_median': _median(r['iou'] for r in rows),
            'boundary_distance_median_m_median': _median(r['boundary_distance_median_m'] for r in rows),
            'boundary_distance_p90_m_median': _median(r['boundary_distance_p90_m'] for r in rows),
            'hausdorff_m_median': _median(r['hausdorff_m'] for r in rows),
            'centroid_shift_m_median': _median(r['centroid_shift_m'] for r in rows),
            'area_delta_m2_median': _median(r['area_delta_m2'] for r in rows),
            'vertex_delta_median': _median(r['vertex_delta'] for r in rows),
            'n_iou_lt_0_9': sum(r['iou'] < .9 for r in rows),
            'n_hausdorff_ge_0_45_m': sum(r['hausdorff_m'] >= HAUSDORFF_TOLERANCE_M for r in rows),
            'n_human_vertex_count_4': sum(r['human_vertex_count'] == 4 for r in rows)}


def summarize(rows, counters, step_m=STEP_M):
    human = [r for r in rows if r['tier'] in registry.LABEL_TIERS]
    paired = [r for r in human if r['model_present']]
    by_kind = sorted({r['kind'] for r in paired}, key=str)
    worst = sorted((r for r in paired if r['geometry'] == 'ok'), key=lambda r: -(r['boundary_distance_p90_m'] or 0))[:WORST_N]
    return {'version': VERSION, 'note': NOTE, 'step_m': step_m, 'hausdorff_tolerance_m': HAUSDORFF_TOLERANCE_M,
            'n_scenes_seen': counters.get('scenes_seen', 0), 'n_scenes_with_review': counters.get('scenes_with_review', 0),
            'n_scenes_model_unverified': counters.get('scenes_model_unverified', 0),
            'n_objects_seen': counters.get('objects_seen', 0),
            'n_rows': len(rows), 'n_human_tier_rows': len(human), 'n_rejected_rows': sum(r['rejected'] for r in rows),
            'n_manual_rows': sum(not r['model_present'] for r in rows),
            'n_human_tier_with_model_pair': len(paired),
            'n_human_tier_ring_changed': sum(r['ring_changed'] and r['model_present'] for r in human),
            'n_human_tier_holes_only_changed': sum(r['edit_kind'] == 'holes_only' for r in human),
            'n_invalid_geometry': sum(r['geometry'] == 'invalid' for r in rows),
            'by_tier': dict(sorted(Counter(str(r['tier']) for r in rows).items())),
            'by_provenance': dict(sorted(Counter(r['provenance'] for r in rows).items())),
            'by_kind': dict(sorted(Counter(str(r['kind']) for r in rows).items())),
            'by_review_status': dict(sorted(Counter(str(r['review_status']) for r in rows).items())),
            'by_edit_kind': dict(sorted(Counter(r['edit_kind'] for r in rows).items())),
            'unchanged_by_tier': {k.split(':', 1)[1]: v for k, v in sorted(counters.items()) if k.startswith('unchanged:')},
            'excluded_changed_by_provenance': {k.split(':', 1)[1]: v for k, v in sorted(counters.items())
                                               if k.startswith('excluded_changed:')},
            'excluded': {k.split(':', 1)[1]: v for k, v in sorted(counters.items()) if k.startswith('excluded:')},
            'agreement_human_tier': agreement(paired),
            'agreement_by_kind': {k: agreement([r for r in paired if r['kind'] == k]) for k in by_kind},
            'agreement_by_tier': {t: agreement([r for r in paired if r['tier'] == t]) for t in registry.LABEL_TIERS},
            'worst_by_boundary_distance_p90': [
                {k: r[k] for k in ('scene_id', 'object_id', 'kind', 'tier', 'boundary_distance_p90_m', 'hausdorff_m', 'iou')}
                for r in worst]}


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    with os.fdopen(handle, 'w') as out:
        out.write(text)
    os.replace(temporary, path)


def write(out, rows, summary):
    """rows.jsonl then summary.json (which hashes the rows file) under `out`; returns the summary."""
    out = Path(out)
    rows_path = out / 'rows.jsonl'
    _write_atomic(rows_path, ''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    summary = {**summary, 'created_at': datetime.now(UTC).isoformat(), 'code_sha256': code_sha256(),
               'out': str(out), 'files': {'rows.jsonl': hashlib.sha256(rows_path.read_bytes()).hexdigest()}}
    _write_atomic(out / 'summary.json', json.dumps(summary, indent=2, sort_keys=True))
    return summary


def default_out(root):
    return Path(root) / DEFAULT_OUT_ROOT / datetime.now(UTC).strftime('%Y-%m-%d')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--out', help='Output folder (default <root>/data/reviews/eyes/edit-diffs/<UTC date>).')
    parser.add_argument('--scene-ids', nargs='*', help='Restrict to these scene ids.')
    parser.add_argument('--kind', action='append', help='Only objects of this kind (repeatable; default every kind).')
    parser.add_argument('--step-m', type=float, default=STEP_M, help='Boundary sampling step in metres.')
    args = parser.parse_args(argv)
    if not (args.step_m > 0):
        parser.error('--step-m must be positive')
    rows, counters = build(args.root, args.scene_ids, args.kind, args.step_m)
    summary = write(args.out or default_out(args.root), rows, summarize(rows, counters, args.step_m))
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
