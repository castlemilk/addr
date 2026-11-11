"""Cross-capture agreement between overlapping roof captures: diagnostics, not labels/accuracy.

Replays the production display dedupe (`scene_confidence.dedupe_key` + `suppressed_overlaps`,
the single source of truth) over every current indexed roof inside the bounds at once and
records one sidecar row per hidden roof: which shown capture hides it, IoU and Hausdorff in
local metres, and both captures' evidence signals. Rows land in the eyes index
(`data/reviews/eyes/eyes.sqlite`, table cross_capture); a build record sits beside it.

Population choice, mirroring `query()` rather than calling it: the suburb index is opened
read-only (query() would create tables/WAL), the whole bbox is paired in one pass (no 0.2 deg
windows, so pairs never straddle a window edge), no band filter is applied (query() only filters
after pairing), and stale scenes whose latest.json mtime differs from the indexed stamp are
excluded and listed, exactly as the map does. Both captures come from the same orthomosaic, so
agreement between them is not independent evidence (`same_mosaic_caveat`).

Line mismatch is null-aware: a roof with zero observed segments is indexed with mismatch 0.0, which
means "no lines", so `mismatch` is None there and the indexed value stays in `mismatch_raw`.
A build replaces the pairs of every roof it considered and drops rows whose hidden or shown roof is
no longer a current indexed roof anywhere (stale scene, re-assessed ids), so the sidecar never keeps
pairs the map no longer hides. `summary()` rates describe the last build's population (its build_id);
rows kept from earlier builds over other bounds are counted separately as `index_pairs`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np
import shapely
from shapely.geometry import Polygon, shape

from . import eyes_index
from .scene_confidence import dedupe_key, suppressed_overlaps
from .tiles import WORLD_SPAN_M

VERSION = 'cross-capture-v1'
INDEX = Path('data/suburbs/mooroolbark/confidence.sqlite')
BOUNDARY = Path('data/suburbs/mooroolbark/boundary.geojson')
BUILD = Path('data/reviews/eyes/cross-capture/build.json')
SIGNALS = {'band': 'band', 'score': 'detector_score', 'mismatch': 'image_line_mismatch_m',
           'segments': 'observed_segments', 'clipped': 'clipped', 'area_m2': 'area_m2', 'model_signature': 'model_signature'}
HIGHER = {'low': ('medium', 'high')}


def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def connection(root=Path('.')):
    """Production confidence index, read-only; never created or migrated here."""
    file = Path(root)/INDEX
    if not file.exists():
        raise FileNotFoundError(file)
    return sqlite3.connect(f'file:{quote(str(file))}?mode=ro', uri=True)


def default_bounds(root=Path('.')):
    file = Path(root)/BOUNDARY
    if not file.exists():
        raise FileNotFoundError(file)
    data = json.loads(file.read_text())
    geometries = [shape(f['geometry'] if 'geometry' in f else f) for f in data.get('features', [data])]
    return [round(v, 10) for v in shapely.union_all(geometries).bounds]


def validate_bounds(bounds):
    bounds = [float(v) for v in bounds] if len(bounds) == 4 else []
    if (not bounds or not all(math.isfinite(v) for v in bounds)
            or not -180 < bounds[0] < bounds[2] < 180 or not -85 < bounds[1] < bounds[3] < 85):
        raise ValueError('Bounds must be W S E N in degrees with W<E and S<N')
    return bounds


def local_metres(polygon, origin):
    """Project WGS84 to metres around origin (lon, lat), matching scene_confidence.geographic's scale."""
    lon0, lat0 = origin
    scale = WORLD_SPAN_M*math.cos(math.radians(lat0))
    y0 = (1-math.asinh(math.tan(math.radians(lat0)))/math.pi)/2

    def project(coords):
        lon, lat = coords[:, 0], coords[:, 1]
        x = (lon-lon0)/360*scale
        y = -((1-np.arcsinh(np.tan(np.radians(lat)))/np.pi)/2-y0)*scale
        return np.c_[x, y]
    return shapely.transform(polygon, project)


def hausdorff_m(a, b):
    origin = (a.centroid.x, a.centroid.y)
    return float(shapely.hausdorff_distance(local_metres(a, origin).boundary, local_metres(b, origin).boundary, densify=.05))


def _signals(props, prefix=''):
    out = {prefix+name: props.get(key) for name, key in SIGNALS.items()}
    segments = props.get('observed_segments')
    observed = isinstance(segments, (int, float)) and not isinstance(segments, bool) and segments > 0
    # Null-aware: zero observed segments is stored as mismatch 0.0 ("no lines"), never a perfect fit.
    out[prefix+'mismatch_raw'] = out[prefix+'mismatch']
    out[prefix+'mismatch'] = out[prefix+'mismatch'] if observed else None
    return out


def _current_scenes(root, scenes, stamps):
    return {sid for sid in scenes if (root/'data/scenes'/sid/'latest.json').exists()
            and (root/'data/scenes'/sid/'latest.json').stat().st_mtime_ns == stamps.get(sid)}


