"""Reference agreement: model roofs vs tiered references in local metres (not accuracy).

IoU, Hausdorff and roof-line precision/recall against T1/T2 references describe
agreement with those few human references only; T3 rows are observation agreement
and never an accuracy denominator; bands are diagnostics. Reads confidence.sqlite
and scene models read-only; writes only the eyes.sqlite `agreement` table and
data/reviews/eyes/agreement/**.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon, shape

from . import eyes_index
from . import reference_registry as registry
from .scene_confidence import geographic
from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy

VERSION = 'reference-agreement-v1'
MIN_IOU = .3
LINE_DISTANCE_M = .5
LINE_ANGLE_DEG = 10.
ANECDOTAL_N = 20
COPY_HAUSDORFF_M = .05  # independent captures never agree this closely; flags derived/copied geometry
DEFAULT_TIERS = ('T1', 'T2', 'T3')
LINE_RULE = 'shorter segment endpoints and midpoint within 0.5 m of the longer segment; angle <= 10 deg; one-to-many'
MODEL_FIELDS = ('band', 'detector_score', 'image_line_mismatch_m', 'observed_segments', 'clipped', 'area_m2', 'profile',
                'reasons', 'geometry_signature')
NOTE = ('Reference agreement for the listed references only, not calibrated accuracy. T3 is observation agreement and '
        'never an accuracy denominator; bands are diagnostics.')


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def lonlat_to_local(points, scene):
    """[lon, lat] -> local scene metres; the inverse of scene_confidence.geographic after the pixel step."""
    center = scene['centroid']
    origin = np.array(lonlat_to_tile_xy(center['lon'], center['lat'], 0))
    scale = WORLD_SPAN_M*math.cos(math.radians(center['lat']))
    return [((np.array(lonlat_to_tile_xy(p[0], p[1], 0)) - origin)*scale*[1, -1]).tolist() for p in points]


def _round(value, digits=4):
    return None if value is None else round(float(value), digits)


def _iou(a, b):
    overlap = a.intersection(b).area
    return overlap/max(1e-20, a.area + b.area - overlap)


def _same_frame(ref, scene):
    payload = ref['payload']
    return bool(ref.get('scene_id') and ref.get('scene_id') == scene.get('id') and payload.get('geometry_local_m')
                and payload.get('image_sha256') and payload.get('image_sha256') == scene.get('image_sha256'))


def reference_polygon(ref, scene):
    """Reference outline in the scene's local metres (exact when drawn in that frame)."""
    payload = ref['payload']
    if _same_frame(ref, scene):
        return registry.valid(shape(payload['geometry_local_m']))[0]
    # valid() reduces any stored geometry (including a foreign MultiPolygon) to one Polygon first.
    polygon = registry.valid(shape(payload['geometry_wgs84']))[0]
    rings = [polygon.exterior.coords, *(hole.coords for hole in polygon.interiors)]
    return registry.valid(Polygon(lonlat_to_local(rings[0], scene), [lonlat_to_local(h, scene) for h in rings[1:]]))[0]


def _segment(points):
    """Two finite 2-D points as [[x, y], [x, y]], or None when the value is not a usable segment."""
    try:
        if len(points) != 2:
            return None
        segment = [[float(v) for v in p[:2]] for p in points]
    except (TypeError, ValueError, IndexError):
        return None
    if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in segment):
        return None
    return segment


def reference_edges(ref, scene):
    """Reference edges in local metres; edges missing the needed coordinates are skipped."""
    same = _same_frame(ref, scene)
    edges = []
    for edge in ref['payload'].get('edges') or []:
        if not isinstance(edge, dict):
            continue
        points = None
        if same and edge.get('a_xy_m') is not None and edge.get('b_xy_m') is not None:
            points = _segment([edge['a_xy_m'], edge['b_xy_m']])
        elif edge.get('a_lonlat') is not None and edge.get('b_lonlat') is not None:
            lonlat = _segment([edge['a_lonlat'], edge['b_lonlat']])
            points = _segment(lonlat_to_local(lonlat, scene)) if lonlat else None
        if points:
            edges.append((points, edge.get('kind')))
    return edges


