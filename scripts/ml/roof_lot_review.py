"""Parcel-conditioned SAM roof searches, separate from physical roof topology.

Numbered cadastral lots define allocation/search regions, not roof masks. Run
SAM on unpainted, padded RGB crops and intersect its roof evidence with each
lot. Never turn a yard, shared drive or parcel cut into a roof/fold. Results
remain review candidates; parcel registration and roof heights are unresolved.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from pathlib import Path

import cv2
import numpy as np
from shapely.affinity import translate
from shapely.errors import GEOSException
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .roof_block_context import polygons, rings
from .roof_boundary import regularize_eaves
from .scene_batch import signature
from .scene_training import atomic_json

VERSION = 'parcel-roof-search-v1'
POLICY_VERSION = 'parcel-roof-allocation-v2'
POLICY = {'max_area_change_fraction': .08, 'max_boundary_shift_m': .5,
          'max_image_residual_px': 1.5, 'max_image_residual_increase_px': .15,
          'max_position_uncertainty_m': 5., 'max_lots_per_scene': 12}
CACHE = Path('data/roof-review-loop/lot-segmentation')


def geometry(value):
    try:
        if not isinstance(value, (list, tuple)) or len(value) > 64:
            raise ValueError('Expected bounded polygon coordinates.')
        parts = []
        for polygon in value:
            if not polygon or sum(len(ring) for ring in polygon) > 12000:
                raise ValueError('Invalid polygon size.')
            for ring in polygon:
                xy = np.asarray(ring, dtype=float)
                if xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 3 or not np.isfinite(xy).all():
                    raise ValueError('Invalid polygon coordinates.')
            part = Polygon(polygon[0], polygon[1:])
            if not part.is_valid or part.is_empty or part.area <= 0:
                raise ValueError('Invalid polygon topology.')
            parts.append(part)
        return unary_union(parts)
    except (TypeError, IndexError) as error:
        raise ValueError('Invalid polygon coordinates.') from error


def pixel_scale(scene):
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    if frame.shape != (3, 2) or not np.isfinite(frame).all():
        raise ValueError('Invalid image registration.')
    scales = np.linalg.svd(frame[:2], compute_uv=False)
    if scales.min() < .005 or scales.max() > 5 or scales.max()/scales.min() > 1.01:
        raise ValueError('Parcel inference requires a registered square-pixel image.')
    return float(np.sqrt(abs(np.linalg.det(frame[:2]))))


def lines(value):
    if value.is_empty:
        return []
    if value.geom_type in ('LineString', 'LinearRing'):
        return [list(map(list, value.coords))]
    return [line for part in getattr(value, 'geoms', []) for line in lines(part)]


def searches(scene, block):
    """Exclusive lot cores with uncertainty retained, not silently buffered away."""
    numbered = [lot for lot in block.get('lots', []) if re.fullmatch(r'\d+[A-Za-z]?', str(lot.get('lot', '')))]
    try:
        mpp = pixel_scale(scene)
        sizes = [scene['image_width'], scene['image_height']]
        if any(isinstance(n, bool) or not isinstance(n, int) or not 2 <= n <= 16384 for n in sizes):
            raise ValueError('Invalid image dimensions.')
        image = box(0, 0, *sizes)
        ids = [lot.get('pfi') for lot in block.get('lots', [])]
        if any(not isinstance(pfi, str) or not pfi for pfi in ids) or len(ids) != len(set(ids)):
            raise ValueError('Missing or duplicate parcel identities.')
        shapes = {lot['pfi']: geometry(lot['polygons']) for lot in block.get('lots', [])}
        if any(poly.is_empty for poly in shapes.values()):
            raise ValueError('Empty cadastral geometry.')
        common = unary_union([shapes[lot['pfi']] for lot in block.get('lots', []) if lot not in numbered])
    except (KeyError, TypeError, ValueError, GEOSException) as error:
        return [{'pfi': str(lot.get('pfi', '')), 'lot': str(lot['lot']), 'polygons': [], 'core': [], 'crop': None,
                 'horizontal_uncertainty_m': None, 'status': 'needs_context', 'reason': str(error)} for lot in numbered]
    results = []
    for lot in sorted(numbered, key=lambda lot: (int(re.match(r'\d+', str(lot['lot']))[0]), str(lot['lot']))):
        poly = shapes[lot['pfi']]
        others = unary_union([shapes[other['pfi']] for other in numbered if other['pfi'] != lot['pfi']])
        overlap = poly.intersection(others.union(common))
        core = poly.difference(overlap)
        uncertainty = lot.get('horizontal_uncertainty_m')
        valid_uncertainty = (not isinstance(uncertainty, bool) and isinstance(uncertainty, (int, float))
                             and math.isfinite(uncertainty) and 0 <= uncertainty <= POLICY['max_position_uncertainty_m'])
        reason = None
        if not poly.is_valid or core.is_empty or core.area*mpp*mpp < 8:
            reason = 'Invalid, overlapping or too small a lot search region.'
        elif not valid_uncertainty:
            reason = 'Parcel position uncertainty is missing or exceeds the bounded search.'
        elif not image.buffer(-1).covers(poly):
            reason = 'The lot is clipped by this image; obtain a fuller capture.'
        elif overlap.area > poly.area*.02:
            reason = 'Overlapping cadastral lots require review before unit allocation.'
        # Square crops preserve aspect ratio through the SAM image encoder.
        padding = max(4., float(uncertainty if valid_uncertainty else 0)+2.)/mpp
        w, s, e, n = poly.bounds
        side = math.ceil(max(e-w, n-s)+2*padding)
        cx, cy = (w+e)/2, (s+n)/2
        left, top = math.floor(cx-side/2), math.floor(cy-side/2)
        crop = [left, top, left+side, top+side]
        if not image.covers(box(*crop)):
            reason = reason or 'The padded lot search crosses the image edge; obtain more context.'
        results.append({**lot, 'horizontal_uncertainty_m': uncertainty if valid_uncertainty else None,
                        'core': rings(core), 'crop': crop, 'status': 'needs_context' if reason else 'ready',
                        'reason': reason, 'overlap_excluded_m2': round(overlap.area*mpp*mpp, 3)})
    return results


def cache_key(scene, block):
    return signature([VERSION, scene['id'], scene['image_sha256'], scene['model_signature'],
                      block.get('lots', []), scene['pixel_to_meters']])


def detect_lot(image, search, segmenter):
    """No synthetic image masking: cadastral edges cannot create image features."""
    from .segmentation import mask_to_polygons
    if search.get('status', 'ready') != 'ready':
        raise ValueError('This lot needs more evidence before inference.')
    left, top, right, bottom = search['crop']
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height and right-left == bottom-top):
        raise ValueError('A complete square crop is required; no padding or distorted pixels.')
    crop = image.crop((left, top, right, bottom))
    features = segmenter.encode_image(crop)
    result = []
    for proposal in segmenter.proposals(features, 'roof', crop.size, threshold=.35, max_proposals=16):
        parts = [translate(poly, xoff=left, yoff=top) for poly in
                 mask_to_polygons(proposal.mask, min_area_px=16, simplify_px=.5)]
        result.append({'score': float(proposal.score), 'polygons': rings(unary_union(parts))})
    return result


def allocate_lot(scene, image, search, detections, context=()):
    """Constrain segmentation to the lot; retain unresolved eaves separately."""
    if search.get('status') != 'ready' or image.size != (scene['image_width'], scene['image_height']):
        raise ValueError('Lot search or image registration is not ready.')
    core = geometry(search['core'])
    mpp = pixel_scale(scene)
    tolerance = float(search['horizontal_uncertainty_m'])/mpp
    supported, scores = [], []
    for detection in detections:
        score = detection['score']
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('Invalid detector score.')
        poly = geometry(detection['polygons'])
        if poly.is_empty:
            continue
        if not box(*search['crop']).buffer(.01).covers(poly):
            raise ValueError('Detector coordinates do not belong to the registered crop.')
        if poly.is_empty or not poly.is_valid or detection['score'] < .35:
            continue
        overlap = poly.intersection(core)
        # Reject tiny fixture masks and remote roof detections in padded context.
        if overlap.area*mpp*mpp >= 8 and overlap.area >= core.area*.08:
            supported.append(poly)
            scores.append(detection['score'])
    raw = unary_union(supported).intersection(core)
    if raw.is_empty:
        return {'status': 'no_roof_evidence', 'polygons': [], 'reason': 'SAM found no supported roof region in this lot; the parcel is not used as a fallback roof.'}
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    distances = cv2.distanceTransform(255-cv2.Canny(gray, 50, 120), cv2.DIST_L2, 3)
    def residual(poly):
        # Cadastral cuts are allocation edges, not expected visual eaves.
        edge = poly.boundary.difference(core.boundary.buffer(.1))
        if edge.length < 1:
            return None
        pts = np.array([edge.interpolate(t, normalized=True).coords[0] for t in np.linspace(0, 1, 300)])
        pts = np.clip(pts.astype(int), (0, 0), (image.width-1, image.height-1))
        return float(np.minimum(distances[pts[:, 1], pts[:, 0]], 10).mean())
    output, fits = [], []
    for part in polygons(raw):
        fitted, fit = regularize_eaves(part, image.size, scene['pixel_to_meters'], hypothesis=True)
        fitted = fitted.intersection(core)
        before, after = residual(part), residual(fitted)
        if fit['accepted'] and (before is None or after is None or after > min(POLICY['max_image_residual_px'], before+POLICY['max_image_residual_increase_px'])):
            fit.update(accepted=False, reason='Straight-edge proposal lacks original-image support')
        fit.update(original_image_residual_px=before, fitted_image_residual_px=after)
        fits.append(fit)
        output.append(fitted if fit['accepted'] else part)
    crop_candidate = unary_union(output)
    allocated = crop_candidate
    strategy, comparison = 'lot-crop', None
    baseline = unary_union(context).intersection(core)
    # A crop can erase a covered connector or a lower roof level. It does not
    # independently prove the wider detector was wrong. Keep its image-checked
    # eaves when the close crop loses substantial coverage or adds fragments.
    # This is a no-regression fallback, not a claim of new geometric accuracy.
    if not baseline.is_empty:
        lost = baseline.difference(crop_candidate).area/baseline.area
        added = crop_candidate.difference(baseline).area/baseline.area
        def vertices(shape):
            return sum(len(p.exterior.coords)-1 for p in polygons(shape))
        fragmented = (len(polygons(crop_candidate)) != len(polygons(baseline))
                      or vertices(crop_candidate) > max(8, vertices(baseline)*1.5))
        holes_before = unary_union([Polygon(h) for p in polygons(baseline) for h in p.interiors])
        holes_after = unary_union([Polygon(h) for p in polygons(crop_candidate) for h in p.interiors])
        holes_changed = holes_before.symmetric_difference(holes_after).area*mpp*mpp > .01
        shift = baseline.boundary.hausdorff_distance(crop_candidate.boundary)*mpp
        before_residual, after_residual = residual(baseline), residual(crop_candidate)
        worse_edges = (before_residual is None or after_residual is None
                       or after_residual > min(POLICY['max_image_residual_px'], before_residual+POLICY['max_image_residual_increase_px']))
        comparison = {'lost_context_fraction': round(lost, 4), 'added_fraction': round(added, 4),
                      'adds_fragments_or_corners': fragmented, 'courtyard_change': holes_changed,
                      'boundary_shift_m': round(shift, 4), 'worse_image_edges': worse_edges}
        if (lost > POLICY['max_area_change_fraction'] or added > POLICY['max_area_change_fraction'] or fragmented
                or holes_changed or shift > POLICY['max_boundary_shift_m'] or worse_edges):
            allocated, strategy = baseline, 'context-retained'
    # Outer eaves can cross an uncertain cadastral registration. Retain these
    # observations, but never assign the same area to two numbered lots.
    outside_evidence = unary_union(context) if strategy == 'context-retained' else unary_union(supported)
    uncertain = outside_evidence.intersection(core.buffer(tolerance)).difference(core)
    crop_edge = box(*search['crop']).boundary
    unresolved_crop = allocated.boundary.intersection(crop_edge.buffer(.6))
    return {'status': 'needs_review', 'polygons': rings(allocated), 'raw_polygons': rings(raw),
            'crop_polygons': rings(crop_candidate), 'boundary_strategy': strategy, 'scale_comparison': comparison,
            'roof_edges': lines(allocated.boundary.difference(core.boundary.buffer(.1)).difference(crop_edge.buffer(.6))),
            'allocation_edges': lines(allocated.boundary.intersection(core.boundary.buffer(.1))),
            'uncertain_polygons': rings(uncertain), 'crop_cut_edges': lines(unresolved_crop),
            'area_m2': round(allocated.area*mpp*mpp, 2),
            'lot_coverage_fraction': round(allocated.area/core.area, 4),
            'segmentation_scores': scores, 'boundary_fits': fits,
            'reason': 'Image-supported roof area allocated to this lot. Dashed cuts are cadastral divisions, not inferred hips, valleys or measured party walls.',
            'roof_family': 'unresolved', 'height_verified': False, 'human_approved': False, 'eligible': False}


def lot_review(root, scene, block, image, directory=None, *, observations=None):
    from .roof_family_review import straight_flat_outline

    planned = searches(scene, block)
    result = {'version': POLICY_VERSION, 'policy': dict(POLICY), 'status': 'not_run', 'lots': [], 'default_scope': 'lot',
              'reason': 'Each numbered lot is a separate roof search. Yard and common-property pixels are not roof labels.',
              'source_model_unchanged': True, 'roof_topology_changed': False}
    if not planned:
        return {**result, 'status': 'unavailable', 'reason': 'No unambiguous numbered cadastral lots are available.'}
    path = (directory or root/CACHE)/f'{cache_key(scene, block)}.json'
    saved = observations if observations is not None else json.loads(path.read_text()) if path.exists() else None
    if saved and saved.get('key') != cache_key(scene, block):
        raise ValueError('Lot segmentation belongs to a different image, model or parcel snapshot.')
    if saved:
        for key in ('scene_id', 'image_sha256', 'model_signature'):
            if saved.get(key) != scene['id' if key == 'scene_id' else key]:
                raise ValueError('Lot observation source identity changed.')
        if not saved.get('detector_weights') or not isinstance(saved.get('lots'), list):
            raise ValueError('Lot observations lack detector provenance.')
        ids = [row.get('pfi') for row in saved['lots']]
        if len(ids) != len(set(ids)) or any(pfi not in {s['pfi'] for s in planned} for pfi in ids):
            raise ValueError('Duplicate or unknown lot observations.')
    context, context_fits = [], []
    for member in block.get('members', []):
        obj = next(o for o in scene['objects'] if o['id'] == member['object_id'])
        if member.get('protected') or obj.get('geometry_reviewed') or obj.get('roof_line_edits') or obj.get('roof_feedback'):
            continue
        fitted, fit = straight_flat_outline(obj, scene, image)
        context.append(Polygon(fitted['ring'], fitted.get('holes', [])))
        context_fits.append(fit)
    for search in planned:
        item = {**search, 'polygons': [], 'parcel_polygons': search['polygons']}
        core = geometry(search['core'])
        item['parent_object_ids'] = [member['object_id'] for member in block.get('members', [])
                                     if core.intersection(Polygon(next(o for o in scene['objects'] if o['id'] == member['object_id'])['ring'])).area > core.area*.08]
        observation = next((r for r in saved['lots'] if r['pfi'] == search['pfi']), None) if saved else None
        if search['status'] != 'ready':
            pass
        elif observation is None:
            item.update(status='not_run', reason='Run parcel-conditioned SAM extraction for this lot.')
        elif observation.get('error'):
            item.update(status='failed', reason=observation['error'])
        else:
            try:
                if observation.get('crop') != search['crop']:
                    raise ValueError('Cached crop registration changed.')
                item.update(allocate_lot(scene, image, search, observation['detections'], context))
            except (KeyError, ValueError, TypeError, GEOSException) as error:
                item.update(status='failed', reason=str(error))
            item['context_boundary_fits'] = context_fits
            if item.get('boundary_strategy') == 'context-retained':
                item['reason'] = 'The close SAM crop lost roof coverage or added fragments. Retained the wider-image outline, allocated to this lot; compare the crop before changing it. Parcel cuts are not roof folds.'
        result['lots'].append(item)
    result['status'] = ('partial' if any(item['status'] in ('not_run', 'needs_context', 'failed') for item in result['lots']) else 'complete') if saved else 'not_run'
    result['detector_weights'] = saved.get('detector_weights') if saved else None
    return result


def main():
    import argparse
    import time

    from .roof_block_context import lot_context, parcel_snapshot
    from .roof_review_loop import check_input, protected_reason, run_path
    from .sam3 import Sam3TextSegmenter

    parser = argparse.ArgumentParser(description='Cache bounded parcel-conditioned SAM searches without publishing labels.')
    parser.add_argument('--run', required=True)
    parser.add_argument('--objects', type=int, nargs='+', required=True)
    args = parser.parse_args()
    root = Path.cwd()
    directory = run_path(root, args.run)
    report = json.loads((directory/'report.json').read_text())
    if not 1 <= len(args.objects) <= 4 or any(i < 0 or i >= len(report['rows']) for i in args.objects):
        parser.error('Choose 1–4 existing roof indices.')
    models = root/'scripts/ml/models/sam3'
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in models.iterdir() if p.suffix in ('.onnx', '.json')}
    segmenter = None
    # Concurrent UI/CLI starts must not consume or overwrite a partial search.
    (root/CACHE).mkdir(parents=True, exist_ok=True)
    lock = (root/CACHE/'extraction.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('Another parcel extraction is running; its checkpoint is unchanged.')
    for index in args.objects:
        scene, obj, image = check_input(root, directory, index, report['rows'][index])
        block = lot_context(scene, obj, parcel_snapshot(directory, index, scene)['data'])
        planned = searches(scene, block)
        if not 1 <= len(planned) <= 12:
            parser.error('A bounded pilot needs 1–12 unambiguous numbered lots per site.')
        if any(protected_reason(root, scene, next(o for o in scene['objects'] if o['id'] == m['object_id'])) for m in block['members']):
            parser.error('A roof in this cadastral context has protected feedback.')
        key = cache_key(scene, block)
        path = root/CACHE/f'{key}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            saved = json.loads(path.read_text())
            if saved.get('detector_weights') != hashes:
                raise ValueError('Cached lot observations use different weights; generate a new source model before comparison.')
            validate_checkpoint(saved, key, hashes, planned)
            lot_review(root, scene, block, image)
            print(json.dumps({'index': index, 'status': 'cached', 'artifact': str(path)}), flush=True)
            continue
        if segmenter is None:
            segmenter = Sam3TextSegmenter(models)
        checkpoint = path.with_suffix('.partial')
        prior = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
        if prior:
            validate_checkpoint(prior, key, hashes, planned)
        observations = prior['lots'] if prior else []
        for search in planned:
            if any(item['pfi'] == search['pfi'] for item in observations):
                continue
            started = time.monotonic()
            item = {'pfi': search['pfi'], 'lot': search['lot'], 'crop': search['crop']}
            if search['status'] == 'ready':
                try:
                    item['detections'] = detect_lot(image, search, segmenter)
                except (RuntimeError, ValueError) as error:
                    item['error'] = str(error)
            else:
                item['error'] = search['reason']
            observations.append(item)
            atomic_json(checkpoint, {'key': key, 'detector_weights': hashes, 'lots': observations})
            print(json.dumps({'index': index, 'lot': search['lot'], 'proposals': len(item.get('detections', [])),
                              'error': item.get('error'), 'seconds': round(time.monotonic()-started, 1)}), flush=True)
        # Revalidate saved feedback and the source after expensive inference.
        check_input(root, directory, index, report['rows'][index])
        atomic_json(path, {'version': VERSION, 'key': key, 'scene_id': scene['id'], 'image_sha256': scene['image_sha256'],
                          'model_signature': scene['model_signature'], 'detector_weights': hashes,
                          'lots': observations, 'human_approved': False})
        print(json.dumps({'index': index, 'artifact': str(path)}), flush=True)


def validate_checkpoint(saved, key, hashes, planned):
    if saved.get('key') != key or saved.get('detector_weights') != hashes or not isinstance(saved.get('lots'), list):
        raise ValueError('Lot extraction has different source inputs or weights.')
    expected = {search['pfi']: search for search in planned}
    seen = set()
    for row in saved['lots']:
        pfi = row.get('pfi')
        if pfi in seen or pfi not in expected or row.get('crop') != expected[pfi]['crop']:
            raise ValueError('Duplicate, unknown or misregistered checkpoint lot.')
        if not row.get('error') and not isinstance(row.get('detections'), list):
            raise ValueError('Checkpoint lot has no detector outcome.')
        seen.add(pfi)


if __name__ == '__main__':
    main()
