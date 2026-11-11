"""Per-roof fitter and image-edge diagnostics for saved scene buildings.

Re-runs the production roof fitter (`scene_roof.propose_roof`, called exactly as
`scene_house.add_roof_estimates` does) on the saved PNG, ring and affine, and keeps
the compact reports that persistence discards: geometry audit, line refinement,
topology cost before/after and the per-end hip/gable support the fitter used. Adds
outline edge support against Canny edges (original gray, CLAHE view separately),
per-vertex nearest-edge distance and crease contrast across stored roof lines.

These are diagnostics, not labels or accuracy. Nothing here is a human decision,
a calibrated interval or evidence for a different image; rows go only to the eyes
sidecar index (`roof_diagnostics`). No SAM3, no extraction lock, no production writes.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import io
import json
import math
import os
import sqlite3
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from shapely.errors import GEOSException

from . import eyes_index

VERSION = 'roof-diagnostics-v1'
TABLE = 'roof_diagnostics'
RISE_M = 2.
FORM = 'auto'
EDGE_TOLERANCE_PX = 1.5
UNSUPPORTED_BELOW = .3
CANNY = (50, 150)
BLUR = (3, 3)
CREASE_BAND_M = 1
CREASE_THRESHOLD_L = 6
FITTER_FILES = ('scene_roof.py', 'roof_line_fit.py', 'roof_topology.py', 'roof_form.py', 'roof_structure.py',
                'roof_physics.py', 'roof_junctions.py', 'roof_wings.py', 'roof_assembly.py', 'roof_surface.py',
                'roof_boundary.py', 'rooflines.py')
PROPOSE_ERRORS = (ValueError, ArithmeticError, RuntimeError, GEOSException, KeyError, TypeError)
IMAGE_ERRORS = (ValueError, ArithmeticError, TypeError, KeyError, IndexError, cv2.error)
COST_KEYS = ('score', 'image_line_mismatch_m', 'observed_segments', 'physical_planes', 'crease_runs', 'junctions',
             'short_junction_links', 'small_planes')
NOTE = 'Diagnostics, not labels or accuracy: fitter reports and image-edge support on the saved scene PNG only.'
CONFIDENCE_INDEX = Path('data/suburbs/mooroolbark/confidence.sqlite')
_CAPTURE = threading.Lock()


@functools.cache
def code_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@functools.cache
def fitter_sha256():
    """Combined hash of the fitter modules, so recomputation drift is attributable."""
    digest = hashlib.sha256()
    for name in FITTER_FILES:
        file = Path(__file__).with_name(name)
        digest.update(name.encode() + b'\0' + (hashlib.sha256(file.read_bytes()).hexdigest().encode() if file.exists() else b'missing'))
    return digest.hexdigest()


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def _round(value, digits=4):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


def _jsonable(value):
    """Plain JSON values: numpy scalars/arrays to Python, non-finite floats to None (browser-safe JSON)."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray | np.generic):
        return _jsonable(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _error(stage, error, *, unexpected=False):
    return {'stage': stage, 'error': f'{type(error).__name__}: {error}'[:500], **({'unexpected': True} if unexpected else {})}


def roof_form_model():
    """The learned roof-form model `roof_form.learned_choice` would consult (env var, else CWD-relative path)."""
    path = Path(os.environ.get('ROOF_FORM_MODEL', 'scripts/ml/models/roof-form.json'))
    if not path.exists():
        return {'path': str(path), 'active': False, 'sha256': None}
    from .roof_form import _trained_model
    try:
        active = _trained_model(str(path), path.stat().st_mtime_ns) is not None
    except (ValueError, OSError, KeyError, TypeError):
        active = False
    return {'path': str(path), 'active': active, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def _sample(array, xs, ys):
    """Nearest pixel-centre lookup (integer coordinates are pixel centres); NaN outside."""
    xs, ys = np.floor(np.asarray(xs, float) + .5), np.floor(np.asarray(ys, float) + .5)
    height, width = array.shape[:2]
    inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    values = np.full(len(xs), np.nan)
    values[inside] = array[ys[inside].astype(int), xs[inside].astype(int)]
    return values


def canny_edges(gray):
    """Canny 50/150 after a 3x3 Gaussian on an 8-bit single-channel view."""
    return cv2.Canny(cv2.GaussianBlur(np.ascontiguousarray(gray, np.uint8), BLUR, 0), *CANNY)


def edge_distance(edges):
    """Distance (px) from every pixel to the nearest edge pixel; inf when the view has no edges."""
    if not np.any(edges):
        return np.full(edges.shape, np.inf)
    return cv2.distanceTransform((edges == 0).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE).astype(float)


def _open_ring(ring):
    points = np.asarray(ring, float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
        raise ValueError('Outline ring must be at least two finite pixel points.')
    if len(points) > 2 and np.allclose(points[0], points[-1]):
        points = points[:-1]
    return points


def edge_support(ring, edges, *, distance=None):
    """Share of 1 px-spaced outline samples within 1.5 px of an edge, per outline edge."""
    points = _open_ring(ring)
    distance = edge_distance(edges) if distance is None else distance
    per_edge, lengths, outside = [], [], 0
    for a, b in zip(points, np.roll(points, -1, axis=0), strict=True):
        length = float(np.linalg.norm(b - a))
        t = np.linspace(0, 1, max(2, math.ceil(length) + 1))
        values = _sample(distance, a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1]))
        measured = values[~np.isnan(values)]
        outside += int(np.isnan(values).sum())
        per_edge.append(_round(np.mean(measured <= EDGE_TOLERANCE_PX)) if len(measured) else None)
        lengths.append(round(length, 3))
    vertex = _sample(distance, points[:, 0], points[:, 1])
    scored = [(s, w) for s, w in zip(per_edge, lengths, strict=True) if s is not None]
    total = sum(w for _, w in scored)
    return {'per_edge': per_edge, 'edge_length_px': lengths,
            'median': _round(np.median([s for s, _ in scored])) if scored else None,
            'weighted_mean': _round(sum(s*w for s, w in scored) / total) if total else None,
            'unsupported_fraction': _round(sum(w for s, w in scored if s < UNSUPPORTED_BELOW) / total) if total else None,
            'vertex_distance_px': [None if np.isnan(v) else _round(v, 3) for v in vertex],
            'samples_outside_image': outside}


def lab_lightness(image):
    """OpenCV 8-bit LAB L (0-255, = L* x 2.55), as roof_form uses for end evidence."""
    return cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2LAB)[:, :, 0].astype(float)