def model_lines(obj):
    """(segment, kind) for each well-formed roof_lines entry of a model object."""
    lines = obj.get('roof_lines')
    found = []
    for line in lines if isinstance(lines, list) else []:
        segment = _segment(line.get('line_meters')) if isinstance(line, dict) else None
        if segment:
            found.append((segment, line.get('kind')))
    return found


def image_identity(ref, scene):
    payload = ref['payload']
    source = payload.get('image_sha256') or payload.get('boundary_image_sha256')
    target = scene.get('image_sha256')
    if not source or not target:
        return 'unknown'
    return 'same_image' if source == target else 'different_image'


def model_eave_m(obj):
    """Model eave estimate: per-part eave_m when stored, else the lowest roof-part vertex height."""
    parts = obj.get('roof_parts')
    parts = [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    eaves = [p['eave_m'] for p in parts if number(p.get('eave_m'))]
    if eaves:
        return float(statistics.median(eaves))
    heights = [z for p in parts if isinstance(p.get('ring_z'), list) for z in p['ring_z'] if number(z)]
    return float(min(heights)) if heights else None


def _point_segment(point, a, b):
    direction = b - a
    t = np.clip(np.dot(point - a, direction)/max(1e-20, np.dot(direction, direction)), 0, 1)
    return float(np.linalg.norm(point - (a + t*direction)))


def lines_match(first, second, distance=LINE_DISTANCE_M, angle=LINE_ANGLE_DEG):
    s, t = np.asarray(first, dtype=float)[:, :2], np.asarray(second, dtype=float)[:, :2]
    ds, dt = s[1] - s[0], t[1] - t[0]
    ls, lt = float(np.linalg.norm(ds)), float(np.linalg.norm(dt))
    if ls < 1e-9 or lt < 1e-9:
        return False
    if math.degrees(math.acos(min(1., abs(float(np.dot(ds, dt)))/(ls*lt)))) > angle:
        return False
    short, long = (s, t) if ls <= lt else (t, s)
    return max(_point_segment(p, long[0], long[1]) for p in (short[0], short[1], short.mean(axis=0))) <= distance


def line_metrics(model_lines, ref_edges, distance=LINE_DISTANCE_M, angle=LINE_ANGLE_DEG):
    """Precision/recall of model roof lines vs reference edges; kind precision also needs equal kinds.

    `distance` (m) and `angle` (deg) forward to lines_match; the defaults are the repo rule (0.5 m, 10 deg).
    """
    if not ref_edges:
        return {'line_precision': None, 'line_recall': None, 'line_kind_precision': None,
                'n_model_lines': len(model_lines), 'n_ref_edges': 0, 'n_matched_model_lines': None, 'n_matched_ref_edges': None}
    matches = [[lines_match(m, r, distance, angle) for r, _ in ref_edges] for m, _ in model_lines]
    matched_model = sum(any(row) for row in matches)
    kind_model = sum(any(hit and model_lines[i][1] == ref_edges[j][1] for j, hit in enumerate(row)) for i, row in enumerate(matches))
    matched_ref = sum(any(matches[i][j] for i in range(len(model_lines))) for j in range(len(ref_edges)))
    n = len(model_lines)
    return {'line_precision': _round(matched_model/n) if n else None, 'line_recall': _round(matched_ref/len(ref_edges)),
            'line_kind_precision': _round(kind_model/n) if n else None, 'n_model_lines': n, 'n_ref_edges': len(ref_edges),
            'n_matched_model_lines': matched_model, 'n_matched_ref_edges': matched_ref}


def metrics(ref, obj, scene):
    """Outline, roof-form, eave and roof-line agreement of one model object with one reference, in local metres."""
    reference = reference_polygon(ref, scene)
    model = registry.valid(registry.local_polygon(obj, scene))[0]
    payload = ref['payload']
    ref_type, model_type = payload.get('roof_type'), obj.get('roof_type')
    ref_eave, model_eave = payload.get('eave_height_m'), model_eave_m(obj)
    hausdorff = reference.boundary.hausdorff_distance(model.boundary)
    identity = image_identity(ref, scene)
    return {'iou': _round(_iou(reference, model)), 'hausdorff_m': _round(hausdorff),
            'area_ratio': _round(model.area/reference.area) if reference.area > 0 else None,
            'centroid_shift_m': _round(reference.centroid.distance(model.centroid)),
            'ref_area_m2': _round(reference.area, 2), 'model_area_m2': _round(model.area, 2),
            'roof_type_match': None if not ref_type or not model_type else ref_type == model_type,
            'ref_roof_type': ref_type, 'model_roof_type': model_type,
            'eave_delta_m': None if ref_eave is None or model_eave is None else _round(model_eave - ref_eave),
            'ref_eave_m': ref_eave, 'model_eave_m': _round(model_eave),
            **line_metrics(model_lines(obj), reference_edges(ref, scene)),
            'image_identity': identity, 'frame': 'scene_local' if _same_frame(ref, scene) else 'converted_wgs84',
            'geometry_copy_suspected': identity != 'same_image' and hausdorff < COPY_HAUSDORFF_M}


def _owned(scene):
    return set(scene.get('owned_object_ids') or [o.get('id') for o in scene.get('objects', [])])


def candidates(ref, root=Path('.'), scene_ids=(), cache=None, min_iou=MIN_IOU):
    """Model objects overlapping a reference (IoU >= min_iou): indexed roofs by bbox, plus buildings in
    `scene_ids` and the reference's own scene read from their latest.json."""
    root, cache = Path(root), {} if cache is None else cache
    polygon = registry.valid(shape(ref['payload']['geometry_wgs84']))[0]
    west, south, east, north = polygon.bounds
    found = {}
    database = root/'data/suburbs/mooroolbark/confidence.sqlite'
    if database.exists():
        with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as db:
            records = db.execute('SELECT scene_id, object_id, payload FROM roofs WHERE east>=? AND west<=? AND north>=? AND south<=?',
                                 (west, east, south, north)).fetchall()
            stamps = dict(db.execute('SELECT id, mtime FROM scenes'))
        for scene_id, object_id, raw in records:
            try:
                feature = json.loads(raw)
                props = feature.get('properties') or {}
                iou = _iou(polygon, registry.valid(shape(feature['geometry']))[0])
            except registry.SKIPPABLE:
                continue  # a corrupt index row is not a candidate; the index itself is never repaired here
            if iou < min_iou:
                continue
            scene = registry.scene_model(root, scene_id, props.get('model_signature'), cache) if isinstance(scene_id, str) else None
            objects = scene.get('objects') if isinstance(scene, dict) else None
            obj = next((o for o in (objects if isinstance(objects, list) else []) if isinstance(o, dict) and o.get('id') == object_id), None)
            if obj is None:
                continue
            latest = root/'data/scenes'/scene_id/'latest.json'
            stale = not latest.exists() or latest.stat().st_mtime_ns != stamps.get(scene_id)
            found[(scene_id, object_id)] = {'scene_id': scene_id, 'object_id': object_id, 'iou_wgs84': iou, 'indexed': True,
                                            'stale_index': stale, 'confidence': props, 'obj': obj, 'scene': scene,
                                            'model_signature': props.get('model_signature') or scene.get('model_signature')}
    extra = [*scene_ids, *([ref['scene_id']] if ref.get('scene_id') else [])]
    for scene_id in dict.fromkeys(extra):
        scene = registry.scene_model(root, scene_id, None, cache)
        if not scene:
            continue
        owned = _owned(scene)
        objects = scene.get('objects')
        for obj in objects if isinstance(objects, list) else []:
            if not isinstance(obj, dict):
                continue
            key = (scene_id, obj.get('id'))
            if (obj.get('kind') != 'building' or obj.get('status') in ('rejected', 'superseded') or obj.get('id') not in owned
                    or key in found):
                continue
            try:
                other = registry.valid(geographic(obj, scene))[0]
            except registry.SKIPPABLE:
                continue
            iou = _iou(polygon, other)
            if iou >= min_iou:
                found[key] = {'scene_id': scene_id, 'object_id': obj['id'], 'iou_wgs84': iou, 'indexed': False, 'stale_index': None,
                              'confidence': None, 'obj': obj, 'scene': scene, 'model_signature': scene.get('model_signature')}
    return sorted(found.values(), key=lambda c: (-c['iou_wgs84'], c['scene_id'], c['object_id']))


def references(root=Path('.')):
    """Registry rows from eyes.sqlite, or an in-memory registry build when none is written."""
    root = Path(root)
    if eyes_index.path(root).exists():
        with closing(eyes_index.connection(root, readonly=True)) as db:
            try:
                rows = eyes_index.rows(db, 'references', version=registry.VERSION)
            except sqlite3.OperationalError:
                rows = []
        if rows:
            return rows
    return registry.build(root)


def select(refs, tiers=DEFAULT_TIERS, bounds=None, include_model_outputs=False):
    """Building references with independent geometry (T3 model-output echoes excluded unless asked)."""
    chosen = []
    for ref in refs:
        payload = ref.get('payload')
        if not isinstance(payload, dict) or ref.get('tier') not in tiers or payload.get('object_kind') != 'building':
            continue
        if not include_model_outputs and not payload.get('independent_geometry'):
            continue
        if bounds and (ref['east'] < bounds[0] or ref['west'] > bounds[2] or ref['north'] < bounds[1] or ref['south'] > bounds[3]):
            continue
        chosen.append(ref)
    return sorted(chosen, key=lambda r: r['ref_id'])


def _median(values):
    return _round(statistics.median(values)) if values else None


def summarize(refs, rows):
    """Per-tier agreement with explicit denominators, using each reference's best-IoU candidate."""
    best, errors = {}, Counter()
    for row in rows:
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        if payload.get('iou') is None:
            errors[row['ref_id']] += 1  # metric failure rows carry no IoU and never count as a candidate
            continue
        current = best.get(row['ref_id'])
        if current is None or payload['iou'] > current['iou']:
            best[row['ref_id']] = payload
    by_tier = {}
    for tier in sorted({r['tier'] for r in refs}):
        members = [r for r in refs if r['tier'] == tier]
        bests = [best[r['ref_id']] for r in members if r['ref_id'] in best]
        ious = [b['iou'] for b in bests]
        hausdorffs = [b['hausdorff_m'] for b in bests if b.get('hausdorff_m') is not None]
        n, n85 = len(members), sum(i >= .85 for i in ious)
        by_tier[tier] = {'tier_name': registry.TIERS.get(tier, tier), 'n_refs': n, 'n_with_candidate': len(bests),
                         'n_matched_iou_ge_0_5': sum(i >= .5 for i in ious), 'n_iou_ge_0_85': n85,
                         'share_iou_ge_0_85': _round(n85/n) if n else None, 'share_denominator': n,
                         'median_iou': _median(ious), 'median_hausdorff_m': _median(hausdorffs),
                         'median_denominator': len(bests),
                         'by_image_identity': dict(sorted(Counter(b.get('image_identity') for b in bests).items())),
                         'n_best_copy_suspected': sum(bool(b.get('geometry_copy_suspected')) for b in bests),
                         'n_metric_errors': sum(errors[r['ref_id']] for r in members),
                         'anecdotal': n < ANECDOTAL_N, 'flag': f'anecdotal (n<{ANECDOTAL_N})' if n < ANECDOTAL_N else None,
                         'accuracy_denominator': tier in registry.LABEL_TIERS}
    return {'version': VERSION, 'n_refs': len(refs), 'n_rows': len(rows), 'by_tier': by_tier, 'min_candidate_iou': MIN_IOU,
            'line_match_rule': LINE_RULE, 'note': NOTE}


def agreement_rows(ref, found):
    """Agreement rows for one reference's candidates; best_for_ref marks the highest metric IoU."""
    stamp = {'code_sha256': code_sha256(), 'computed_at': datetime.now(UTC).isoformat()}
    rows = []
    for candidate in found:
        obj, props = candidate['obj'], candidate['confidence']
        try:
            measured = metrics(ref, obj, candidate['scene'])
        except registry.SKIPPABLE as exc:
            # A degenerate object is recorded, not fatal: iou None keeps it out of every summary count.
            measured = {'iou': None, 'hausdorff_m': None, 'error': f'{type(exc).__name__}: {exc}',
                        'image_identity': image_identity(ref, candidate['scene'])}
        payload = {**measured, 'ref_tier': ref['tier'], 'ref_source_kind': ref['source_kind'],
                   'ref_provenance': ref['payload'].get('provenance'), 'ref_scene_id': ref.get('scene_id'),
                   'ref_object_id': ref.get('object_id'), 'ref_crm_feature_id': ref.get('crm_feature_id'),
                   'ref_address_id': ref.get('address_id'), 'indexed': candidate['indexed'], 'stale_index': candidate['stale_index'],
                   'iou_wgs84': _round(candidate['iou_wgs84']), 'detector_score_raw': obj.get('score'),
                   'model': {k: props.get(k) for k in MODEL_FIELDS} if props else None,
                   'line_match_rule': LINE_RULE, 'min_candidate_iou': MIN_IOU, **stamp}
        rows.append({'ref_id': ref['ref_id'], 'scene_id': candidate['scene_id'], 'object_id': candidate['object_id'],
                     'model_signature': candidate['model_signature'], 'version': VERSION, 'payload': payload})
    ranked = sorted(rows, key=lambda r: (r['payload']['iou'] is None, -(r['payload']['iou'] or 0), r['scene_id'], r['object_id']))
    for rank, row in enumerate(ranked):
        row['payload']['rank_by_iou'] = rank
        row['payload']['best_for_ref'] = rank == 0 and row['payload']['iou'] is not None
    return ranked


def agreement_id(rows):
    stable = [{**r, 'payload': {k: v for k, v in r['payload'].items() if k != 'computed_at'}}
              for r in sorted(rows, key=lambda r: (r['ref_id'], r['scene_id'], r['object_id']))]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def build(root=Path('.'), scene_ids=(), tiers=DEFAULT_TIERS, bounds=None, include_model_outputs=False):
    """Compute and store agreement rows for the selected references; returns the tier summary."""
    root = Path(root)
    refs = references(root)
    selected = select(refs, tiers, bounds, include_model_outputs)
    cache, rows = {}, []
    for ref in selected:
        rows.extend(agreement_rows(ref, candidates(ref, root, scene_ids, cache)))
    identifier = agreement_id(rows)
    summary = {**summarize(selected, rows), 'agreement_id': identifier, 'scene_ids': list(scene_ids), 'tiers': list(tiers),
               'bounds': list(bounds) if bounds else None, 'code_sha256': code_sha256(), 'computed_at': datetime.now(UTC).isoformat()}
    known, chosen = {r['ref_id'] for r in refs}, {r['ref_id'] for r in selected}
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        existing = {r[0] for r in db.execute('SELECT DISTINCT ref_id FROM agreement WHERE version=?', (VERSION,))}
        stale = sorted((existing & chosen) | (existing - known))
        db.executemany('DELETE FROM agreement WHERE version=? AND ref_id=?', [(VERSION, r) for r in stale])
        eyes_index.replace_rows(db, 'agreement', rows)
    folder = root/'data/reviews/eyes/agreement'/identifier
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'agreement.jsonl').write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
    temporary = folder/'summary.json.tmp'
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True))
    temporary.replace(folder/'summary.json')
    return summary


