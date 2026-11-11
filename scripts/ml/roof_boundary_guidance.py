"""Original-RGB boundary comparisons, separate from production mask cleanup.

Finite eave fits and signed profiles expose shadow/canopy uncertainty. An
enhanced image may nominate a line location; every measurement below uses the
saved RGB. Neither brightness edges nor generated detail establish hidden eaves.
"""
from __future__ import annotations

import cv2
import numpy as np
from shapely import intersects_xy
from shapely import points as shapely_points
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from .roof_boundary import model_canopies
from .roof_surface import _fitted_eaves, refine_roof_surface

VERSION = 'rgb-eave-comparison-v1'
LIMITS = {'visible_displacement_m': 2., 'visible_displacement_source_pixels': 8.,
          'hidden_displacement_m': 2., 'area_change_fraction': .20,
          'roof_material_chroma': 14., 'roof_material_lightness': 35.,
          'minimum_step_rgb': 18., 'minimum_anchor_share': .60}


def _green(rgb):
    return (rgb[..., 1] > rgb[..., 0]*1.08) & (rgb[..., 1] > rgb[..., 2]*1.08)


def _sample(rgb, points):
    shape = points.shape[:-1]
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.remap(rgb, pts[..., 0], pts[..., 1], cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT).reshape(*shape, 3).astype(float)


def _visibility(points, rgb, canopy):
    colour = _sample(rgb, points)
    hidden = _green(colour) | intersects_xy(canopy, points[..., 0], points[..., 1])
    return ~hidden


def _line_samples(a, b):
    # At most one anchor per 1.5 original pixels; subpixel line localization does
    # not manufacture independent evidence from interpolated/enhanced pixels.
    length = np.linalg.norm(b-a)
    count = max(3, min(128, int(length/1.5)))
    return a+np.linspace(.10, .90, count)[:, None]*(b-a)


def _profile(a, b, rgb, raw, canopy, max_shift, discoveries=()):
    delta = b-a
    length = float(np.linalg.norm(delta))
    normal = np.array([delta[1], -delta[0]])/max(length, 1e-9)  # oriented exterior
    centers = _line_samples(a, b)
    offsets = np.unique(np.r_[np.arange(-max_shift, max_shift+.01, 1.), 0.,
                              [item[0] for item in discoveries]])
    points = centers[None]+offsets[:, None, None]*normal
    near_in = points-.75*normal
    far_in = points-2.5*normal
    near_out = points+.75*normal
    interior_points = centers-(max_shift+3.)*normal
    interior = _sample(rgb, interior_points)
    inside, inner, outside = (_sample(rgb, p) for p in (near_in, far_in, near_out))
    valid = (_visibility(near_in, rgb, canopy) & _visibility(far_in, rgb, canopy)
             & ~intersects_xy(canopy, near_out[..., 0], near_out[..., 1])
             & intersects_xy(raw, far_in[..., 0], far_in[..., 1])
             & _visibility(interior_points, rgb, canopy)[None]
             & intersects_xy(raw, interior_points[..., 0], interior_points[..., 1])[None])
    for location in (near_in, far_in, near_out):
        valid &= ((location[..., 0] >= 0) & (location[..., 0] < rgb.shape[1]-1)
                  & (location[..., 1] >= 0) & (location[..., 1] < rgb.shape[0]-1))
    # Normalize illumination before comparing material. A cast shadow scales a
    # coloured roof's RGB too; subtracting its mean would falsely call that a
    # material change. Very dark samples cannot establish chromaticity.
    def chroma(values):
        return values/np.maximum(values.sum(axis=-1, keepdims=True), 45.)*255.
    continuation = ((np.linalg.norm(chroma(inside)-chroma(inner), axis=-1) <= LIMITS['roof_material_chroma'])
                    & (abs(inside.mean(axis=-1)-inner.mean(axis=-1)) <= LIMITS['roof_material_lightness'])
                    & (np.linalg.norm(chroma(inside)-chroma(interior)[None], axis=-1) <= LIMITS['roof_material_chroma'])
                    & (abs(inside.mean(axis=-1)-interior.mean(axis=-1)[None]) <= LIMITS['roof_material_lightness']))
    contrast = np.linalg.norm(inside-outside, axis=-1)/np.sqrt(3)
    step = contrast >= LIMITS['minimum_step_rgb']
    material_exit = ((np.linalg.norm(chroma(inside)-chroma(outside), axis=-1) > LIMITS['roof_material_chroma'])
                     & (inside.mean(axis=-1) >= 15) & (outside.mean(axis=-1) >= 15))
    anchors = valid & continuation & step
    rows = []
    for i, offset in enumerate(offsets):
        count = int(anchors[i].sum())
        # A small unobscured fragment cannot validate the entire side. Hidden
        # spans are retained as inferred only when an actual metre or 20% of a
        # short side has visible anchors (checked in metric units by caller).
        available = int(valid[i].sum())
        fraction = count/max(available, 1)
        gain = float(np.median(contrast[i][anchors[i]])) if count else 0.
        exits = int((anchors[i] & material_exit[i]).sum())
        rows.append({'offset_pixels': float(offset), 'anchor_count': count, 'available_samples': available,
                     'sample_count': len(centers), 'anchor_share': round(fraction, 4),
                     'material_exit_share': round(exits/max(count, 1), 4), 'step_rgb': round(gain, 3),
                     'green_exterior_share': round(float((_green(outside[i]) & anchors[i]).sum()/max(count, 1)), 4),
                     'score': gain*fraction if count >= 3 and fraction >= LIMITS['minimum_anchor_share'] else 0.,
                     'view_origin': next((origin for shift, origin in discoveries if abs(shift-offset) < 1e-7), 'original_rgb_profile')})
    baseline = min(rows, key=lambda row: abs(row['offset_pixels']))
    best = max(rows, key=lambda row: (row['score']-.5*abs(row['offset_pixels']), -abs(row['offset_pixels'])))
    if best['score'] < baseline['score']+8 or abs(best['offset_pixels']) < .5:
        best = baseline
    return best, baseline, normal