def crease_contrast(lines, meters_to_pixel, image):
    """|mean L| difference between 1 m bands either side of each stored roof line."""
    lightness = lab_lightness(image) if isinstance(image, Image.Image) else np.asarray(image, float)
    frame = np.asarray(meters_to_pixel, float)
    scale = math.sqrt(abs(np.linalg.det(frame[:2])))
    across = (np.arange(max(1, math.ceil(CREASE_BAND_M*scale))) + .5) / max(1, math.ceil(CREASE_BAND_M*scale)) * CREASE_BAND_M
    per_line = []
    for line in lines or []:
        try:
            per_line.append(_crease_line(line, frame, scale, across, lightness))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
            per_line.append({'kind': line.get('kind') if isinstance(line, dict) else None, 'delta_l': None,
                             'error': f'{type(error).__name__}: {error}'[:200]})
    measured = [e['delta_l'] for e in per_line if e['delta_l'] is not None]
    return {'per_line': per_line, 'n_lines': len(per_line), 'n_measured': len(measured),
            'n_errors': sum('error' in e for e in per_line), 'band_m': CREASE_BAND_M,
            'threshold_l': CREASE_THRESHOLD_L, 'l_scale': 'opencv-lab-8bit-0-255',
            'share_ge_threshold': _round(sum(d >= CREASE_THRESHOLD_L for d in measured) / len(measured)) if measured else None}


