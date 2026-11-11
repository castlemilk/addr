"""Versioned model-evidence bands and geographically gated accuracy calibration.

Evidence scores are diagnostics, never probabilities or training labels.
Intervals, when available, describe validation-group boundary success rates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import Polygon, mapping
from shapely.strtree import STRtree

from .scene_batch import signature
from .scene_evaluation import evaluate_objects
from .scene_self_review import audit_scene
from .scene_training import atomic_json, collect_scenes
from .tiles import WORLD_SPAN_M, lonlat_to_tile_xy, tile_xy_to_lonlat

VERSION = 'roof-evidence-v1'
BANDS = ('low', 'medium', 'high', 'unassessed')
COLORS = {'low': '#df5947', 'medium': '#c58913', 'high': '#087f8c', 'unassessed': '#64748b'}
LABELS = {'low': 'Low evidence', 'medium': 'Mixed evidence', 'high': 'Stronger evidence', 'unassessed': 'Not assessed'}
MIN_CALIBRATION_GROUPS = 20


def geometry_signature(obj):
    return signature({k: obj.get(k) for k in ('ring', 'holes', 'roof_parts', 'roof_annotation', 'roof_lines', 'roof_type', 'model')})


def evidence(obj, audit):
    flags = {i['code'] for i in audit['issues']}
    reasons = [i['message'] for i in audit['issues']]
    score = obj.get('score')
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1:
        return {'band': 'unassessed', 'reasons': ['Detector support is unavailable.'], 'detector_score': None}
    cost = audit.get('cost', {})
    if not audit.get('geometry_passed') or 'image_cut' in flags or score < .65:
        band = 'low'
        if score < .65:
            reasons.append('Detector support is below 0.65; this score is not calibrated accuracy.')
    elif (score >= .8 and cost.get('observed_segments', 0) >= 3
          and cost.get('image_line_mismatch_m', 999) <= .35 and not flags):
        band = 'high'
        reasons = ['Geometry checks pass and multiple registered image lines support the model.']
    else:
        band = 'medium'
        if not reasons:
            reasons = ['Geometry checks pass, but image or detector support is not strong enough for the highest band.']
    return {'band': band, 'reasons': reasons, 'detector_score': round(score, 4),
            'geometry_passed': audit.get('geometry_passed', False),
            'image_line_mismatch_m': cost.get('image_line_mismatch_m'),
            'observed_segments': cost.get('observed_segments', 0)}


def profile(scene):
    analysis = scene.get('analysis', {})
    # Match the frozen inference components; don't combine unrelated model generations.
    return signature({k: v for k, v in analysis.items() if k.endswith('_code') or k in ('pipeline', 'roof_model', 'input_geometry', 'inference_profile')})


def geographic(obj, scene):
    center = scene['centroid']
    origin = np.array(lonlat_to_tile_xy(center['lon'], center['lat'], 0))
    scale = WORLD_SPAN_M*math.cos(math.radians(center['lat']))
    def ring(points):
        xy = np.c_[points, np.ones(len(points))] @ np.asarray(scene['pixel_to_meters'])
        return [list(tile_xy_to_lonlat(*(origin + p*[1, -1]/scale), 0)) for p in xy]
    return Polygon(ring(obj['ring']), [ring(h) for h in obj.get('holes', [])])


def connection(root):
    folder = root/'data/suburbs/mooroolbark'
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder/'confidence.sqlite', timeout=30)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS scenes(id TEXT PRIMARY KEY, signature TEXT, mtime INTEGER, bounds TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS roofs(scene_id TEXT, object_id TEXT, west REAL, south REAL, east REAL, north REAL, payload TEXT, PRIMARY KEY(scene_id,object_id))')
    db.execute('CREATE INDEX IF NOT EXISTS roof_bounds ON roofs(west,east,south,north)')
    return db


def assess(scene_id, root=Path('.'), *, index=True):
    """Write confidence.json for one scene; index=False (captures) leaves the suburb confidence.sqlite untouched."""
    from .scene_self_review import validate_id
    folder = root/'data/scenes'/validate_id(scene_id)
    source = (folder/'latest.json').read_bytes()
    scene = json.loads(source)
    from .scene_split import image_split_audit
    if image_split_audit(scene, 'unassigned')['status'] == 'invalid_frame':
        raise ValueError('Invalid geographic frame')
    if len(scene['image_sha256']) != 64 or any(c not in '0123456789abcdef' for c in scene['image_sha256']):
        raise ValueError('Invalid imagery identity')
    image_path = folder/f"{scene['image_sha256']}.png"
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != scene['image_sha256']:
        raise ValueError('Source imagery changed')
    with Image.open(image_path) as image:
        if image.size != (scene['image_width'], scene['image_height']):
            raise ValueError('Imagery dimensions changed')
        rows = audit_scene(scene, image.convert('RGB'))
    audited = {r['object_id']: r for r in rows if r['kind'] == 'building'}
    report = {'version': VERSION, 'model_signature': scene['model_signature'], 'profile': profile(scene),
              'image_sha256': scene['image_sha256'], 'objects': []}
    owned = set(scene.get('owned_object_ids', [o['id'] for o in scene['objects']]))
    features = []
    for obj in scene['objects']:
        if obj['id'] not in audited:
            continue
        row = {'object_id': obj['id'], 'geometry_signature': geometry_signature(obj), **evidence(obj, audited[obj['id']])}
        report['objects'].append(row)
        if obj['id'] not in owned:
            continue
        polygon = geographic(obj, scene)
        if not polygon.is_valid or polygon.area <= 0:
            continue
        props = {**row, 'scene_id': scene_id, 'label': obj['label'], 'capture_title': scene['title'],
                 'model_signature': scene['model_signature'], 'profile': report['profile'], 'color': COLORS[row['band']],
                 'band_label': LABELS[row['band']], 'clipped': any(i['code'] == 'image_cut' for i in audited[obj['id']]['issues']),
                 'area_m2': audited[obj['id']].get('area_m2', 0),
                 'review_url': f'/scenes?scene={scene_id}&object={obj["id"]}&kind=building&status=all&layer=model'}
        features.append((polygon, {'type': 'Feature', 'geometry': mapping(polygon), 'properties': props}))
    if (folder/'latest.json').read_bytes() != source:
        raise ValueError('Model changed during confidence assessment')
    atomic_json(folder/'confidence.json', report)
    if not index:
        return report
    extent = geographic({'ring': [[0, 0], [scene['image_width'], 0], [scene['image_width'], scene['image_height']], [0, scene['image_height']]]}, scene)
    with closing(connection(root)) as db, db:
        db.execute('DELETE FROM roofs WHERE scene_id=?', (scene_id,))
        db.executemany('INSERT INTO roofs VALUES(?,?,?,?,?,?,?)',
                       [(scene_id, f['properties']['object_id'], *p.bounds, json.dumps(f)) for p, f in features])
        db.execute('INSERT OR REPLACE INTO scenes VALUES(?,?,?,?)',
                   (scene_id, scene['model_signature'], (folder/'latest.json').stat().st_mtime_ns, json.dumps(extent.bounds)))
    return report


def wilson(successes, total):
    if not isinstance(total, int) or not isinstance(successes, int) or not 0 <= successes <= total:
        raise ValueError('Use valid binomial counts')
    if not total:
        return None
    z = 1.959963984540054
    p, d = successes/total, 1+z*z/total
    center = (p+z*z/(2*total))/d
    half = z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/d
    return [max(0., center-half), min(1., center+half)]


def calibrate(root=Path('.')):
    encoded, _, coverage = collect_scenes(root)
    eligible = {(r['scene_id'], r['kind']) for c in coverage['classes'] for r in c['layers']
                if r['kind'] == 'building' and r['split'] == 'validation' and r['eligible']}
    groups = {}
    for line in encoded.splitlines():
        row = json.loads(line)
        if (row['scene_id'], 'building') not in eligible:
            continue
        folder = root/'data/scenes'/row['scene_id']
        path = folder/'confidence.json'
        if not path.exists():
            continue
        confidence = json.loads(path.read_text())
        if confidence['version'] != VERSION or confidence['model_signature'] != row['model_signature']:
            continue
        # Older captures lack a frozen weights/prompt identity. They can have
        # evidence bands, but must not be pooled into a calibrated model rate.
        model = json.loads((folder/'latest.json').read_text())
        identity = model.get('analysis', {}).get('inference_profile', '')
        if len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            continue
        if model['model_signature'] != row['model_signature'] or profile(model) != confidence['profile']:
            continue
        result = evaluate_objects([o for o in row['prediction'] if o['kind'] == 'building'],
                                  [o for o in row['reference'] if o['kind'] == 'building'], threshold=.85)
        success = {m['prediction_id'] for m in result['matches'] if m['iou'] >= .85}
        for obj in confidence['objects']:
            if obj['band'] == 'unassessed':
                continue
            key = (confidence['profile'], obj['band'], row['spatial_group'])
            candidate = {'key': signature([row['scene_id'], obj['object_id']]),
                         'success': obj['object_id'] in success, 'scene_id': row['scene_id'], 'object_id': obj['object_id']}
            if key not in groups or candidate['key'] < groups[key]['key']:
                groups[key] = candidate  # Fixed hash sample: one roof per area and band, never chosen by outcome.
    strata = {}
    for (fingerprint, band, group), row in groups.items():
        entry = strata.setdefault(fingerprint+':'+band, {'profile': fingerprint, 'band': band, 'samples': []})
        entry['samples'].append({'spatial_group': group, **row})
    for entry in strata.values():
        count = len(entry['samples'])
        wins = sum(r['success'] for r in entry['samples'])
        entry.update(groups=count, successes=wins, interval=wilson(wins, count) if count >= MIN_CALIBRATION_GROUPS else None)
    report = {'version': VERSION, 'assessed_at': datetime.now(UTC).isoformat(), 'dataset_version': coverage['version'], 'eligible_validation_layers': len(eligible),
              'minimum_groups': MIN_CALIBRATION_GROUPS, 'confidence_level': .95, 'strata': strata,
              'target': 'Correct building with boundary IoU >= 0.85; not apex, height, recall or per-house accuracy.',
              'method': 'Wilson interval over one fixed-hash roof per validation spatial group and evidence band. Assumes representative independent areas.',
              'source': 'https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm'}
    atomic_json(root/'data/suburbs/mooroolbark/calibration.json', report)
    return report


HIDDEN_OVERLAP_KEYS = ('scene_id', 'object_id', 'band', 'detector_score', 'image_line_mismatch_m', 'observed_segments', 'clipped')


def dedupe_key(feature):
    """Display preference when captures overlap: unclipped, stronger band, then stable ids."""
    p = feature['properties']
    return (p['clipped'], {'high': 0, 'medium': 1, 'low': 2, 'unassessed': 3}[p['band']], p['scene_id'], p['object_id'])


def suppressed_overlaps(features, polygons):
    """Pair each hidden roof with the earlier-sorted kept roof from another scene it duplicates.

    features/polygons are index-aligned and already sorted by dedupe_key. Returns
    (kept_index, suppressed_index, iou) for every roof hidden by a same-footprint (IoU >= .65)
    capture; a roof is hidden by the first kept roof found, and every index not returned as
    suppressed is kept. This is the single source of truth for cross-capture pairing.
    """
    tree = STRtree(polygons)
    kept, pairs = set(), []
    for i, polygon in enumerate(polygons):
        duplicate = None
        for j in tree.query(polygon, predicate='intersects'):
            if j not in kept or features[j]['properties']['scene_id'] == features[i]['properties']['scene_id']:
                continue
            other = polygons[j]
            overlap = polygon.intersection(other).area
            iou = overlap/max(1e-20, polygon.area+other.area-overlap)
            if iou >= .65:
                duplicate = (int(j), i, iou)
                break
        if duplicate is None:
            kept.add(i)
        else:
            pairs.append(duplicate)
    return pairs


def query(bounds, root=Path('.'), band='all'):
    if (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
            or not -180 < bounds[0] < bounds[2] < 180 or not -85 < bounds[1] < bounds[3] < 85
            or bounds[2]-bounds[0] > .2 or bounds[3]-bounds[1] > .2 or band not in (*BANDS, 'all')):
        raise ValueError('Choose a suburb-sized view and a valid confidence band')
    with closing(connection(root)) as db:
        rows = db.execute('SELECT payload FROM roofs WHERE east>=? AND west<=? AND north>=? AND south<=? LIMIT 20001',
                          (bounds[0], bounds[2], bounds[1], bounds[3])).fetchall()
        stamps = dict(db.execute('SELECT id,mtime FROM scenes'))
    if len(rows) > 20000:
        raise ValueError('Zoom in to see confidence outlines')
    current = {sid for sid, stamp in stamps.items() if (root/'data/scenes'/sid/'latest.json').exists()
               and (root/'data/scenes'/sid/'latest.json').stat().st_mtime_ns == stamp}
    features = [json.loads(r[0]) for r in rows]
    features = [f for f in features if f['properties']['scene_id'] in current]
    features.sort(key=dedupe_key)
    polygons = [Polygon(f['geometry']['coordinates'][0], f['geometry']['coordinates'][1:]) for f in features]
    pairs = suppressed_overlaps(features, polygons)
    suppressed = {s for _, s, _ in pairs}
    for k, s, iou in pairs:
        # Additive diagnostic: the shown roof lists the same-footprint captures it hides. Not accuracy.
        hidden = features[s]['properties']
        features[k]['properties'].setdefault('hidden_overlaps', []).append(
            {**{key: hidden.get(key) for key in HIDDEN_OVERLAP_KEYS}, 'iou': round(iou, 4)})
    retained = [f for i, f in enumerate(features) if i not in suppressed]
    counts = {b: sum(f['properties']['band'] == b for f in retained) for b in BANDS}
    path = root/'data/suburbs/mooroolbark/calibration.json'
    calibration = json.loads(path.read_text()) if path.exists() else {'strata': {}, 'eligible_validation_layers': 0, 'minimum_groups': MIN_CALIBRATION_GROUPS}
    for feature in retained:
        p = feature['properties']
        stratum = calibration['strata'].get(p['profile']+':'+p['band'], {})
        p['interval'] = stratum.get('interval')
        p['calibration_groups'] = stratum.get('groups', 0)
    return {'type': 'FeatureCollection', 'features': [f for f in retained if band == 'all' or f['properties']['band'] == band],
            'counts': counts, 'duplicates': len(features)-len(retained), 'version': VERSION,
            'calibration': {k: v for k, v in calibration.items() if k != 'strata'}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scenes', nargs='+')
    p.add_argument('--calibrate', action='store_true')
    p.add_argument('--bounds', type=float, nargs=4)
    p.add_argument('--band', default='all')
    args = p.parse_args()
    if args.bounds:
        from .scene_suburb import progress
        print(json.dumps({**query(args.bounds, band=args.band), 'suburb': progress(Path('.'))}))
    elif args.calibrate:
        print(json.dumps(calibrate()))
    else:
        for sid in args.scenes or []:
            report = assess(sid)
            print(json.dumps({'scene': sid, 'roofs': len(report['objects'])}), flush=True)


if __name__ == '__main__':
    main()