def _discovery_lines(upscale):
    if not upscale or upscale.get('status') != 'available':
        return []
    enhanced = cv2.imread(upscale['output_path'], cv2.IMREAD_GRAYSCALE)
    if enhanced is None:
        raise ValueError('Upscale output image is unavailable')
    detected = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(enhanced)[0]
    if detected is None:
        return []
    affine = np.asarray(upscale['upscaled_pixel_center_index_to_source'], dtype=float)
    pairs = np.asarray(detected).reshape(-1, 2, 2)
    pairs = np.c_[pairs.reshape(-1, 2), np.ones(len(pairs)*2)] @ affine
    pairs = pairs.reshape(-1, 2, 2)
    lengths = np.linalg.norm(pairs[:, 1]-pairs[:, 0], axis=1)
    # Bounded nominations; duplicates are collapsed per eave below, not votes.
    return list(pairs[np.argsort(-lengths)[:80]])


def _nominations(a, b, discovered, max_shift):
    tangent = (b-a)/np.linalg.norm(b-a)
    normal = np.array([tangent[1], -tangent[0]])
    nominations = []
    for segment in discovered:
        direction = segment[1]-segment[0]
        if abs(float(direction @ tangent))/max(np.linalg.norm(direction), 1e-9) < np.cos(np.deg2rad(8)):
            continue
        along = (segment-a) @ tangent
        if min(max(along), np.linalg.norm(b-a))-max(min(along), 0) < 3:
            continue
        offsets = (segment-a) @ normal
        shift = float(np.mean(offsets))
        if max(abs(offsets)) <= max_shift and all(abs(shift-old[0]) > .35 for old in nominations):
            nominations.append((shift, 'realesrgan_x4plus_discovery_original_rgb_gate'))
    return nominations[:8]


def _offset_ring(ring, shifts):
    lines = []
    for i, a in enumerate(ring):
        b = ring[(i+1) % len(ring)]
        direction = b-a
        normal = np.array([direction[1], -direction[0]])/np.linalg.norm(direction)
        lines.append((normal, float(normal @ a)+shifts.get(i, 0.)))
    points = []
    for i, (normal, offset) in enumerate(lines):
        previous, old_offset = lines[i-1]
        if abs(np.linalg.det([previous, normal])) < .12:
            return None
        points.append(np.linalg.solve([previous, normal], [old_offset, offset]))
    return np.asarray(points)