def _crease_line(line, frame, scale, across, lightness):
    a, b = (np.asarray(p, float) for p in line['line_meters'])
    if a.shape != (2,) or b.shape != (2,) or not (np.isfinite(a).all() and np.isfinite(b).all()):
        raise ValueError('Roof line ends must be two finite 2D local-metre points.')
    length = float(np.linalg.norm(b - a))
    entry = {'kind': line.get('kind'), 'length_m': round(length, 3), 'delta_l': None}
    if length * scale >= 1:
        tangent = (b - a) / length
        normal = np.array([-tangent[1], tangent[0]])
        t = np.linspace(0, 1, max(2, math.ceil(length*scale) + 1))
        centre = a + t[:, None]*(b - a)
        means = []
        for sign in (1, -1):
            band = (centre[:, None, :] + sign*across[None, :, None]*normal).reshape(-1, 2)
            pixels = np.c_[band, np.ones(len(band))] @ frame
            values = _sample(lightness, pixels[:, 0], pixels[:, 1])
            values = values[~np.isnan(values)]
            means.append(float(values.mean()) if len(values) >= 3 else None)
        if None not in means:
            entry.update(left_l=_round(means[0], 2), right_l=_round(means[1], 2), delta_l=_round(abs(means[0]-means[1]), 2))
    return entry


def image_views(image):
    """Per-image arrays reused across every roof in a scene (original gray, CLAHE, LAB L)."""
    from .roof_line_fit import enhance_roof_detail
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    views = {'size': image.size, 'lightness': lab_lightness(image)}
    for name, view in (('original', gray), ('clahe', enhance_roof_detail(image))):
        edges = canny_edges(view)
        views[name] = (edges, edge_distance(edges))
    return views


def _propose(obj, scene, image, trace, context_hint=None):
    """propose_roof exactly as add_roof_estimates calls it; `trace` keeps what it discards.

    propose_roof only returns end decisions inside an accepted topology report and drops its
    final audit when it raises, so the module-level `select_roof_ends` and `audit_roof` bindings
    in scene_roof are wrapped for the duration of this one call (lock-guarded, restored in
    finally) to record the decisions (`trace['ends']`) and the audit (`trace['audit']`) it used.
    Safe across processes; not for other threads calling propose_roof concurrently.
    """
    from . import scene_roof
    frame = np.asarray(scene['pixel_to_meters'])

    def local(ring):
        return (np.c_[ring, np.ones(len(ring))] @ frame).tolist()

    def recording(function, key, pick):
        def wrapper(*args, **kwargs):
            result = function(*args, **kwargs)
            trace[key] = pick(result)
            return result
        return wrapper
    with _CAPTURE:
        ends, audit = scene_roof.select_roof_ends, scene_roof.audit_roof
        scene_roof.select_roof_ends = recording(ends, 'ends', lambda result: result[1])
        scene_roof.audit_roof = recording(audit, 'audit', lambda result: result)
        try:
            return scene_roof.propose_roof(local(obj['ring']), obj['model']['height_m'], RISE_M, FORM, image=image,
                                           meters_to_pixel=scene['meters_to_pixel'], holes=[local(h) for h in obj.get('holes', [])],
                                           **({} if context_hint is None else {'context_hint': context_hint}))
        finally:
            scene_roof.select_roof_ends, scene_roof.audit_roof = ends, audit


def _compact_audit(audit):
    if not isinstance(audit, dict):
        return None
    drainage = audit.get('drainage') or {}
    return {'passed': audit.get('passed'), 'issues': audit.get('issues'), 'coverage': audit.get('coverage'),
            'max_planarity_error_m': audit.get('max_planarity_error_m'), 'max_seam_error_m': audit.get('max_seam_error_m'),
            'drainage': {'status': drainage.get('status'), 'passed': drainage.get('passed'), 'sample_count': drainage.get('sample_count')}}


def _compact_lines(report):
    if report is None:
        return None
    return {'accepted': report.get('accepted'), 'reason': report.get('reason'), 'detected_segments': report.get('detected_segments'),
            'matched_edges': report.get('matched_edges'), 'weighted_residual_before': report.get('weighted_residual_before_m'),
            'weighted_residual_after': report.get('weighted_residual_after_m'), 'max_shift_m': report.get('max_shift_m'),
            'conflicting_matches_removed': report.get('conflicting_matches_removed')}