def current_roofs(root=Path('.')):
    """(scene_id, object_id) of every indexed roof whose scene stamp still matches latest.json (read-only)."""
    root = Path(root)
    with closing(connection(root)) as db:
        keys = db.execute('SELECT scene_id, object_id FROM roofs').fetchall()
        stamps = dict(db.execute('SELECT id,mtime FROM scenes'))
    current = _current_scenes(root, {sid for sid, _ in keys}, stamps)
    return {(sid, oid) for sid, oid in keys if sid in current}


def collect(root=Path('.'), bounds=None):
    """Pair every current indexed roof in bounds; returns (cross_capture rows, population meta). Writes nothing."""
    root = Path(root)
    bounds = validate_bounds(bounds if bounds is not None else default_bounds(root))
    with closing(connection(root)) as db:
        rows = db.execute('SELECT payload FROM roofs WHERE east>=? AND west<=? AND north>=? AND south<=?',
                          (bounds[0], bounds[2], bounds[1], bounds[3])).fetchall()
        stamps = dict(db.execute('SELECT id,mtime FROM scenes'))
    features = [json.loads(r[0]) for r in rows]
    scenes = {f['properties']['scene_id'] for f in features}
    current = _current_scenes(root, scenes, stamps)
    features = sorted((f for f in features if f['properties']['scene_id'] in current), key=dedupe_key)
    polygons = [Polygon(f['geometry']['coordinates'][0], f['geometry']['coordinates'][1:]) for f in features]
    now, code = datetime.now(UTC), code_sha256()
    stamp = now.isoformat(timespec='seconds')
    build_id = hashlib.sha256(json.dumps([bounds, now.isoformat(), code]).encode()).hexdigest()[:16]
    records = []
    for k, s, iou in suppressed_overlaps(features, polygons):
        hidden, shown = features[s]['properties'], features[k]['properties']
        payload = {'iou': round(iou, 4), 'hausdorff_m': round(hausdorff_m(polygons[s], polygons[k]), 3),
                   **_signals(hidden), **_signals(shown, 'other_'), 'hidden_by': 'other',
                   'band_disagreement': hidden['band'] != shown['band'], 'same_mosaic_caveat': True,
                   'version': VERSION, 'code_sha256': code, 'computed_at': stamp, 'build_id': build_id}
        records.append({'scene_id': hidden['scene_id'], 'object_id': hidden['object_id'], 'other_scene_id': shown['scene_id'],
                        'other_object_id': shown['object_id'], 'version': VERSION, 'payload': payload})
    meta = {'version': VERSION, 'bounds': bounds, 'rows': len(rows), 'current_rows': len(features),
            'scenes': len(scenes), 'stale_scenes': sorted(scenes-current), 'pairs': len(records),
            'code_sha256': code, 'computed_at': stamp, 'build_id': build_id,
            'considered': [[f['properties']['scene_id'], f['properties']['object_id']] for f in features]}
    return records, meta


def build(root=Path('.'), bounds=None):
    """Write cross_capture rows for the bounds, replacing earlier rows of every roof considered and
    dropping rows whose hidden or shown roof is no longer current anywhere in the index."""
    root = Path(root)
    records, meta = collect(root, bounds)
    considered = {tuple(key) for key in meta.pop('considered')}
    live = current_roofs(root)
    with closing(eyes_index.connection(root)) as db:
        eyes_index.ensure_schema(db)
        existing = db.execute('SELECT scene_id, object_id, other_scene_id, other_object_id FROM cross_capture WHERE version=?',
                              (VERSION,)).fetchall()
        drop = [key for key in existing if key[:2] in considered or key[:2] not in live or key[2:] not in live]
        db.executemany('DELETE FROM cross_capture WHERE version=? AND scene_id=? AND object_id=? AND other_scene_id=? AND other_object_id=?',
                       [(VERSION, *key) for key in drop])
        eyes_index.replace_rows(db, 'cross_capture', records)  # commits the deletes and inserts together
    meta['replaced_or_removed'] = len(drop)
    path = root/BUILD
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(meta, indent=1, sort_keys=True))
    temporary.replace(path)
    return {**meta, 'path': str(path)}