def boundary_candidate(obj, scene, image, *, upscale=None):
    """Return a fresh outline comparison/report; never modify the saved object.

    The caller refits the roof on this exact ring using original RGB and checks
    physical geometry. ``accepted`` means source-material support for a review
    hypothesis, never permission to publish or verified hidden construction.
    """
    upscale = {key: value for key, value in upscale.items() if key != 'cache_hit'} if upscale else None
    report = {'method': VERSION, 'accepted': False, 'changed': False, 'review_required': True,
              'scope': 'Unapplied boundary comparison; saved geometry and feedback remain bound to their original outline.',
              'reason': 'insufficient-source-eave-support', 'limits': LIMITS,
              'upscale': upscale or {'status': 'not_requested'},
              'visibility_note': 'Green RGB pixels are unavailable eave samples, not new tree detections. Hidden continuation is inferred.',
              'photometry_source': 'original_rgb_only'}
    report['material_anchor'] = 'Fixed inward source-RGB samples beyond the offset search, plus paired roof-side continuation; insufficient interior coverage is unavailable.'
    raw_ring = (obj.get('boundary_refinement') or {}).get('raw_ring')
    if (not raw_ring or obj.get('source') != 'sam3-text-onnx' or obj.get('geometry_reviewed')
            or obj.get('roof_line_edits') or obj.get('status') in ('rejected', 'superseded')):
        return obj, {**report, 'reason': 'protected-or-missing-raw-detector-boundary'}
    raw = Polygon(raw_ring, obj.get('holes', []))
    current = Polygon(obj['ring'], obj.get('holes', []))
    if not raw.is_valid or not current.is_valid or len(raw_ring) > 300:
        return obj, {**report, 'reason': 'invalid-or-complex-boundary'}
    source_points = np.asarray(raw.exterior.coords)
    if np.any((source_points < .01) | (abs(source_points-np.array(image.size)) < .01)):
        return obj, {**report, 'reason': 'image-cut'}
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    scale = float(np.linalg.svd(frame[:2], compute_uv=False).max())
    limit_px = min(LIMITS['visible_displacement_m']/scale, LIMITS['visible_displacement_source_pixels'])
    canopies = model_canopies(scene.get('objects', []))
    nearby = [p for p in canopies if p.distance(raw) <= 2/scale]
    canopy = unary_union(nearby).buffer(.25/scale)
    rgb = np.asarray(image.convert('RGB'))
    report['detector_canopy_count'] = len(nearby)
    discovered = _discovery_lines(upscale)
    report['enhanced_discovery_segments'] = len(discovered)
    # Always begin from retained raw observations; never progressively refit a
    # saved simplified/reviewer outline. Holes stay exact throughout.
    fresh, _ = refine_roof_surface(raw, image, frame, canopies=canopies)
    seeds = [('raw-mask-bounded-fit', np.asarray(orient(fresh).exterior.coords[:-1]))]
    metric = np.asarray(orient(raw).exterior.coords[:-1]) @ frame[:2]+frame[2]
    for tolerance in (.45, .9):
        fit = _fitted_eaves(metric, tolerance)
        if fit is None:
            continue
        pixels = (fit-frame[2]) @ np.linalg.inv(frame[:2])
        polygon = Polygon(pixels)
        if not polygon.is_valid or len(pixels) > 48:
            continue
        # Larger smoothing is only available where original RGB or eligible
        # detector canopy masks make the raw edge unobservable. Visible recesses
        # retain the production cleanup budget.
        samples = np.concatenate([_line_samples(a, b) for a, b in zip(source_points[:-1], source_points[1:], strict=True)])
        visible = _visibility(samples, rgb, canopy)
        distance = polygon.boundary.distance(shapely_points(samples))
        if np.any(distance[visible]*scale > .45):
            continue
        seeds.append((f'raw-eave-runs-{tolerance:g}m', np.asarray(orient(polygon).exterior.coords[:-1])))
    candidates = []
    ambiguous = []
    for method, ring in seeds:
        if len(ring) > 48:
            continue
        evidence = []
        for i, a in enumerate(ring):
            b = ring[(i+1) % len(ring)]
            best, baseline, _ = _profile(a, b, rgb, raw, canopy, limit_px, _nominations(a, b, discovered, limit_px))
            length_m = float(np.linalg.norm(b-a)*scale)
            supported_length = length_m*best['anchor_count']/best['sample_count']
            valid = best['score'] > 0 and supported_length >= min(1., length_m*.2)
            supported = (valid and best['material_exit_share'] >= LIMITS['minimum_anchor_share']
                         and best['green_exterior_share'] < .5)
            evidence.append({**best, 'edge': i, 'line_pixels': [a.tolist(), b.tolist()], 'length_m': round(length_m, 3),
                             'supported_length_m': round(supported_length, 3), 'original_baseline_step_rgb': baseline['step_rgb'],
                             'status': 'material_exit_supported' if supported else
                                       'vegetation_adjacent_ambiguous' if valid and best['green_exterior_share'] >= .5 else
                                       'brightness_edge_ambiguous' if valid else 'unavailable',
                             'discovery_origin': best['view_origin']})
        movable = [e for e in evidence if e['status'] != 'unavailable' and abs(e['offset_pixels']) >= .5]
        movable.sort(key=lambda e: -(e['score']*e['supported_length_m']))
        if method != 'raw-mask-bounded-fit' and any(e['status'] == 'unavailable' for e in evidence):
            continue
        # Whole fit, all supported offsets, or one of four strongest source
        # nominations. This is a small finite comparison, not a boundary search.
        seed_uncertain = any(e['status'] != 'material_exit_supported' for e in evidence)
        choices = [((), seed_uncertain), (tuple(e['edge'] for e in movable if e['status'] == 'material_exit_supported'), seed_uncertain),
                   (tuple(e['edge'] for e in movable), True)]
        choices += [((e['edge'],), e['status'] != 'material_exit_supported' or (method != 'raw-mask-bounded-fit' and seed_uncertain)) for e in movable[:4]]
        for owners, uncertain in choices:
            shifts = {i: evidence[i]['offset_pixels'] for i in owners}
            corners = _offset_ring(ring, shifts) if shifts else ring
            if corners is None:
                continue
            candidate = Polygon(corners, obj.get('holes', []))
            if not candidate.is_valid or not box(0, 0, *image.size).covers(candidate):
                continue
            difference = candidate.symmetric_difference(raw).area/max(raw.area, 1e-9)
            visible_distances, hidden_distances = [], []
            for first, second in ((raw, candidate), (candidate, raw)):
                vertices = np.asarray(first.exterior.coords)
                samples = np.concatenate([vertices, *[_line_samples(a, b) for a, b in zip(vertices[:-1], vertices[1:], strict=True)]])
                visible = _visibility(samples, rgb, canopy)
                distance = second.exterior.distance(shapely_points(samples))*scale
                visible_distances.extend(distance[visible])
                hidden_distances.extend(distance[~visible])
            visible_max = float(max(visible_distances, default=0))
            hidden_max = float(max(hidden_distances, default=0))
            displacement = max(visible_max, hidden_max)
            if (difference > LIMITS['area_change_fraction'] or visible_max > limit_px*scale
                    or hidden_max > LIMITS['hidden_displacement_m']):
                continue
            if candidate.exterior.hausdorff_distance(current.exterior) < .1:
                continue
            # A simplified side needs some visible source support; canopy or
            # pixels generated by a model never locate an entirely hidden eave.
            if not owners and any(e['status'] == 'unavailable' for e in evidence):
                continue
            moved = [evidence[i] for i in owners]
            gain = sum(max(0., e['score']-e['original_baseline_step_rgb'])*e['supported_length_m'] for e in moved)
            if owners and gain <= 0:
                continue
            inferred = []
            for a, b in zip(corners, np.roll(corners, -1, axis=0), strict=True):
                samples = _line_samples(a, b)
                hidden = ~_visibility(samples, rgb, canopy)
                if hidden.mean() >= .15:
                    inferred.append({'line_pixels': [a.tolist(), b.tolist()],
                                     'hidden_fraction': round(float(hidden.mean()), 4), 'status': 'inferred_continuation_not_observed'})
            detail = {**report, 'accepted': not uncertain, 'changed': True,
                      'reason': 'source-material-eave-hypothesis' if not uncertain else 'ambiguous-brightness-edge-comparison',
                      'seed': method, 'edge_evidence': evidence, 'moved_edges': list(owners), 'inferred_edges': inferred,
                      'input_vertices': len(raw_ring), 'output_vertices': len(corners),
                      'area_change_fraction': round(difference, 5), 'max_displacement_m': round(displacement, 4),
                      'max_visible_displacement_m': round(visible_max, 4), 'max_hidden_displacement_m': round(hidden_max, 4),
                      'source_pixel_resolution_m': scale,
                      'ambiguity': 'An interior cast shadow, painted strip or equipment can resemble an eave. Brightness-only edges remain unaccepted comparisons.'}
            item = (gain, candidate, detail)
            (ambiguous if uncertain else candidates).append(item)
    pool = candidates or ambiguous
    if not pool:
        return obj, report
    _, polygon, report = max(pool, key=lambda item: item[0])
    candidate = {**obj, 'ring': list(map(list, polygon.exterior.coords[:-1])),
                 'holes': obj.get('holes', []), 'boundary_guidance': report,
                 'model': {**obj.get('model', {}), 'area_m2': float(polygon.area*abs(np.linalg.det(frame[:2])))}}
    return candidate, report