def _compact_topology(report):
    if report is None:
        return None
    return {'accepted': report.get('accepted'), 'reason': report.get('reason'), 'candidates': len(report.get('candidates') or []),
            **{key: {k: (report.get(key) or {}).get(k) for k in COST_KEYS} for key in ('before', 'after')}}


def _compact_ends(decisions):
    if decisions is None:
        return None
    return [{'hip_support': d.get('hip_support'), 'gable_support': d.get('gable_support'), 'image_choice': d.get('image_choice'),
             'reason': str(d.get('reason'))[:200] if d.get('reason') is not None else None, 'selected': d.get('selected')}
            for d in decisions.get('ends', [])]


def _same_json(a, b):
    return json.dumps(_jsonable(a), sort_keys=True) == json.dumps(_jsonable(b), sort_keys=True)


def diagnose_object(obj, scene, image, *, views=None, context_hint=None):
    """Compact diagnostics payload for one building; failures become `error`/`stage` fields.

    context_hint (default None: image-only call, unchanged) is forwarded to propose_roof exactly as the pipeline
    does for hinted roofs; the call block and form_evidence record it.

    Expected fitter refusals (the add_roof_estimates exception set plus KeyError/TypeError) and,
    flagged `unexpected`, any other exception are recorded per stage so one bad roof never
    aborts a background run. The payload is plain JSON (no numpy scalars, no NaN/Infinity).
    """
    started = time.perf_counter()
    stored = obj.get('roof_annotation')
    payload = {'version': VERSION, 'code_sha256': code_sha256(), 'fitter_code_sha256': fitter_sha256(), 'note': NOTE,
               'object_id': obj.get('id'), 'image_sha256': scene.get('image_sha256'),
               'call': {'form': FORM, 'rise_m': RISE_M, 'eave_m': (obj.get('model') or {}).get('height_m'),
                        'roof_form_model': roof_form_model(), 'context_hint': context_hint},
               'roof_type_stored': obj.get('roof_type'), 'roof_geometry_source_stored': obj.get('roof_geometry_source'),
               'stored_annotation_present': bool(stored), 'roof_line_edits_present': bool(obj.get('roof_line_edits'))}
    errors, trace = [], {}
    try:
        proposal = _propose(obj, scene, image, trace, context_hint)
        form = proposal.get('form_evidence') or {}
        topology = proposal['topology']
        accepted = bool(topology and topology.get('accepted'))
        auto = trace.get('ends')
        payload.update(audit=_compact_audit(proposal['audit']), line_refinement=_compact_lines(proposal['line_refinement']),
                       topology=_compact_topology(topology),
                       ends=_compact_ends(topology.get('end_decisions') if accepted else auto),
                       ends_source='topology' if accepted else 'auto-captured' if auto is not None else None,
                       form_uncertain=bool(proposal['form_uncertain']), roof_type_recomputed=proposal['roof_type'],
                       form_evidence={'version': form.get('version'), 'decision': form.get('decision'), 'score': form.get('score'), 'rule': form.get('rule'),
                                      'context': form.get('context'), 'reasons': form.get('reasons')} if form else None,
                       recomputed_matches_stored=_same_json(proposal['annotation'], stored) if stored else None)
    except PROPOSE_ERRORS as error:
        errors.append(_error('propose_roof', error))
    except Exception as error:  # a fitter bug must become a row, not abort the run
        errors.append(_error('propose_roof', error, unexpected=True))
    if errors and trace.get('audit') is not None:
        payload['audit'] = _compact_audit(trace['audit'])   # the audit propose_roof computed before refusing
    try:
        views = views or image_views(image)
        payload['edge_support'] = {'tolerance_px': EDGE_TOLERANCE_PX, 'unsupported_threshold': UNSUPPORTED_BELOW,
                                   'canny': list(CANNY), 'blur': list(BLUR),
                                   **{name: edge_support(obj['ring'], views[name][0], distance=views[name][1])
                                      for name in ('original', 'clahe')}}
    except IMAGE_ERRORS as error:
        errors.append(_error('edge_support', error))
    except Exception as error:  # recorded as a row, never aborts the run
        errors.append(_error('edge_support', error, unexpected=True))
    try:
        payload['crease_contrast'] = crease_contrast(obj.get('roof_lines'), scene['meters_to_pixel'],
                                                     views['lightness'] if views else image)
    except IMAGE_ERRORS as error:
        errors.append(_error('crease_contrast', error))
    except Exception as error:  # recorded as a row, never aborts the run
        errors.append(_error('crease_contrast', error, unexpected=True))
    if errors:
        payload.update(error=errors[0]['error'], stage=errors[0]['stage'], errors=errors)
    payload.update(elapsed_seconds=round(time.perf_counter() - started, 4), computed_at=_now())
    return _jsonable(payload)