def stored_summary(root=Path('.'), tiers=DEFAULT_TIERS, bounds=None, include_model_outputs=False):
    root = Path(root)
    with closing(eyes_index.connection(root, readonly=True)) as db:
        refs = eyes_index.rows(db, 'references', version=registry.VERSION)
        rows = eyes_index.rows(db, 'agreement', version=VERSION)
    selected = select(refs, tiers, bounds, include_model_outputs)
    chosen = {r['ref_id'] for r in selected}
    return summarize(selected, [r for r in rows if r['ref_id'] in chosen])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Model vs reference roof agreement (reference agreement, not accuracy).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--summary', action='store_true', help='Summarize stored agreement rows by tier.')
    parser.add_argument('--scene-ids', nargs='*', default=[], help='Extra non-indexed scenes whose buildings are candidates.')
    parser.add_argument('--tiers', nargs='+', default=list(DEFAULT_TIERS), choices=sorted(registry.TIERS))
    parser.add_argument('--bounds', nargs=4, type=float, metavar=('WEST', 'SOUTH', 'EAST', 'NORTH'))
    parser.add_argument('--include-model-outputs', action='store_true', help='Also compare T3 rows that echo model geometry.')
    args = parser.parse_args(argv)
    root = Path(args.root)
    options = {'tiers': tuple(args.tiers), 'bounds': args.bounds, 'include_model_outputs': args.include_model_outputs}
    if args.build:
        summary = build(root, tuple(args.scene_ids), **options)
    elif args.summary:
        if not eyes_index.path(root).exists():
            parser.error('No eyes index yet; run --build first')
        summary = stored_summary(root, **options)
    else:
        parser.print_help()
        return 2
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
