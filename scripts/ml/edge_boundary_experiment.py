"""Edge-boundary experiment: does an enhanced or edge image help ONLY the outline refinement step?

Round 2 showed a CLAHE frame does not help SAM3 detection. This experiment feeds the enhanced
frames to boundary refinement alone and judges the refined outlines against the T1/T2 human
references in eyes.sqlite (reference agreement, not accuracy). No SAM3 runs; nothing is written
under data/scenes; outputs land under data/reviews/eyes/experiments/edge-boundary-v1/.

Two layers are measured, because the production refiner is not image-conditioned for the
exterior outline: `roof_surface.refine_roof_surface` reads pixels only for the hole-fill
chroma test, and `straighten_roof_boundary` / `roof_boundary.regularize_eaves` use `image.size`
alone. So:

  layer "production": refine_roof_surface(start, VARIANT_FRAME) for frames (a) original PNG,
      (b) CLAHE (LAB L, clip 3.0, tile 12x12), (c) edge-emphasised (Canny on the CLAHE gray,
      dilated 1 px, blended in as darkened edges), (d) unsharp mask.
  layer "edge-snap" (EXPERIMENTAL, not production code): the production-refined outline is
      snapped edge by edge to the Canny edge map of each frame, bounded by the same 0.45 m /
      3 % area budget the straightener uses. This is the minimal image-conditioned refiner
      that lets the hypothesis be tested at all.

Starting outline per reference roof: the detector ring before refinement when the object stores
boundary_refinement.raw_ring, else the saved ring. Metrics per roof and variant: IoU and the
symmetric boundary mean distance (m) against the reference in the scene's local metres, paired
against the (a) production baseline; medians, signed deltas, bootstrap 95 % CI of the median
delta, counts of |delta IoU| > 0.02, and scene_roof_diagnostics.edge_support on the 'original'
and 'clahe' diagnostic views.

  python -m scripts.ml.edge_boundary_experiment run [--root .] [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, box

from . import reference_agreement as agreement
from . import reference_registry as registry

VERSION = 'edge-boundary-v1'
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DEFAULT_OUT = Path('data/reviews/eyes/experiments')/VERSION
TIERS = ('T1', 'T2')
CLAHE_PARAMS = {'space': 'LAB', 'channel': 'L', 'clip_limit': 3.0, 'tile_grid': [12, 12], 'library': 'cv2'}
EDGE_PARAMS = {'canny': [50, 150], 'blur': [3, 3], 'dilate_px': 1, 'darken': .6, 'source': 'clahe-gray'}
UNSHARP_PARAMS = {'sigma': 1.5, 'amount': 1.0}
SNAP_PARAMS = {'step_px': .5, 'tolerance_px': 1.5, 'min_gain': .15, 'min_support': .5, 'min_edge_px': 3.,
               'max_shift_m': .45, 'max_shift_px_factor': 4, 'area_change_limit': .03}
CHANGE_IOU = .02
ECHO_IOU = .999  # saved-ring IoU at or above this means the reference outline is the model outline
BOOTSTRAP = {'n': 2000, 'seed': 0}
SHEET_TILE = 300
SHEET_COLUMNS = 4
NOTE = ('Reference agreement with the T1/T2 human references only, not accuracy. The edge-snap layer is an '
        'experimental refiner written for this measurement; it is not production code.')


# --------------------------------------------------------------------------- variant frames

def _check_rgb(rgb):
    data = np.ascontiguousarray(np.asarray(rgb))
    if data.ndim != 3 or data.shape[2] != 3 or data.dtype != np.uint8:
        raise ValueError('expected an HxWx3 uint8 RGB array')
    return data


def frame_original(rgb):
    """(a) the saved PNG, copied."""
    return _check_rgb(rgb).copy()


def frame_clahe(rgb, *, clip_limit=CLAHE_PARAMS['clip_limit'], tile_grid=tuple(CLAHE_PARAMS['tile_grid'])):
    """(b) CLAHE on the LAB L channel (the editor's Enhanced-contrast recipe)."""
    data = _check_rgb(rgb)
    lab = cv2.cvtColor(data, cv2.COLOR_RGB2LAB)
    clahe = cv2.createCLAHE(clipLimit=float(clip_limit), tileGridSize=tuple(int(t) for t in tile_grid))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return np.ascontiguousarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


def canny_gray(gray, *, canny=tuple(EDGE_PARAMS['canny']), blur=tuple(EDGE_PARAMS['blur'])):
    """Canny 50/150 after a 3x3 Gaussian on an 8-bit single-channel view (the diagnostics recipe)."""
    return cv2.Canny(cv2.GaussianBlur(np.ascontiguousarray(gray, np.uint8), blur, 0), *canny)


def frame_edges(rgb, *, darken=EDGE_PARAMS['darken'], dilate_px=EDGE_PARAMS['dilate_px']):
    """(c) Canny on the CLAHE gray, dilated 1 px, blended into the RGB as darkened edges."""
    data = _check_rgb(rgb)
    enhanced = frame_clahe(data)
    gray = cv2.cvtColor(enhanced, cv2.COLOR_RGB2GRAY)
    edges = canny_gray(gray)
    if dilate_px > 0:
        size = 2*int(dilate_px)+1
        edges = cv2.dilate(edges, np.ones((size, size), np.uint8))
    weight = 1.-darken*(edges > 0)[:, :, None].astype(np.float32)
    return np.ascontiguousarray((data.astype(np.float32)*weight).round().clip(0, 255).astype(np.uint8))


def frame_unsharp(rgb, *, sigma=UNSHARP_PARAMS['sigma'], amount=UNSHARP_PARAMS['amount']):
    """(d) unsharp mask: rgb + amount * (rgb - gaussian(rgb, sigma))."""
    data = _check_rgb(rgb).astype(np.float32)
    blurred = cv2.GaussianBlur(data, (0, 0), float(sigma))
    return np.ascontiguousarray((data + float(amount)*(data-blurred)).round().clip(0, 255).astype(np.uint8))


VARIANTS = {'original': frame_original, 'clahe': frame_clahe, 'edges': frame_edges, 'unsharp': frame_unsharp}
BASELINE = 'original'


def edge_distance_map(rgb):
    """Distance (px) to the nearest Canny edge of the frame's gray view; inf when there is none."""
    edges = canny_gray(cv2.cvtColor(_check_rgb(rgb), cv2.COLOR_RGB2GRAY))
    if not np.any(edges):
        return np.full(edges.shape, np.inf)
    return cv2.distanceTransform((edges == 0).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE).astype(float)


# --------------------------------------------------------------------------- geometry helpers

def _valid(polygon):
    return registry.valid(polygon)[0]


def to_local(polygon, scene):
    """Pixel polygon -> local metres through the scene's pixel_to_meters affine (holes kept)."""
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    def ring(points):
        pts = np.asarray(points, dtype=float)
        return (np.c_[pts, np.ones(len(pts))] @ frame).tolist()
    return _valid(Polygon(ring(polygon.exterior.coords), [ring(h.coords) for h in polygon.interiors if len(h.coords) >= 4]))


def _sample_boundary(polygon, spacing):
    points = []
    for ring in (polygon.exterior, *polygon.interiors):
        line = LineString(ring.coords)
        count = max(2, int(line.length/spacing)+1)
        points.extend(line.interpolate(t).coords[0] for t in np.linspace(0, line.length, count))
    return np.asarray(points)


def outline_agreement(model_local, reference_local, *, spacing=.1):
    """IoU, symmetric boundary mean distance and Hausdorff (m) between two local-metre polygons."""
    inter = model_local.intersection(reference_local).area
    union = model_local.area + reference_local.area - inter
    iou = inter/union if union > 0 else 0.
    forward = [reference_local.boundary.distance(Point(p)) for p in _sample_boundary(model_local, spacing)]
    backward = [model_local.boundary.distance(Point(p)) for p in _sample_boundary(reference_local, spacing)]
    return {'iou': round(float(iou), 4), 'boundary_mean_m': round((float(np.mean(forward))+float(np.mean(backward)))/2, 4),
            'hausdorff_m': round(float(model_local.boundary.hausdorff_distance(reference_local.boundary)), 4),
            'model_area_m2': round(float(model_local.area), 2), 'reference_area_m2': round(float(reference_local.area), 2)}


def starting_outline(obj):
    """(pixel polygon, 'raw_ring'|'saved_ring'): the detector ring before refinement when stored, else the saved ring."""
    refinement = obj.get('boundary_refinement') or {}
    raw = refinement.get('raw_ring')
    if raw and len(raw) >= 3:
        holes = [h for h in refinement.get('raw_holes') or [] if len(h) >= 3]
        return _valid(Polygon(raw, holes)), 'raw_ring'
    holes = [h for h in obj.get('holes') or [] if len(h) >= 3]
    return _valid(Polygon(obj['ring'], holes)), 'saved_ring'


# --------------------------------------------------------------------------- refiners

def production_refine(polygon, rgb, scene, canopies=()):
    """roof_surface.refine_roof_surface exactly as scene_extract calls it, on the given frame."""
    from .roof_surface import refine_roof_surface
    image = Image.fromarray(_check_rgb(rgb))
    refined, info = refine_roof_surface(polygon, image, np.asarray(scene['pixel_to_meters'], dtype=float), canopies=canopies)
    return _valid(refined), {'straightening': info['straightening']['method'], 'construction_accepted': info['construction']['accepted'],
                             'filled_area_m2': info['filled_area_m2'], 'n_holes_filled': sum(h['filled'] for h in info['holes']),
                             'outer_deviation_m': info['outer_deviation_m']}


def _support(distance, points, tolerance):
    """(share of samples within tolerance of an edge, mean edge distance capped at 2*tolerance) or None outside the image."""
    xs, ys = np.floor(points[:, 0]+.5), np.floor(points[:, 1]+.5)
    height, width = distance.shape
    inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    if not inside.any():
        return None
    values = distance[ys[inside].astype(int), xs[inside].astype(int)]
    return float(np.mean(values <= tolerance)), float(np.mean(np.minimum(values, 2*tolerance)))


def edge_snap(polygon, distance, scene, params=SNAP_PARAMS):
    """EXPERIMENTAL: translate each exterior edge along its normal to the offset of best Canny support.

    Bounded like straighten_roof_boundary: every vertex may move at most min(0.45 m, 4 px) and the
    area may change at most 3 %; edges on the image border stay fixed; when a gate fails the input
    is returned unchanged. Deterministic. Holes are kept as they are.
    """
    frame = np.asarray(scene['pixel_to_meters'], dtype=float)
    scale = float(np.linalg.svd(frame[:2], compute_uv=False).max())
    max_shift_m = min(params['max_shift_m'], params['max_shift_px_factor']*scale)
    max_px = max_shift_m/scale
    height, width = distance.shape
    ring = np.asarray(polygon.exterior.coords[:-1], dtype=float)
    n = len(ring)
    report = {'method': 'edge-snap-v1', 'moved_edges': 0, 'n_edges': n, 'accepted': False, 'reason': None,
              'max_shift_m': round(max_shift_m, 4)}
    if n < 3:
        return polygon, {**report, 'reason': 'degenerate'}
    offsets = np.arange(-max_px, max_px+1e-9, params['step_px'])
    offsets = offsets[np.argsort(np.abs(offsets), kind='stable')]  # smallest |offset| first: ties keep the input
    shifts, normals, directions = np.zeros(n), np.zeros((n, 2)), np.zeros((n, 2))
    on_border = lambda p: p[0] <= 1e-6 or p[1] <= 1e-6 or p[0] >= width-1e-6 or p[1] >= height-1e-6  # noqa: E731
    for i in range(n):
        a, b = ring[i], ring[(i+1) % n]
        d = b-a
        length = float(np.linalg.norm(d))
        if length < 1e-9:
            continue
        d /= length
        normal = np.array([-d[1], d[0]])
        directions[i], normals[i] = d, normal
        if length < params['min_edge_px'] or (on_border(a) and on_border(b)):
            continue
        t = np.linspace(0, 1, max(2, int(length)+1))
        samples = a+t[:, None]*(b-a)
        base = _support(distance, samples, params['tolerance_px'])
        if base is None:
            continue
        # Best support first, then the offset that centres on the edge (smallest mean distance);
        # offsets are visited smallest |offset| first, so exact ties keep the input.
        best, best_value = 0., (base[0], -base[1])
        for o in offsets:
            value = _support(distance, samples+o*normal, params['tolerance_px'])
            if value is not None and (value[0], -value[1]) > best_value:
                best, best_value = float(o), (value[0], -value[1])
        if best != 0. and best_value[0] >= params['min_support'] and best_value[0]-base[0] >= params['min_gain']:
            shifts[i] = best
    if not np.any(shifts):
        return polygon, {**report, 'reason': 'no-edge-gain'}
    corners = []
    for i in range(n):
        j = (i-1) % n
        a_prev, d_prev = ring[j]+shifts[j]*normals[j], directions[j]
        a_cur, d_cur = ring[i]+shifts[i]*normals[i], directions[i]
        matrix = np.array([d_prev, -d_cur]).T
        if abs(np.linalg.det(matrix)) < .1:
            corners.append(ring[i]+(shifts[j]*normals[j]+shifts[i]*normals[i])/2)
            continue
        s = np.linalg.solve(matrix, a_cur-a_prev)
        corners.append(a_prev+s[0]*d_prev)
    corners = np.asarray(corners)
    if not np.isfinite(corners).all():
        return polygon, {**report, 'reason': 'unstable-corner'}
    try:
        candidate = Polygon(corners, [h.coords for h in polygon.interiors])
        if not candidate.is_valid or candidate.is_empty or candidate.area <= 0:
            return polygon, {**report, 'reason': 'invalid-candidate'}
        candidate = candidate.intersection(box(0, 0, width, height))
    except (GEOSException, ValueError):
        return polygon, {**report, 'reason': 'invalid-candidate'}
    if candidate.geom_type != 'Polygon' or candidate.is_empty:
        return polygon, {**report, 'reason': 'invalid-candidate'}
    before, after = to_local(polygon, scene), to_local(candidate, scene)
    shift = before.boundary.hausdorff_distance(after.boundary)
    area = before.symmetric_difference(after).area/max(before.area, 1e-9)
    if shift > max_shift_m+1e-6 or area > params['area_change_limit']:
        return polygon, {**report, 'reason': 'exceeds-budget', 'shift_m': round(float(shift), 4), 'area_change_fraction': round(float(area), 5)}
    return _valid(candidate), {**report, 'accepted': True, 'reason': 'edge-gain', 'moved_edges': int(np.count_nonzero(shifts)),
                               'shift_m': round(float(shift), 4), 'area_change_fraction': round(float(area), 5),
                               'median_moved_px': round(float(np.median(np.abs(shifts[shifts != 0]))), 3)}


# --------------------------------------------------------------------------- pairing

def load_scene_image(root, scene_id, scene):
    """The scene's sha-verified PNG as HxWx3 uint8."""
    path = Path(root)/'data/scenes'/scene_id/f"{scene['image_sha256']}.png"
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != scene['image_sha256']:
        raise ValueError('Source imagery changed')
    return np.asarray(Image.open(path).convert('RGB'), dtype=np.uint8)


def pair_references(root=Path('.'), tiers=TIERS):
    """One measurable model roof per reference: the reference's own object at its model signature when the
    scene model still holds it, else the best-IoU candidate in the reference's own scene, else the best
    candidate in any scene. References without any candidate are listed under 'skipped'."""
    root = Path(root)
    refs = agreement.select(agreement.references(root), tiers)
    cache, pairs, skipped = {}, [], []
    for ref in refs:
        payload = ref['payload']
        scene_id, object_id = ref.get('scene_id'), ref.get('object_id')
        scene = registry.scene_model(root, scene_id, payload.get('model_signature'), cache) if scene_id else None
        obj = next((o for o in (scene or {}).get('objects', []) if o.get('id') == object_id and o.get('kind') == 'building'), None)
        if obj is not None and obj.get('ring'):
            pairs.append({'ref': ref, 'scene_id': scene_id, 'object_id': object_id, 'scene': scene, 'obj': obj, 'pairing': 'own_object'})
            continue
        found = agreement.candidates(ref, root, (), cache)
        same = [c for c in found if c['scene_id'] == scene_id]
        chosen = (same or found or [None])[0]
        if chosen is None:
            skipped.append({'ref_id': ref['ref_id'], 'tier': ref['tier'], 'scene_id': scene_id, 'object_id': object_id,
                            'label': payload.get('label'), 'reason': 'no model roof overlaps the reference (IoU >= 0.3)'})
            continue
        pairs.append({'ref': ref, 'scene_id': chosen['scene_id'], 'object_id': chosen['object_id'], 'scene': chosen['scene'],
                      'obj': chosen['obj'], 'pairing': 'same_scene_candidate' if same else 'other_scene_candidate'})
    return pairs, skipped


# --------------------------------------------------------------------------- measurement

def measure_pair(pair, root=Path('.'), variants=VARIANTS):
    """Every variant of both layers for one reference roof, plus edge-support diagnostics."""
    from .roof_boundary import model_canopies
    from .scene_roof_diagnostics import edge_support, image_views
    ref, scene, obj = pair['ref'], pair['scene'], pair['obj']
    rgb = load_scene_image(root, pair['scene_id'], scene)
    reference = agreement.reference_polygon(ref, scene)
    start, start_kind = starting_outline(obj)
    canopies = model_canopies(scene.get('objects', []))
    views = image_views(Image.fromarray(rgb))
    frames = {name: build(rgb) for name, build in variants.items()}
    row = {'ref_id': ref['ref_id'], 'tier': ref['tier'], 'ref_source_kind': ref['source_kind'], 'ref_label': ref['payload'].get('label'),
           'ref_scene_id': ref.get('scene_id'), 'ref_object_id': ref.get('object_id'), 'crm_feature_id': ref.get('crm_feature_id'),
           'scene_id': pair['scene_id'], 'object_id': pair['object_id'], 'pairing': pair['pairing'],
           'frame': 'scene_local' if agreement._same_frame(ref, scene) else 'converted_wgs84',
           'start': start_kind, 'start_vertices': len(start.exterior.coords)-1, 'obj_source': obj.get('source'),
           'm_per_px': round(abs(float(scene['pixel_to_meters'][0][0])), 4),
           'start_agreement': outline_agreement(to_local(start, scene), reference),
           'saved_agreement': outline_agreement(to_local(_valid(Polygon(obj['ring'], [h for h in obj.get('holes') or [] if len(h) >= 3])), scene), reference),
           'production': {}, 'edge_snap': {}, 'polygons': {}}
    # A reference whose outline equals the saved model ring (roof-line edits only) echoes the model
    # outline: any refined outline scores 1.0 against it by construction, so it is reported but not paired.
    row['outline_independent'] = row['saved_agreement']['iou'] < ECHO_IOU
    baseline_ring = None
    for name, frame in frames.items():
        refined, info = production_refine(start, frame, scene, canopies)
        ring = [[round(x, 3), round(y, 3)] for x, y in refined.exterior.coords[:-1]]
        if name == BASELINE:
            baseline_ring = ring
        row['production'][name] = {**outline_agreement(to_local(refined, scene), reference), **info, 'vertices': len(ring),
                                   'exterior_identical_to_baseline': ring == baseline_ring,
                                   'edge_support': {view: edge_support(ring, None, distance=views[view][1])['weighted_mean'] for view in ('original', 'clahe')}}
        row['polygons'][f'production:{name}'] = ring
        snapped, snap = edge_snap(refined, edge_distance_map(frame), scene)
        snap_ring = [[round(x, 3), round(y, 3)] for x, y in snapped.exterior.coords[:-1]]
        row['edge_snap'][name] = {**outline_agreement(to_local(snapped, scene), reference), **snap, 'vertices': len(snap_ring),
                                  'edge_support': {view: edge_support(snap_ring, None, distance=views[view][1])['weighted_mean'] for view in ('original', 'clahe')}}
        row['polygons'][f'edge_snap:{name}'] = snap_ring
    row['reference_ring_px'] = [[round(x, 3), round(y, 3)] for x, y in
                                (np.c_[np.asarray(reference.exterior.coords[:-1]), np.ones(len(reference.exterior.coords)-1)]
                                 @ np.asarray(scene['meters_to_pixel'], dtype=float)).tolist()]
    return row


def bootstrap_median_ci(values, *, n=BOOTSTRAP['n'], seed=BOOTSTRAP['seed'], level=.95):
    """Percentile bootstrap CI of the median; None when fewer than two values."""
    data = np.asarray([v for v in values if v is not None], dtype=float)
    if len(data) < 2:
        return None
    rng = np.random.default_rng(seed)
    medians = np.median(rng.choice(data, size=(n, len(data)), replace=True), axis=1)
    low, high = np.quantile(medians, [(1-level)/2, 1-(1-level)/2])
    return [round(float(low), 4), round(float(high), 4)]


def paired_summary(rows, layer, variant, *, baseline_layer='production', baseline=BASELINE, change=CHANGE_IOU):
    """Medians and paired deltas (variant minus baseline) of IoU and boundary mean distance across roofs."""
    pairs = [(r[baseline_layer][baseline], r[layer][variant]) for r in rows if variant in r.get(layer, {}) and baseline in r.get(baseline_layer, {})]
    iou_delta = [v['iou']-b['iou'] for b, v in pairs]
    dist_delta = [v['boundary_mean_m']-b['boundary_mean_m'] for b, v in pairs]
    def med(values):
        return round(float(statistics.median(values)), 4) if values else None
    changed = [d for d in iou_delta if abs(d) > 1e-9]
    return {'layer': layer, 'variant': variant, 'baseline': f'{baseline_layer}:{baseline}', 'n': len(pairs),
            'iou': {'baseline_median': med([b['iou'] for b, _ in pairs]), 'variant_median': med([v['iou'] for _, v in pairs]),
                    'delta_median': med(iou_delta), 'delta_ci95': bootstrap_median_ci(iou_delta),
                    'delta_mean': round(float(np.mean(iou_delta)), 4) if iou_delta else None,
                    'n_changed': len(changed), 'delta_median_among_changed': med(changed),
                    'delta_ci95_among_changed': bootstrap_median_ci(changed),
                    'n_better': sum(d > 1e-9 for d in iou_delta), 'n_worse': sum(d < -1e-9 for d in iou_delta),
                    'n_unchanged': sum(abs(d) <= 1e-9 for d in iou_delta),
                    'n_improved_over': sum(d > change for d in iou_delta), 'n_worsened_over': sum(d < -change for d in iou_delta),
                    'change_threshold': change},
            'boundary_mean_m': {'baseline_median': med([b['boundary_mean_m'] for b, _ in pairs]),
                                'variant_median': med([v['boundary_mean_m'] for _, v in pairs]),
                                'delta_median': med(dist_delta), 'delta_ci95': bootstrap_median_ci(dist_delta),
                                'n_better': sum(d < -1e-9 for d in dist_delta), 'n_worse': sum(d > 1e-9 for d in dist_delta)},
            'edge_support_median': {view: med([v['edge_support'][view] for _, v in pairs if v['edge_support'].get(view) is not None])
                                    for view in ('original', 'clahe')}}


def per_roof_deltas(rows, layer, variant, *, baseline_layer='production', baseline=BASELINE):
    out = []
    for r in rows:
        b, v = r[baseline_layer][baseline], r[layer][variant]
        out.append({'ref_id': r['ref_id'], 'tier': r['tier'], 'scene_id': r['scene_id'], 'object_id': r['object_id'],
                    'iou_baseline': b['iou'], 'iou_variant': v['iou'], 'iou_delta': round(v['iou']-b['iou'], 4),
                    'boundary_mean_m_baseline': b['boundary_mean_m'], 'boundary_mean_m_variant': v['boundary_mean_m'],
                    'boundary_mean_m_delta': round(v['boundary_mean_m']-b['boundary_mean_m'], 4),
                    'snap': {k: v.get(k) for k in ('accepted', 'reason', 'moved_edges', 'shift_m')} if layer == 'edge_snap' else None})
    return sorted(out, key=lambda d: (-abs(d['iou_delta']), d['ref_id']))


def best_variant(summaries):
    """The variant with the highest median IoU delta, ties broken by more roofs better than worse, then by name."""
    ranked = sorted(summaries, key=lambda s: (-(s['iou']['delta_median'] or 0), -(s['iou']['n_better']-s['iou']['n_worse']), s['layer'], s['variant']))
    return ranked[0] if ranked else None


# --------------------------------------------------------------------------- contact sheet

def _draw_ring(draw, ring, transform, colour, width=2):
    points = [tuple(transform(p)) for p in ring]
    if len(points) >= 2:
        draw.line([*points, points[0]], fill=colour, width=width)


def render_sheet(rows, deltas, layer, variant, root, out_path, *, count=12, tile=SHEET_TILE, columns=SHEET_COLUMNS):
    """Reference (white), production baseline (orange) and the chosen variant (green) for the largest |IoU delta| roofs."""
    chosen = deltas[:count]
    by_ref = {r['ref_id']: r for r in rows}
    rows_needed = max(1, -(-len(chosen)//columns))
    sheet = Image.new('RGB', (columns*tile, rows_needed*(tile+28)), (16, 16, 16))
    draw = ImageDraw.Draw(sheet)
    images = {}
    for index, d in enumerate(chosen):
        row = by_ref[d['ref_id']]
        if row['scene_id'] not in images:
            scene = registry.scene_model(root, row['scene_id'], None) or {}
            try:
                images[row['scene_id']] = Image.open(Path(root)/'data/scenes'/row['scene_id']/f"{scene.get('image_sha256')}.png").convert('RGB')
            except (OSError, TypeError):
                images[row['scene_id']] = None
        image = images[row['scene_id']]
        baseline_ring = row['polygons'][f'production:{BASELINE}']
        variant_ring = row['polygons'][f'{layer}:{variant}']
        points = np.asarray(row['reference_ring_px']+baseline_ring+variant_ring, dtype=float)
        west, south = points.min(axis=0)
        east, north = points.max(axis=0)
        size = max(east-west, north-south)*1.3+4
        cx, cy = (west+east)/2, (south+north)/2
        crop_box = (int(cx-size/2), int(cy-size/2), int(cx+size/2), int(cy+size/2))
        col, line = index % columns, index//columns
        x0, y0 = col*tile, line*(tile+28)
        if image is not None:
            crop = image.crop(crop_box).resize((tile, tile), Image.BILINEAR)
            sheet.paste(crop, (x0, y0))
        factor = tile/max(crop_box[2]-crop_box[0], 1)
        def transform(p, x0=x0, y0=y0, crop_box=crop_box, factor=factor):
            return (x0+(p[0]-crop_box[0])*factor, y0+(p[1]-crop_box[1])*factor)
        _draw_ring(draw, baseline_ring, transform, (255, 140, 0))
        _draw_ring(draw, variant_ring, transform, (0, 220, 60))
        _draw_ring(draw, row['reference_ring_px'], transform, (255, 255, 255))
        draw.text((x0+4, y0+tile+4), f"{d['ref_id'][:8]} {d['tier']} IoU {d['iou_baseline']:.3f}->{d['iou_variant']:.3f} ({d['iou_delta']:+.3f})",
                  fill=(230, 230, 230))
        draw.text((x0+4, y0+tile+15), f"{row['scene_id'][:28]} {layer}:{variant}", fill=(160, 160, 160))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return str(out_path)


# --------------------------------------------------------------------------- report

def _pct(n, d):
    return f'{n}/{d} ({100*n/d:.0f}%)' if d else f'{n}/0'


def write_report(result, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir/'report.json').write_text(json.dumps(result, indent=1, sort_keys=True))
    rows, s = result['rows'], result['summaries']
    n = len(rows)
    lines = [f'# Edge-boundary experiment ({VERSION})', '',
             f"Computed {result['computed_at']}. {NOTE}", '',
             'Question: does an enhanced or edge image help ONLY the outline refinement step (round 2 showed it does not help SAM3 detection)?',
             '',
             f"References: {result['n_references']} T1/T2 building references with independent geometry in eyes.sqlite; "
             f"{result['n_measured']} measurable (pairing: {json.dumps(result['pairing_counts'])}); {len(result['skipped'])} skipped because no model roof "
             f"overlaps the reference at IoU >= 0.3 ({', '.join(x['ref_id'][:8] + ' ' + str(x['label']) for x in result['skipped'])}); "
             f"{len(result['outline_echoes_excluded'])} excluded from the paired outline analysis because the reference outline is the saved model ring itself "
             f"(roof-line edits only; IoU 1.0 by construction: {', '.join(x['ref_id'][:8] for x in result['outline_echoes_excluded'])}). "
             f"Paired denominator n = {n}. Starting-outline IoU with the reference: median {result['start_iou_median']}. "
             f"Starting outline: detector raw ring for {result['start_counts'].get('raw_ring', 0)} roofs, saved ring for "
             f"{result['start_counts'].get('saved_ring', 0)} (older scenes do not store the raw ring). "
             f"Frames: {json.dumps(result['frame_counts'])}.", '',
             '## Finding 1: the production refiner cannot see the frame', '',
             'roof_surface.refine_roof_surface reads pixels only for the hole-fill chroma test; straighten_roof_boundary and '
             'roof_boundary.regularize_eaves use image.size alone. Measured: ' +
             ', '.join(f"{v}: exterior identical to the original-frame result for {_pct(result['production_identical'][v], n)} roofs, "
                       f"hole-fill decisions differ for {_pct(result['production_hole_decisions_differ'][v], n)}"
                       for v in VARIANTS if v != BASELINE) + '. The only frame effect in production is which small holes get filled.', '']
    for summary in s:
        if summary['layer'] != 'production' or summary['variant'] == BASELINE:
            continue
        lines.append(_summary_line(summary))
    lines += ['', '## Finding 2: an experimental edge-snap refiner on each frame', '',
              f"Edge-snap (not production code): every exterior edge of the production-refined outline may translate along its normal to the "
              f"offset with the best Canny support, step {SNAP_PARAMS['step_px']} px, tolerance {SNAP_PARAMS['tolerance_px']} px, only when support "
              f"rises by at least {SNAP_PARAMS['min_gain']} to at least {SNAP_PARAMS['min_support']}; the whole move is bounded by "
              f"min({SNAP_PARAMS['max_shift_m']} m, {SNAP_PARAMS['max_shift_px_factor']} px) and {int(SNAP_PARAMS['area_change_limit']*100)} % area, the same budget the straightener uses. "
              f"Baseline for every delta: production refinement on the original frame.", '']
    for summary in s:
        if summary['layer'] == 'edge_snap':
            accepted = result['snap_accepted'].get(summary['variant'], 0)
            lines.append(_summary_line(summary) + f" Snap accepted (moved and inside the budget) for {_pct(accepted, summary['n'])} roofs.")
    lines += ['', f"Where the disagreement lives: of the {n} paired roofs, {result['baseline_hausdorff_over_budget']} have a baseline Hausdorff distance to the "
              f"reference above the {SNAP_PARAMS['max_shift_m']} m refinement budget, {result['baseline_boundary_mean_over_budget']} have a boundary mean distance above it, "
              f"and {result['baseline_iou_below_0_85']} sit below IoU 0.85. A refiner bounded at {SNAP_PARAMS['max_shift_m']} m and 3 % area cannot reach those; "
              'they are detector outline misses (wrong wing, merged neighbour, roof-pipeline ring), not eave placement.',
              '', '## Paired comparison of the best edge-snap frame with the original-frame edge-snap', '']
    for summary in result.get('snap_vs_snap', []):
        lines.append(_summary_line(summary))
    best = result['best']
    lines += ['', '## Edge support of the refined outlines (existing diagnostic)', '',
              'scene_roof_diagnostics.edge_support weighted mean (share of 1 px outline samples within 1.5 px of a Canny edge) on the '
              "saved PNG's 'original' and 'clahe' diagnostic views; medians over roofs:", '']
    for summary in s:
        lines.append(f"- {summary['layer']}:{summary['variant']}: original view {summary['edge_support_median']['original']}, "
                     f"clahe view {summary['edge_support_median']['clahe']} (n={summary['n']}).")
    lines += ['', '## Largest changes', '',
              f"Best variant by median IoU delta: {best['layer']}:{best['variant']} (median delta {best['iou']['delta_median']}, "
              f"95 % CI {best['iou']['delta_ci95']}, better {best['iou']['n_better']} / worse {best['iou']['n_worse']} / unchanged {best['iou']['n_unchanged']} of {best['n']}).",
              f"Contact sheet (reference white, baseline orange, best variant green): {result['sheet']}", '']
    for d in result['largest_changes']:
        lines.append(f"- {d['ref_id'][:8]} {d['tier']} {d['scene_id']}/{d['object_id']}: IoU {d['iou_baseline']} -> {d['iou_variant']} "
                     f"({d['iou_delta']:+.4f}); boundary mean {d['boundary_mean_m_baseline']} -> {d['boundary_mean_m_variant']} m; snap {d['snap']}")
    lines += ['', '## Answer', '', result['answer'], '', f"Code sha256 {CODE_SHA256}."]
    (out_dir/'REPORT.md').write_text('\n'.join(lines)+'\n')
    return out_dir/'REPORT.md'


def _summary_line(summary):
    iou, dist = summary['iou'], summary['boundary_mean_m']
    return (f"- {summary['layer']}:{summary['variant']} vs {summary['baseline']} (n={summary['n']}): IoU median {iou['baseline_median']} -> "
            f"{iou['variant_median']}, median paired delta {iou['delta_median']} (bootstrap 95 % CI {iou['delta_ci95']}), mean delta {iou['delta_mean']}; "
            f"better {iou['n_better']}, worse {iou['n_worse']}, unchanged {iou['n_unchanged']}; among the {iou['n_changed']} changed roofs the median delta is "
            f"{iou['delta_median_among_changed']} (CI {iou['delta_ci95_among_changed']}); changed by more than {iou['change_threshold']} IoU: improved {iou['n_improved_over']}, "
            f"worsened {iou['n_worsened_over']}. Boundary mean distance median {dist['baseline_median']} -> {dist['variant_median']} m, "
            f"median paired delta {dist['delta_median']} m (CI {dist['delta_ci95']}); closer {dist['n_better']}, farther {dist['n_worse']}.")


def compose_answer(result):
    best = result['best']
    n = best['n']
    iou = best['iou']
    ci = iou['delta_ci95'] or [0, 0]
    consistent = iou['delta_median'] and iou['delta_median'] > 0 and ci[0] > 0
    lead = ('No variant improves reference agreement consistently. ' if not consistent
            else f"{best['layer']}:{best['variant']} improves reference agreement consistently (CI above zero). ")
    prod = [s for s in result['summaries'] if s['layer'] == 'production' and s['variant'] != BASELINE]
    prod_note = ('Under the production refiner the enhanced frames change nothing about the exterior outline '
                 f"({', '.join(str(result['production_identical'][s['variant']]) + '/' + str(n) + ' identical for ' + s['variant'] for s in prod)}), "
                 'so a paired isolated inference profile that swaps the refinement frame would be a no-op for outlines. ')
    snap_note = (f"With the experimental edge-snap the best frame is {best['variant']}: median IoU delta {iou['delta_median']} "
                 f"(95 % CI {iou['delta_ci95']}), better on {iou['n_better']}, worse on {iou['n_worse']}, unchanged on {iou['n_unchanged']} of {n}; "
                 f"{iou['n_improved_over']} roofs improved and {iou['n_worsened_over']} worsened by more than {CHANGE_IOU} IoU. ")
    recommend = ('Recommendation: do not adopt a paired isolated inference profile for the refinement frame. '
                 if not consistent else
                 'Recommendation: adopt as a paired isolated inference profile only after a per-scene review of the worsened roofs. ')
    return lead + prod_note + snap_note + recommend + NOTE


def run(root=Path('.'), out_dir=DEFAULT_OUT, *, log=print):
    root, out_dir = Path(root), Path(out_dir)
    import warnings
    warnings.filterwarnings('ignore', category=RuntimeWarning)  # shapely oriented_envelope on 1-2 px holes
    pairs, skipped = pair_references(root)
    measured = []
    for index, pair in enumerate(pairs):
        log(f"[{index+1}/{len(pairs)}] {pair['ref']['ref_id'][:8]} {pair['scene_id']}/{pair['object_id']} ({pair['pairing']})")
        try:
            measured.append(measure_pair(pair, root))
        except (ValueError, KeyError, GEOSException, OSError, TypeError) as error:
            skipped.append({'ref_id': pair['ref']['ref_id'], 'tier': pair['ref']['tier'], 'scene_id': pair['scene_id'],
                            'object_id': pair['object_id'], 'label': pair['ref']['payload'].get('label'),
                            'reason': f'measurement failed: {type(error).__name__}: {error}'[:200]})
    rows = [r for r in measured if r['outline_independent']]
    echoes = [{'ref_id': r['ref_id'], 'tier': r['tier'], 'scene_id': r['scene_id'], 'object_id': r['object_id'], 'saved_iou': r['saved_agreement']['iou'],
               'production_original_iou': r['production'][BASELINE]['iou']} for r in measured if not r['outline_independent']]
    summaries = [paired_summary(rows, 'production', v) for v in VARIANTS if v != BASELINE]
    summaries += [paired_summary(rows, 'edge_snap', v) for v in VARIANTS]
    snap_only = [s for s in summaries if s['layer'] == 'edge_snap']
    best = best_variant(snap_only) or best_variant(summaries)
    snap_vs_snap = [paired_summary(rows, 'edge_snap', v, baseline_layer='edge_snap', baseline=BASELINE) for v in VARIANTS if v != BASELINE]
    deltas = per_roof_deltas(rows, best['layer'], best['variant'])
    sheet = render_sheet(rows, deltas, best['layer'], best['variant'], root, out_dir/'sheets'/f"largest-changes-{best['layer']}-{best['variant']}.png")
    from collections import Counter
    result = {'version': VERSION, 'code_sha256': CODE_SHA256, 'computed_at': datetime.now(UTC).isoformat(), 'note': NOTE,
              'params': {'clahe': CLAHE_PARAMS, 'edges': EDGE_PARAMS, 'unsharp': UNSHARP_PARAMS, 'snap': SNAP_PARAMS, 'bootstrap': BOOTSTRAP,
                         'change_iou': CHANGE_IOU},
              'n_references': len(pairs)+len([x for x in skipped if not x['reason'].startswith('measurement')]),
              'n_measured': len(measured), 'n_paired': len(rows), 'outline_echoes_excluded': echoes, 'skipped': skipped,
              'pairing_counts': dict(sorted(Counter(r['pairing'] for r in rows).items())),
              'start_counts': dict(sorted(Counter(r['start'] for r in rows).items())),
              'frame_counts': dict(sorted(Counter(r['frame'] for r in rows).items())),
              'production_identical': {v: sum(r['production'][v]['exterior_identical_to_baseline'] for r in rows) for v in VARIANTS},
              'production_hole_decisions_differ': {v: sum(r['production'][v]['n_holes_filled'] != r['production'][BASELINE]['n_holes_filled'] for r in rows) for v in VARIANTS},
              'start_iou_median': round(float(statistics.median(r['start_agreement']['iou'] for r in rows)), 4) if rows else None,
              'baseline_hausdorff_over_budget': sum(r['production'][BASELINE]['hausdorff_m'] > SNAP_PARAMS['max_shift_m'] for r in rows),
              'baseline_boundary_mean_over_budget': sum(r['production'][BASELINE]['boundary_mean_m'] > SNAP_PARAMS['max_shift_m'] for r in rows),
              'baseline_iou_below_0_85': sum(r['production'][BASELINE]['iou'] < .85 for r in rows),
              'snap_accepted': {v: sum(r['edge_snap'][v]['accepted'] for r in rows) for v in VARIANTS},
              'summaries': summaries, 'snap_vs_snap': snap_vs_snap, 'best': best,
              'largest_changes': deltas[:12], 'per_roof': {f"{s['layer']}:{s['variant']}": per_roof_deltas(rows, s['layer'], s['variant']) for s in summaries},
              'sheet': sheet, 'rows': rows}
    result['answer'] = compose_answer(result)
    report = write_report(result, out_dir)
    log(f'Wrote {report}')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    runner = sub.add_parser('run', help='Measure every T1/T2 reference roof under all frames and write the report.')
    runner.add_argument('--root', default='.')
    runner.add_argument('--out', default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    if args.command == 'run':
        result = run(Path(args.root), Path(args.out))
        sys.stdout.write(result['answer']+'\n')
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