def load_scene(scene_id, root=Path('.')):
    """Read-only load of latest.json and its sha-verified PNG (the sole evidence gate)."""
    from .scene_self_review import validate_id
    folder = Path(root)/'data/scenes'/validate_id(scene_id)
    scene = json.loads((folder/'latest.json').read_bytes())
    digest = scene.get('image_sha256', '')
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid imagery identity')
    source = (folder/f'{digest}.png').read_bytes()
    if hashlib.sha256(source).hexdigest() != digest:
        raise ValueError('Source imagery changed')
    with Image.open(io.BytesIO(source)) as opened:          # decode exactly the verified bytes
        image = opened.convert('RGB')
    if image.size != (scene['image_width'], scene['image_height']):
        raise ValueError('Imagery dimensions changed')
    return scene, image


def building_objects(scene, only_owned=True):
    """Buildings in a scene; owned defaults to every object when the scene has no owned list (as assess does)."""
    owned = set(scene.get('owned_object_ids', [o['id'] for o in scene['objects']]))
    return [o for o in scene['objects'] if o.get('kind') == 'building' and (not only_owned or o['id'] in owned)]


def review_states(scene_id, root=Path('.'), model_signature=None):
    """Context only (never labels here): review.json status per object id, read-only; {} when absent."""
    file = Path(root)/'data/scenes'/scene_id/'review.json'
    try:
        review = json.loads(file.read_bytes())
        same = review.get('model_signature') == model_signature
        return {o['id']: {'status': o.get('status'), 'geometry_reviewed': bool(o.get('geometry_reviewed')),
                          'roof_line_edits_present': bool(o.get('roof_line_edits')), 'same_model_signature': same}
                for o in review.get('objects', []) if isinstance(o, dict) and 'id' in o}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _scene_rows(task):
    root, scene_id, only_owned = task
    try:
        scene, image = load_scene(scene_id, Path(root))
        signature = scene['model_signature']
        owned = set(scene.get('owned_object_ids', [o['id'] for o in scene['objects']]))
        buildings = building_objects(scene, only_owned=False)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return scene_id, [], 0, f'{type(error).__name__}: {error}'[:500]
    views = image_views(image)
    review = review_states(scene_id, root, signature)
    rows = []
    for obj in buildings:
        if only_owned and obj['id'] not in owned:
            continue
        payload = diagnose_object(obj, scene, image, views=views)
        payload.update(owned=obj['id'] in owned, review=review.get(obj['id']))
        rows.append({'scene_id': scene_id, 'object_id': obj['id'], 'model_signature': signature, 'version': VERSION,
                     'payload': payload})
    return scene_id, rows, len(buildings), None