def summary(root=Path('.')):
    """Rates over the recorded pairs with their denominators; cross-capture agreement is not accuracy."""
    root = Path(root)
    found = []
    if eyes_index.path(root).exists():
        with closing(eyes_index.connection(root, readonly=True)) as db:
            found = eyes_index.rows(db, 'cross_capture', version=VERSION)
    record = json.loads((root/BUILD).read_text()) if (root/BUILD).exists() else None
    index_pairs = len(found)
    if record and record.get('build_id'):
        # Rates must share the last build's denominator; earlier builds' rows are only counted.
        found = [r for r in found if isinstance(r['payload'], dict) and r['payload'].get('build_id') == record['build_id']]
    else:
        record = None
    hidden = {(r['scene_id'], r['object_id']) for r in found}
    shown = {(r['other_scene_id'], r['other_object_id']) for r in found}
    payloads = [r['payload'] for r in found]
    considered = record['current_rows'] if record else None
    involved = len(hidden | shown)
    out = {'version': VERSION, 'pairs': len(found), 'hidden_roofs': len(hidden), 'shown_roofs_hiding': len(shown),
           'roofs_with_overlap': involved, 'roofs_considered': considered,
           'roofs_with_overlap_rate': involved/considered if considered else None,
           'band_disagreement_pairs': sum(bool(p.get('band_disagreement')) for p in payloads),
           'hidden_low_under_higher': sum(p.get('band') == 'low' and p.get('other_band') in HIGHER['low'] for p in payloads),
           'band_matrix': dict(sorted(Counter(f"{p.get('band')}->{p.get('other_band')}" for p in payloads).items())),
           'index_pairs': index_pairs, 'build': record}
    pop = f"{considered} indexed current roofs in bounds {record['bounds']}" if record else 'an unrecorded population (no build record)'
    out['denominators'] = [
        f"pairs: {out['pairs']} (shown -> hidden) pairs over {pop}; cross-capture agreement is a diagnostic, not accuracy",
        f"roofs_with_overlap: {involved} roofs on either side of a pair, denominator {pop}",
        f"band_disagreement_pairs: {out['band_disagreement_pairs']} of {out['pairs']} pairs; both sides share one orthomosaic, so agreement is not independent evidence",
        f"hidden_low_under_higher: {out['hidden_low_under_higher']} of {out['pairs']} pairs where the hidden roof is band low and the shown roof is medium/high",
        f"index_pairs: {index_pairs} {VERSION} rows in eyes.sqlite across all builds (earlier builds' bounds are not a denominator)"]
    return out


def _review_url(scene_id, object_id):
    return f'/scenes?scene={scene_id}&object={object_id}&kind=building&status=all&layer=model'


def hidden_low(root=Path('.')):
    """Low-band roofs hidden under a medium/high capture, read-only from the eyes index (never builds).

    The document behind `GET /api/queues/hidden`: one row per recorded pair whose hidden roof is band
    low and whose shown roof is medium or high, with both captures' evidence signals (model estimates,
    not accuracy) and `/scenes` links. Nothing here decides which capture is right, and an absent or
    schema-less index is reported as unavailable rather than created.
    """
    root = Path(root)
    note = ('hidden roof band low under a shown medium/high capture of the same roof; both share one '
            'orthomosaic, so agreement is consistency, not independent evidence; bands are model '
            'estimates, not accuracy')
    if not eyes_index.path(root).exists():
        return {'version': VERSION, 'count': 0, 'rows': [], 'index_available': False, 'note': note}
    try:
        with closing(eyes_index.connection(root, readonly=True)) as db:
            found = eyes_index.rows(db, 'cross_capture', version=VERSION)
    except sqlite3.OperationalError:
        return {'version': VERSION, 'count': 0, 'rows': [], 'index_available': False, 'note': note}
    rows = []
    for record in found:
        payload = record['payload'] if isinstance(record['payload'], dict) else {}
        if payload.get('band') != 'low' or payload.get('other_band') not in HIGHER['low']:
            continue
        rows.append({'scene_id': record['scene_id'], 'object_id': record['object_id'], 'band': payload.get('band'),
                     'detector_score': payload.get('score'), 'observed_segments': payload.get('segments'),
                     'other_scene_id': record['other_scene_id'], 'other_object_id': record['other_object_id'],
                     'other_band': payload.get('other_band'), 'other_detector_score': payload.get('other_score'),
                     'iou': payload.get('iou'), 'hidden_url': _review_url(record['scene_id'], record['object_id']),
                     'shown_url': _review_url(record['other_scene_id'], record['other_object_id'])})
    rows.sort(key=lambda r: (r['scene_id'], r['object_id'], r['other_scene_id'], r['other_object_id']))
    return {'version': VERSION, 'count': len(rows), 'rows': rows, 'index_available': True, 'note': note}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--build', action='store_true', help='Pair roofs and write cross_capture rows.')
    parser.add_argument('--bounds', type=float, nargs=4, metavar=('W', 'S', 'E', 'N'), help='Default: suburb boundary bounds.')
    parser.add_argument('--summary', action='store_true')
    parser.add_argument('--hidden-low', action='store_true',
                        help='Print low roofs hidden under a medium/high capture from the eyes index (read-only; run alone).')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.hidden_low:
        if args.build or args.summary:
            parser.error('--hidden-low prints its own document; run it without --build or --summary')
        json.dump(hidden_low(root), sys.stdout)
        sys.stdout.write('\n')
        return 0
    out = {}
    if args.build:
        out['build'] = build(root, args.bounds)
    if args.summary:
        out['summary'] = summary(root)
    if not out:
        parser.print_help()
        return 2
    json.dump(out, sys.stdout)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