def run(root=Path('.'), scene_ids=(), *, only_owned=True, workers=1):
    """Diagnose scenes and write `roof_diagnostics` rows; returns coverage counts with denominators."""
    root = Path(root)
    tasks = [(str(root), scene_id, only_owned) for scene_id in dict.fromkeys(scene_ids)]
    coverage = {'version': VERSION, 'only_owned': only_owned, 'scenes_requested': len(tasks), 'scenes_failed': 0,
                'buildings_total': 0, 'attempted': 0, 'succeeded': 0, 'failed': 0,
                'recomputed_matches_stored': 0, 'recomputed_compared': 0, 'scenes': {}}
    timings = []
    db = None
    pool = None
    try:
        if workers > 1 and len(tasks) > 1:
            import multiprocessing
            pool = multiprocessing.get_context('spawn').Pool(min(workers, len(tasks)))
            results = pool.imap(_scene_rows, tasks)
        else:
            results = map(_scene_rows, tasks)
        for scene_id, rows, buildings, error in results:
            failed = sum('error' in r['payload'] for r in rows)
            summary = {'buildings_total': buildings, 'attempted': len(rows), 'succeeded': len(rows) - failed, 'failed': failed}
            if error:
                summary['error'] = error
                coverage['scenes_failed'] += 1
            coverage['scenes'][scene_id] = summary
            for key in ('buildings_total', 'attempted', 'succeeded', 'failed'):
                coverage[key] += summary[key]
            for row in rows:
                payload = row['payload']
                timings.append(payload['elapsed_seconds'])
                coverage['recomputed_compared'] += payload.get('recomputed_matches_stored') is not None
                coverage['recomputed_matches_stored'] += payload.get('recomputed_matches_stored') is True
            if rows:
                if db is None:
                    db = eyes_index.connection(root)
                    eyes_index.ensure_schema(db)
                eyes_index.replace_rows(db, TABLE, rows)
    finally:
        if pool is not None:
            pool.close()
            pool.join()
        if db is not None:
            db.close()
    coverage['seconds_per_roof'] = ({'min': round(min(timings), 4), 'median': round(float(np.median(timings)), 4),
                                     'max': round(max(timings), 4)} if timings else None)
    return coverage


def _index_profiles(root=Path('.')):
    """{scene_id: {profile: indexed roofs}} from the suburb confidence index (opened read-only)."""
    file = Path(root)/CONFIDENCE_INDEX
    with closing(sqlite3.connect(f'file:{file}?mode=ro', uri=True)) as db:
        found = {r[0]: {} for r in db.execute('SELECT id FROM scenes')}
        for scene_id, payload in db.execute('SELECT scene_id, payload FROM roofs'):
            profile = str((json.loads(payload).get('properties') or {}).get('profile'))
            counts = found.setdefault(scene_id, {})
            counts[profile] = counts.get(profile, 0) + 1
    return found


def owned_scene_ids(root=Path('.'), profile=None):
    """Scenes in the suburb confidence index (read-only); `profile` keeps scenes with roofs under that
    evidence-profile prefix, so the scan population (e.g. 10,072 roofs under 53ea49d5...) is not mixed
    with house captures indexed under other profiles."""
    profiles = _index_profiles(root)
    return sorted(s for s, counts in profiles.items() if profile is None or any(p.startswith(profile) for p in counts))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Roof fitter and image-edge diagnostics (diagnostics, not labels or accuracy).')
    parser.add_argument('--root', default='.')
    parser.add_argument('--scenes', nargs='+', default=[])
    parser.add_argument('--all-owned', action='store_true', help='Every scene in the suburb confidence index, owned roofs only.')
    parser.add_argument('--profile', help='With --all-owned: only scenes whose indexed roofs use this evidence-profile prefix.')
    parser.add_argument('--include-unowned', action='store_true', help='Diagnose every building, not only owned objects.')
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args(argv)
    root = Path(args.root)
    indexed = _index_profiles(root) if args.all_owned else {}
    selected = owned_scene_ids(root, args.profile) if args.all_owned else []
    scene_ids = [*args.scenes, *selected]
    if not scene_ids:
        parser.error('Pass --scenes or --all-owned.')
    report = run(root, scene_ids, only_owned=not args.include_unowned, workers=args.workers)
    by_profile = {}
    for scene_id in selected:
        for profile, count in indexed[scene_id].items():
            if args.profile is None or profile.startswith(args.profile):
                by_profile[profile] = by_profile.get(profile, 0) + count
    report['population'] = {'selection': 'confidence-index' if args.all_owned else 'scenes', 'profile': args.profile,
                            'scenes': len(dict.fromkeys(scene_ids)), 'only_owned': not args.include_unowned,
                            **({'indexed_roofs_by_profile': by_profile} if args.all_owned else {})}
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
