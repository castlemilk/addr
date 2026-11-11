"""Corner judge J2 (teacher-corners-v1): do a finer second sampling of the ground and the outline's own pixels put an
outline corner in the same place?

A = the 7.5 cm LINZ Christchurch teacher outlines (linz-chc-2025-0075); B = the 5 cm capture of the same cells
(linz-chc-2025-005, cut at its native 5436 px). Each qualifying corner of a raw A outline is located as an IMAGE
corner twice, by intersecting straight lines fitted to the gradient-profile peaks along its two edges:

  C_B  on B's 5 cm pixels, at the image-registered per-building shift (`register --other linz-chc-2025-005`)
  C_A  on A's own 7.5 cm pixels

repeat_m = |C_B - C_A| says whether the corner is located repeatably at the 0.1 m scale; the signed apex gap of every
LADDER variant (raw, offsets, roundings) to C_B describes where the outlines sit (descriptive only); the same run
with the registered shift moved by 1 m is the null control. Pass or fail comes only from the pre-registered
judges/corners_rules.json (written before any 5 cm run; its sha256 is embedded in every output). Every figure is
agreement between two samplings of the ground, never accuracy; nothing here is promoted and no label changes.

Coordinates: metres from the window's top-left corner (x east, y down) on A's frame; pixel c covers [c, c + 1) x gsd,
so a position in metres is at array coordinate m / gsd - 0.5 (the outline vectoriser puts pixel edges on integers).

  task eyes:teacher-judges CLI_ARGS='corners --source linz-chc-2025-0075 --other linz-chc-2025-005 --cells all'
  task eyes:teacher-judges CLI_ARGS='corners --source linz-chc-2025-0075 --other linz-chc-2025-005 --cells all --null-shift-m 1.0'
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np

from . import common
from . import hires_teacher as ht
from . import teacher_judges as tj
from . import teacher_sources as ts

VERSION = 'teacher-corners-v1'
RULES_NAME = 'corners_rules.json'
J1_PEAK_TOL_AT_IMPORT = float(tj.J1['peak_tol_m'])   # boundary() mutates J1 in-process; this judge refuses to run after it

CORNERS = {
    # corner filter on the raw A outline
    'simplify_m': .25, 'turn_deg': [65., 115.], 'min_edge_m': 1.2,
    # edge samples every step_m from from_m to min(to_m, edge length - end_clear_m) from the vertex: the fit never sees
    # the corner itself
    'from_m': .4, 'to_m': 2., 'step_m': .1, 'end_clear_m': .4, 'min_samples': 6,
    # Known v1 inconsistency, kept because corners_rules.json v1 pre-registered it: an edge shorter than
    # from_m + end_clear_m + (min_samples - 1) * step_m = 1.3 m passes min_edge_m 1.2 but holds only 5 samples, so its
    # corner can never be resolved (rows with min(edge_m) < 1.3 in corners_<other>_<cells>_rows.jsonl). Rules v2 (a new
    # registry entry): set min_edge_m 1.3 or min_samples 5 so the filter and the fit agree.
    # J1's peak rules along the edge normal (profile, gain, angle, vegetation, parabolic sub-sample peak) with this
    # judge's own tolerance, never J1['peak_tol_m']
    'profile_m': .45, 'profile_step_m': .025, 'peak_tol_m': .20, 'gain': 3., 'angle_tol_deg': 20., 'buffer_m': 2.,
    'exg_veg': .06,
    # robust line fit (least squares + one MAD pass) and intersection
    'min_inliers': 5, 'max_rms_m': .05, 'mad_k': 3., 'mad_floor_m': .005, 'min_angle_deg': 45.,
    # statistics
    'resolved_m': .10, 'apex_search_m': 1., 'null_direction': [1., 1.], 'draws': 2000, 'seed': 20260928,
}

GAP_DEFINITION = ("g_V = (+1 if V contains C_B else -1) x the distance from C_B to V's exterior measured along the "
                  "outline corner's bisector (the apex direction), so an apex offset by d sits d sqrt(2) out (offset:0.075 "
                  "-> +0.106 m at a right-angle corner) and a rounding of radius r moves the apex r (sqrt(2) - 1) in "
                  "(round:0.15 -> -0.062 m); None when the bisector line meets no boundary within apex_search_m")

RULES_TEMPLATE = {
    'version': 'teacher-corners-rules-v1',
    'judge': 'corners (J2, teacher_corners.py)',
    'pre_registration': ('written before any 5 cm corner judge run; thresholds are never tuned after seeing output; '
                         'amendments are appended to amendments[] with a timestamp and a reason and reported post hoc'),
    'sources': {'A': 'linz-chc-2025-0075 outlines (7.5 cm teacher instances)',
                'B': 'linz-chc-2025-005 pixels (5 cm, 5436 px windows, 4 shared cells: r020-c044 and r021-c047 judge, '
                     'r021-c033 and r026-c047 train; no training is involved)'},
    'population': ('teacher_judges.population(): outline-matched A instances >= 20 m2, >= 3 m inside the window, NCC ok '
                   'against B (register rows); --cells all'),
    'definitions': {
        'corner': 'vertex of the 0.25 m-simplified raw A outline with |turn| in [65, 115] deg and both adjacent edges >= 1.2 m',
        'image_corner': ('per edge: J1 peak of the gradient-magnitude profile along the edge normal (profile 0.45 m, step '
                         '0.025 m, gain 3 over the building-buffer median, gradient within 20 deg of the normal, ExG > 0.06 '
                         'excluded, parabolic sub-sample peak) accepted within settings.peak_tol_m of the outline edge, '
                         'sampled every 0.1 m from 0.4 m to min(2.0 m, edge - 0.4 m) (>= 6 samples); least-squares line of '
                         'offset against distance, one MAD pass (3 x 1.4826 MAD), >= 5 inliers, RMS <= 0.05 m; the two lines '
                         'intersected unless they meet under 45 deg'),
        'C_B': "image corner on B's 5 cm pixels at the registered per-building shift, expressed in A's frame",
        'C_A': "image corner on A's own 7.5 cm pixels",
        'repeat_m': '|C_B - C_A| in metres',
        'resolved_candidate': 'C_A and C_B both exist (all four lines fit and both intersections exist)',
        'resolved': 'resolved candidate with repeat_m <= resolved_m (0.10 m)',
        'resolved_share': 'resolved corners / all corners passing the corner filter (a corner whose edges cannot hold 6 samples counts as unresolved)',
        'R1_MAD': 'median absolute difference of the paired positions: median over resolved candidates of |C_B - C_A|',
        'R3_MAD': 'median absolute deviation of g_raw about its median over resolved corners (unscaled)',
        'gap': GAP_DEFINITION,
        'null': ("B is sampled at the registered shift plus null_shift_m along null_direction (1, 1)/sqrt 2; C_B is expressed "
                 "in A's frame by removing the same total shift, so a corner is resolved under the null only by chance"),
        'intervals': ('building-cluster bootstrap (unit = (window, index), 2000 draws, seed 20260928, 2.5 / 97.5 percentiles) '
                      'binding; window bootstrap (n = 4) reported only'),
    },
    'rules': {
        'R0': {'text': 'register sanity: global_shift_m median <= 0.3 and ncc_ok_share >= 0.5, else stop (no corner table)',
               'global_shift_m_median_max': .3, 'ncc_ok_share_min': .5},
        'R1': {'text': 'repeatability: median over resolved candidates of |C_B - C_A| <= 0.05 m (building-cluster interval of the '
                       'median reported, not binding)',
               'repeat_m_median_max': .05},
        'R2': {'text': 'resolution: resolved share at 0.10 m minus the resolved share under the 1 m null shift >= 0.5',
               'resolved_m': .10, 'resolved_minus_null_min': .5, 'null_shift_m': 1.},
        'R3': {'text': 'spread: half-width of the building-cluster 95 percent interval of the mean raw gap <= 0.03 m and '
                       'MAD(g_raw) <= 0.15 m',
               'raw_gap_ci_half_width_max': .03, 'raw_gap_mad_max': .15},
        'R4': {'text': 'null control: resolved share under --null-shift-m 1.0 < 0.20', 'null_shift_m': 1.,
               'null_resolved_share_below': .20},
        'R5': {'text': ("photons: register ncc_same_grid_median >= 0.95 -> the report calls this a finer-sampling judge of one "
                        "sortie (repeatability then measures sampling noise only), never cross-capture stability"),
               'ncc_same_grid_median_min': .95, 'wording_if_pass': 'finer-sampling judge of one sortie',
               'wording_if_fail': ('second sampling whose photons are not shown to be shared (same-grid NCC median below 0.95; '
                                   'STAC dates overlap: 5 cm 2025-03-05..03-14, 7.5 cm 2025-03-14..15); repeatability mixes '
                                   'sampling noise with capture differences')},
    },
    'descriptive_only': ['ladder means and paired deltas against raw (the ordering holds for any C; round:0.15 - raw = -0.062 '
                         'm whenever C lies on the corner bisector, so they count corners, not resolution)',
                         'window bootstrap (n = 4)',
                         'J1 ladders at 0.10 and 0.05 m: recorded negative controls (pre-registered expectation: round:0.15 '
                         'does not separate from raw at 0.10; apex displacement 0.062 m)'],
    'if_R1_or_R2_fails': ('the 5 cm judge does not resolve 0.1 m corners with this population; item 3 of the ladder stays '
                          'deferred; growth path = a NEW registry entry (never re-select linz-chc-2025-0075)'),
    'wording': 'agreement between two samplings of the ground (or two models), never accuracy; nothing is promoted',
    'amendments': [],
}


def _num(value, digits=4):
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


def _jsonable(value):
    return json.loads(json.dumps(value))


def _mad(values):
    values = np.asarray([v for v in values if v is not None], float)
    return _num(np.median(np.abs(values - np.median(values)))) if values.size else None


def _xy(point, digits=4):
    return None if point is None else [_num(point[0], digits), _num(point[1], digits)]


def rules_document(settings=None):
    """The pre-registered rules with the judge's settings embedded (written once by write_rules)."""
    document = copy.deepcopy(RULES_TEMPLATE)
    document['settings'] = _jsonable(settings or CORNERS)
    return document


def write_rules(path, settings=None):
    """Write corners_rules.json once; an existing file is never overwritten (amendments carry a timestamp and a reason)."""
    path = Path(path)
    if path.exists():
        raise SystemExit(f'{path} exists: the rules are pre-registered; amend them in amendments[] (timestamp + reason), '
                         'never rewrite')
    common.write_json_atomic(path, {**rules_document(settings), 'written_at': common.now_iso()})
    return path


# --- corner geometry ----------------------------------------------------------------------------------------------

def capture(rgb):
    """What the locator reads from one capture: RGB (vegetation test) and Sobel gradients of its grey."""
    rgb = np.asarray(rgb, dtype=np.uint8)
    gx, gy, mag = tj.sobel(tj.gray_f32(rgb))
    return {'rgb': rgb, 'gx': gx, 'gy': gy, 'mag': mag}


def edge_samples(length_m, settings=None):
    """Distances (m) from the vertex at which an edge of this length is sampled; empty when fewer than min_samples."""
    s = settings or CORNERS
    stop = min(s['to_m'], length_m - s['end_clear_m'])
    count = int(math.floor((stop - s['from_m']) / s['step_m'] + 1e-9)) + 1 if stop >= s['from_m'] - 1e-9 else 0
    d = s['from_m'] + s['step_m'] * np.arange(max(count, 0))
    return d if len(d) >= s['min_samples'] else np.zeros(0)


def outline_corners(polygon, gsd, settings=None):
    """Qualifying corners of a raw outline (A pixels): simplified, |turn| in turn_deg, both adjacent edges >= min_edge_m.

    Each corner carries its vertex (metres), unit vectors u along both edges away from the vertex, their outward
    normals n, the edge lengths and the sample distances along each edge."""
    from shapely.geometry.polygon import orient
    s = settings or CORNERS
    simple = polygon.simplify(s['simplify_m'] / gsd, preserve_topology=True)
    if simple.geom_type != 'Polygon' or simple.is_empty:
        return []
    ring = orient(simple, sign=1.)
    pts = np.asarray(ring.exterior.coords, float)[:-1]
    if len(pts) < 3:
        return []
    turns = ht.turning_angles(ring)
    x, y = pts[:, 0], pts[:, 1]
    orientation = 1. if (x * np.roll(y, -1) - np.roll(x, -1) * y).sum() > 0 else -1.
    lo, hi = s['turn_deg']
    corners = []
    for i in range(len(pts)):
        v, p, q = pts[i], pts[i - 1], pts[(i + 1) % len(pts)]
        e1, e2 = p - v, q - v
        l1, l2 = float(np.hypot(*e1)) * gsd, float(np.hypot(*e2)) * gsd
        if not lo <= turns[i] <= hi or min(l1, l2) < s['min_edge_m']:
            continue
        u1, u2 = e1 / np.hypot(*e1), e2 / np.hypot(*e2)
        # a positively oriented ring has the outward normal (dy, -dx) on edge a -> b: edge p -> v runs along -u1
        n1, n2 = orientation * np.array([-u1[1], u1[0]]), orientation * np.array([u2[1], -u2[0]])
        cross = (v - p)[0] * (q - v)[1] - (v - p)[1] * (q - v)[0]
        corners.append({'corner_index': int(i), 'vertex_m': v * gsd, 'u': (u1, u2), 'n': (n1, n2), 'edge_m': (l1, l2),
                        'turn_deg': float(turns[i]), 'convex': bool(cross * orientation > 0),
                        'd': (edge_samples(l1, s), edge_samples(l2, s))})
    return corners


def building_reference(cap, bounds_m, shift_m, gsd, settings=None):
    """J1's local reference gradient: median magnitude over the building's bbox + buffer_m on this capture's grid."""
    s = settings or CORNERS
    mag = cap['mag']
    h, w = mag.shape
    minx, miny, maxx, maxy = bounds_m
    pad = s['buffer_m']
    x0, y0 = int(max(0, math.floor((minx + shift_m[0] - pad) / gsd))), int(max(0, math.floor((miny + shift_m[1] - pad) / gsd)))
    x1, y1 = int(min(w, math.ceil((maxx + shift_m[0] + pad) / gsd) + 1)), int(min(h, math.ceil((maxy + shift_m[1] + pad) / gsd) + 1))
    return float(np.median(mag[y0:y1, x0:x1])) if x1 > x0 and y1 > y0 else float(np.median(mag))


def edge_peaks(cap, gsd, origin_m, u, n, d, reference, settings=None):
    """J1 peak of the gradient-magnitude profile along the edge normal n at distances d (m) from origin_m along u.

    origin_m is the corner vertex in this capture's frame. Returns (t, ok): the parabolic sub-sample peak offset along
    n (metres) and whether the peak passes J1's rules with this judge's own tolerance (CORNERS['peak_tol_m'])."""
    from scipy import ndimage
    s = settings or CORNERS
    mag, gx, gy, rgb = cap['mag'], cap['gx'], cap['gy'], cap['rgb']
    h, w = mag.shape
    d = np.asarray(d, float)
    offsets = np.arange(-s['profile_m'], s['profile_m'] + 1e-9, s['profile_step_m'])
    sx, sy = origin_m[0] + u[0] * d, origin_m[1] + u[1] * d
    xs = (sx[:, None] + n[0] * offsets[None, :]) / gsd - .5
    ys = (sy[:, None] + n[1] * offsets[None, :]) / gsd - .5
    inside = (xs >= 0) & (xs <= w - 1) & (ys >= 0) & (ys <= h - 1)
    profiles = ndimage.map_coordinates(mag, [ys.ravel(), xs.ravel()], order=1, mode='nearest').reshape(xs.shape)
    profiles[~inside] = 0.
    k = np.arange(len(d))
    peak = profiles.argmax(axis=1)
    peak_t = offsets[peak]
    peak_mag = profiles[k, peak]
    # parabolic sub-pixel peak through the magnitude one PIXEL either side of the profile peak along the normal: the
    # bilinear profile is flat between two pixel centres, so a parabola over profile steps would stay biased by up
    # to half a step; at pixel pitch a symmetric edge sampled at phase f returns exactly 0.5 - f
    around = []
    for step in (-gsd, 0., gsd):
        ax = (sx + n[0] * (peak_t + step)) / gsd - .5
        ay = (sy + n[1] * (peak_t + step)) / gsd - .5
        around.append(ndimage.map_coordinates(mag, [ay, ax], order=1, mode='nearest'))
    left, centre, right = around
    curvature = left - 2. * centre + right
    safe = np.where(np.abs(curvature) > 1e-9, curvature, 1.)
    refine = np.clip(np.where(np.abs(curvature) > 1e-9, .5 * (left - right) / safe, 0.), -.5, .5) * gsd
    px = np.clip(np.rint(xs[k, peak]).astype(int), 0, w - 1)
    py = np.clip(np.rint(ys[k, peak]).astype(int), 0, h - 1)
    grad_x, grad_y = gx[py, px], gy[py, px]
    cosine = np.abs(grad_x * n[0] + grad_y * n[1]) / (np.hypot(grad_x, grad_y) + 1e-9)
    cx = np.clip(np.rint(sx / gsd - .5).astype(int), 0, w - 1)
    cy = np.clip(np.rint(sy / gsd - .5).astype(int), 0, h - 1)
    rgb_f = rgb[cy, cx].astype(np.float32)
    exg = (2 * rgb_f[:, 1] - rgb_f[:, 0] - rgb_f[:, 2]) / (rgb_f.sum(axis=1) + 1.)
    ok = ((np.abs(peak_t) <= s['peak_tol_m'] + 1e-9) & (peak_mag >= s['gain'] * max(reference, 1e-6))
          & (cosine >= math.cos(math.radians(s['angle_tol_deg']))) & (exg <= s['exg_veg']))
    return peak_t + refine, ok


def _fit(d, t, settings=None):
    s = settings or CORNERS
    d, t = np.asarray(d, float), np.asarray(t, float)
    if len(d) < s['min_inliers']:
        return None, 'too_few_peaks'
    b, a = np.polyfit(d, t, 1)
    residual = t - (a + b * d)
    centre = np.median(residual)
    spread = np.median(np.abs(residual - centre))
    keep = np.abs(residual - centre) <= max(s['mad_k'] * 1.4826 * spread, s['mad_floor_m'])
    if keep.sum() < s['min_inliers']:
        return None, 'too_few_inliers'
    b, a = np.polyfit(d[keep], t[keep], 1)
    residual = t[keep] - (a + b * d[keep])
    rms = float(np.sqrt(np.mean(residual * residual)))
    if rms > s['max_rms_m']:
        return None, 'rms_above_max'
    return {'a': float(a), 'b': float(b), 'inliers': int(keep.sum()), 'peaks': int(len(d)), 'rms_m': rms}, 'ok'


def fit_edge_line(d, t, settings=None):
    """Line t = a + b d of peak offset t (m, along the outward normal) against distance d (m) from the vertex: least
    squares, one MAD pass, refit; None when fewer than min_inliers remain or the RMS exceeds max_rms_m."""
    return _fit(d, t, settings)[0]


def intersect(corner, lines, settings=None):
    """Intersection (A's frame, metres) of the two fitted edge lines, and the acute angle between them (degrees)."""
    s = settings or CORNERS
    v = np.asarray(corner['vertex_m'], float)
    (u1, u2), (n1, n2) = corner['u'], corner['n']
    p1, p2 = v + n1 * lines[0]['a'], v + n2 * lines[1]['a']
    d1, d2 = u1 + n1 * lines[0]['b'], u2 + n2 * lines[1]['b']
    d1, d2 = d1 / np.hypot(*d1), d2 / np.hypot(*d2)
    angle = math.degrees(math.asin(min(1., abs(d1[0] * d2[1] - d1[1] * d2[0]))))
    if angle < s['min_angle_deg']:
        return None, angle
    lam, _ = np.linalg.solve(np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]]), p2 - p1)
    return p1 + lam * d1, angle


def image_corner(cap, gsd, corner, reference, shift_m, settings=None):
    """The image corner of one outline corner on one capture (A's frame, metres).

    The capture is sampled at the outline geometry moved by shift_m (the registered shift for B, zero for A); the
    fitted lines are parameterised on the outline edges, so the intersection is already in A's frame."""
    s = settings or CORNERS
    origin = (float(corner['vertex_m'][0] + shift_m[0]), float(corner['vertex_m'][1] + shift_m[1]))
    lines, reasons = [], []
    for u, n, d in zip(corner['u'], corner['n'], corner['d'], strict=True):
        if len(d) < s['min_samples']:
            lines.append(None)
            reasons.append('too_few_samples')
            continue
        t, ok = edge_peaks(cap, gsd, origin, u, n, d, reference, s)
        fit, reason = _fit(d[ok], t[ok], s)
        lines.append(fit)
        reasons.append(reason)
    rms = [_num(line['rms_m']) if line else None for line in lines]
    failed = [r for r in reasons if r != 'ok']
    if failed:
        # v1 reports the first failing edge only (a second edge's too_few_samples hides behind the first edge's
        # too_few_peaks); v2: report every edge's reason so status_A / status_B count every cause
        return {'status': failed[0], 'C': None, 'lines': lines, 'rms_m': rms, 'angle_deg': None}
    point, angle = intersect(corner, lines, s)
    if point is None:
        return {'status': 'lines_under_min_angle', 'C': None, 'lines': lines, 'rms_m': rms, 'angle_deg': _num(angle, 2)}
    return {'status': 'ok', 'C': point, 'lines': lines, 'rms_m': rms, 'angle_deg': _num(angle, 2)}


def variant_polygons_m(polygon, gsd):
    """The LADDER variants of one A outline (A pixels, built by teacher_judges._variant_polygons) in metres."""
    from shapely import affinity
    return {name: (affinity.scale(poly, xfact=gsd, yfact=gsd, origin=(0, 0)) if poly is not None else None)
            for name, poly in tj._variant_polygons(polygon, gsd, list(tj.LADDER)).items()}


def _points(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'Point':
        return [(geometry.x, geometry.y)]
    if geometry.geom_type in ('LineString', 'LinearRing'):
        return list(geometry.coords)
    return [xy for part in geometry.geoms for xy in _points(part)]


def apex_gap(point, polygon_m, bisector, search_m):
    """Signed distance from point to polygon_m's exterior along the bisector line (+ inside, - outside)."""
    from shapely.geometry import LineString, Point
    if point is None or polygon_m is None:
        return None
    p = np.asarray(point, float)
    line = LineString([tuple(p - bisector * search_m), tuple(p + bisector * search_m)])
    hits = _points(line.intersection(polygon_m.exterior))
    if not hits:
        return None
    distance = min(math.hypot(x - p[0], y - p[1]) for x, y in hits)
    return float(distance if polygon_m.contains(Point(p)) else -distance)


def corner_gaps(point, corner, variants, settings=None):
    """Signed apex gap g_V of every variant polygon (metres) to one image corner; None values when point is None."""
    s = settings or CORNERS
    u1, u2 = corner['u']
    bisector = (u1 + u2) / np.hypot(*(u1 + u2))
    return {name: _num(apex_gap(point, poly, bisector, s['apex_search_m'])) for name, poly in variants.items()}


# --- statistics and rules ------------------------------------------------------------------------------------------

def _cluster_draws(keys, draws, seed):
    """Row-index arrays of `draws` cluster resamples (clusters = unique keys, drawn with replacement); None if < 2."""
    order = sorted(set(keys))
    if len(order) < 2:
        return None
    members = {k: [] for k in order}
    for i, key in enumerate(keys):
        members[key].append(i)
    groups = [np.asarray(members[k], dtype=np.int64) for k in order]
    rng = np.random.default_rng(seed)
    return [np.concatenate([groups[j] for j in rng.integers(0, len(groups), len(groups))]) for _ in range(draws)]


def _interval(draws, statistic):
    if not draws:
        return None
    values = [statistic(idx) for idx in draws]
    values = np.array([v for v in values if v is not None and np.isfinite(v)], float)
    return [_num(np.percentile(values, 2.5)), _num(np.percentile(values, 97.5))] if values.size else None


def _nanmedian(values):
    values = values[np.isfinite(values)]
    return float(np.median(values)) if values.size else None


def _nanmean(values):
    values = values[np.isfinite(values)]
    return float(values.mean()) if values.size else None


def _arrays(corners, m):
    """Per-corner arrays of one pass, aligned with `corners`."""
    resolved = np.array([c['passes'][m]['resolved'] for c in corners], bool)
    repeat = np.array([np.nan if c['passes'][m]['repeat_m'] is None else c['passes'][m]['repeat_m'] for c in corners], float)
    gaps = {}
    for name in tj.LADDER:
        gaps[name] = np.array([c['passes'][m]['gaps'][name] if c['passes'][m]['resolved'] and c['passes'][m]['gaps'][name] is not None
                               else np.nan for c in corners], float)
    return resolved, repeat, gaps


def summarise_pass(corners, m, building_draws, window_draws):
    """The corner table of one pass (null shift m): counts, repeatability, resolved share, ladder gaps (descriptive)."""
    n = len(corners)
    resolved, repeat, gaps = _arrays(corners, m)
    status_a, status_b = {}, {}
    for c in corners:
        status_a[c['status_A']] = status_a.get(c['status_A'], 0) + 1
        status_b[c['passes'][m]['status_B']] = status_b.get(c['passes'][m]['status_B'], 0) + 1
    finite_repeat = [float(v) for v in repeat[np.isfinite(repeat)]]
    ladder, paired = {}, {}
    for name in tj.LADDER:
        values = gaps[name]
        finite = values[np.isfinite(values)]
        ladder[name] = {'n': int(finite.size), 'mean': _num(finite.mean()) if finite.size else None,
                        'median': _num(np.median(finite)) if finite.size else None, 'mad': _mad(finite.tolist()),
                        'mean_ci95_building': _interval(building_draws, lambda idx, v=values: _nanmean(v[idx]))}
        if name != 'raw':
            both = np.isfinite(values) & np.isfinite(gaps['raw'])
            delta = values[both] - gaps['raw'][both]
            paired[name] = {'n': int(both.sum()), 'mean_delta': _num(delta.mean()) if delta.size else None,
                            'median_delta': _num(np.median(delta)) if delta.size else None}
    convex = np.array([c['convex'] for c in corners], bool)
    return {
        'null_shift_m': m, 'corners': n, 'buildings': len({(c['window'], c['index']) for c in corners}),
        'windows': len({c['window'] for c in corners}), 'convex_corners': int(convex.sum()),
        'fitted_A': sum(c['C_A'] is not None for c in corners), 'fitted_B': sum(c['passes'][m]['C_B'] is not None for c in corners),
        'resolved_candidates': int(np.isfinite(repeat).sum()), 'resolved': int(resolved.sum()),
        'resolved_share': _num(resolved.mean()) if n else None,
        'resolved_share_ci95_building': _interval(building_draws, lambda idx: float(resolved[idx].mean())),
        'resolved_share_ci95_window': _interval(window_draws, lambda idx: float(resolved[idx].mean())),
        'resolved_share_convex': _num(resolved[convex].mean()) if convex.any() else None,
        'resolved_share_reflex': _num(resolved[~convex].mean()) if (~convex).any() else None,
        'status_A': status_a, 'status_B': status_b,
        'repeat_m': {**tj._spread(finite_repeat), 'mad': _mad(finite_repeat)},
        'repeat_m_median_ci95_building': _interval(building_draws, lambda idx: _nanmedian(repeat[idx])),
        'repeat_m_median_ci95_window': _interval(window_draws, lambda idx: _nanmedian(repeat[idx])),
        'ladder': ladder, 'paired_vs_raw': paired,
    }


def rules_inputs(register_summary, main, null, corners, null_m, building_draws):
    """The flat numbers evaluate_rules reads: register figures, the unshifted pass and the null pass."""
    resolved_main, _, _ = _arrays(corners, 0.)
    resolved_null, _, _ = _arrays(corners, null_m)
    shift = (register_summary.get('global_shift_m') or {}).get('median')
    return {'register_global_shift_m_median': shift, 'register_ncc_ok_share': register_summary.get('ncc_ok_share'),
            'register_ncc_same_grid_median': register_summary.get('ncc_same_grid_median'),
            'repeat_m_median': main['repeat_m']['median'], 'repeat_m_median_ci95_building': main['repeat_m_median_ci95_building'],
            'resolved_share': main['resolved_share'], 'null_resolved_share': null['resolved_share'],
            'resolved_minus_null_ci95_building': _interval(building_draws, lambda idx: float(resolved_main[idx].mean()
                                                                                             - resolved_null[idx].mean())),
            'raw_gap_mean_ci95_building': main['ladder']['raw']['mean_ci95_building'], 'raw_gap_mad_m': main['ladder']['raw']['mad'],
            'corners': main['corners'], 'resolved_candidates': main['resolved_candidates'], 'null_shift_m': null_m}


def evaluate_rules(summary, rules):
    """Mechanical pass / fail of R0-R5 from the flat inputs and the pre-registered thresholds (a missing input fails)."""
    r = rules['rules']
    get = summary.get

    def verdict(ok):
        return 'pass' if ok else 'fail'

    shift, share = get('register_global_shift_m_median'), get('register_ncc_ok_share')
    out = {'R0': {'result': verdict(shift is not None and share is not None and shift <= r['R0']['global_shift_m_median_max']
                                    and share >= r['R0']['ncc_ok_share_min']),
                  'inputs': {'global_shift_m_median': shift, 'ncc_ok_share': share},
                  'threshold': {k: r['R0'][k] for k in ('global_shift_m_median_max', 'ncc_ok_share_min')}}}
    repeat = get('repeat_m_median')
    out['R1'] = {'result': verdict(repeat is not None and repeat <= r['R1']['repeat_m_median_max']),
                 'inputs': {'repeat_m_median': repeat, 'repeat_m_median_ci95_building': get('repeat_m_median_ci95_building')},
                 'threshold': {'repeat_m_median_max': r['R1']['repeat_m_median_max']}}
    resolved, null = get('resolved_share'), get('null_resolved_share')
    difference = _num(resolved - null) if resolved is not None and null is not None else None
    out['R2'] = {'result': verdict(difference is not None and difference >= r['R2']['resolved_minus_null_min']),
                 'inputs': {'resolved_share': resolved, 'null_resolved_share': null, 'resolved_minus_null': difference},
                 'threshold': {k: r['R2'][k] for k in ('resolved_m', 'resolved_minus_null_min', 'null_shift_m')}}
    if get('resolved_minus_null_ci95_building') is not None:
        out['R2']['reported'] = {'resolved_minus_null_ci95_building': get('resolved_minus_null_ci95_building')}
    interval, mad = get('raw_gap_mean_ci95_building'), get('raw_gap_mad_m')
    half = _num((interval[1] - interval[0]) / 2.) if interval and None not in interval else None
    out['R3'] = {'result': verdict(half is not None and mad is not None and half <= r['R3']['raw_gap_ci_half_width_max']
                                   and mad <= r['R3']['raw_gap_mad_max']),
                 'inputs': {'raw_gap_mean_ci95_building': interval, 'ci_half_width_m': half, 'raw_gap_mad_m': mad},
                 'threshold': {k: r['R3'][k] for k in ('raw_gap_ci_half_width_max', 'raw_gap_mad_max')}}
    out['R4'] = {'result': verdict(null is not None and null < r['R4']['null_resolved_share_below']),
                 'inputs': {'null_resolved_share': null},
                 'threshold': {k: r['R4'][k] for k in ('null_shift_m', 'null_resolved_share_below')}}
    ncc = get('register_ncc_same_grid_median')
    shared = ncc is not None and ncc >= r['R5']['ncc_same_grid_median_min']
    out['R5'] = {'result': verdict(shared), 'inputs': {'ncc_same_grid_median': ncc},
                 'threshold': {'ncc_same_grid_median_min': r['R5']['ncc_same_grid_median_min']},
                 'wording': r['R5']['wording_if_pass'] if shared else r['R5']['wording_if_fail']}
    return out


def null_vector(m, settings=None):
    s = settings or CORNERS
    direction = np.asarray(s['null_direction'], float)
    return tuple(float(v) for v in direction / np.hypot(*direction) * m)


def _suffix(m):
    return '' if not m else f'_null{m:g}m'


def _row(c, m):
    p = c['passes'][m]
    return {'window': c['window'], 'index': c['index'], 'corner_index': c['corner_index'], 'area_m2': c['area_m2'],
            'convex': c['convex'], 'turn_deg': c['turn_deg'], 'edge_m': c['edge_m'], 'vertex_m': c['vertex_m'],
            'C_A': _xy(c['C_A']), 'C_B': _xy(p['C_B']), 'repeat_m': _num(p['repeat_m']), 'resolved': p['resolved'],
            'rms_m': {'A': c['rms_A'], 'B': p['rms_B']}, 'status': {'A': c['status_A'], 'B': p['status_B']},
            'gaps': p['gaps'], 'gaps_A': c['gaps_A'], 'null_shift_m': m}


def corners_judge(root, source_id, other_id, cells='all', limit=None, null_shift_m=0., settings=None):
    """J2 over the population's corners; writes corners_<other>_<cells>[_null<m>m].json and _rows.jsonl.

    Every run computes the unshifted pass and the pre-registered null pass (plus its own --null-shift-m pass), so the
    rules are evaluated identically from either file; the file's summary and rows are the run's own pass."""
    if float(tj.J1['peak_tol_m']) != J1_PEAK_TOL_AT_IMPORT:
        raise AssertionError(f"teacher_judges.J1['peak_tol_m'] is {tj.J1['peak_tol_m']}, not its import-time default "
                             f'{J1_PEAK_TOL_AT_IMPORT}: a boundary / ladder call mutated it in this process; run the corner '
                             "judge in its own process (it reads CORNERS['peak_tol_m'], never J1)")
    s = settings or CORNERS
    root = Path(root)
    spec = ts.resolved(ts.source(source_id), root)
    folder = tj.judges_dir(root, spec)
    rules_path = folder / RULES_NAME
    if not rules_path.exists():
        raise SystemExit(f'{rules_path} is missing: pre-register corners_rules.json (teacher_corners.write_rules) before any '
                         'corner judge run')
    rules = common.read_json(rules_path)
    rules_sha = common.sha256_file(rules_path)
    if rules.get('settings') != _jsonable(s):
        raise SystemExit('CORNERS differs from the settings pre-registered in corners_rules.json: amend the rules file '
                         '(timestamp + reason), never tune silently')
    root, spec, other, crops, other_crops, pairs = tj.twin_records(root, source_id, other_id)
    gsd_a, size_a, gsd_b = crops['gsd_m'], crops['native_window_px'], other_crops['gsd_m']
    reg_rows_path, reg_meta_path = folder / f'register_{other["id"]}_rows.jsonl', folder / f'register_{other["id"]}.json'
    if not reg_rows_path.exists() or not reg_meta_path.exists():
        raise SystemExit(f'run `register --other {other["id"]}` first')
    register_summary = common.read_json(reg_meta_path)['summary']
    registration = {(r['window'], r['index']): r for r in tj._read_rows(reg_rows_path)}
    null_m, primary = float(rules['rules']['R4']['null_shift_m']), float(null_shift_m)
    passes = sorted({0., null_m, primary})
    payload = {'version': VERSION, 'judge': 'corners', 'source': spec['id'], 'other': other['id'], 'cells': cells,
               'null_shift_m': primary, 'null_vector_m': list(null_vector(primary, s)), 'passes_computed': passes,
               'rules_file': str(rules_path.relative_to(root)) if rules_path.is_relative_to(root) else str(rules_path),
               'rules_sha256': rules_sha, 'rules_version': rules.get('version'), 'rules_written_at': rules.get('written_at'),
               'amendments': len(rules.get('amendments', [])), 'settings': _jsonable(s),
               'grids': {'A_gsd_m': gsd_a, 'B_gsd_m': gsd_b, 'scale': _num(gsd_a / gsd_b, 6)},
               'register': {k: register_summary.get(k) for k in ('windows', 'buildings', 'ncc_ok_share', 'global_shift_m',
                                                                  'other_gsd_m', 'scale', 'ncc_same_grid_median')},
               'gap_definition': GAP_DEFINITION,
               'note': ('agreement between two samplings of the same ground (the 7.5 cm outline pixels and the 5 cm pixels), '
                        'never accuracy; ladder gaps are descriptive; nothing is promoted and no label changes')}
    r0 = evaluate_rules({'register_global_shift_m_median': (register_summary.get('global_shift_m') or {}).get('median'),
                         'register_ncc_ok_share': register_summary.get('ncc_ok_share'),
                         'register_ncc_same_grid_median': register_summary.get('ncc_same_grid_median')}, rules)
    name = f'corners_{other["id"]}_{cells}{_suffix(primary)}'
    if r0['R0']['result'] != 'pass':
        rules_out = {'R0': r0['R0'], **{k: {'result': 'not_evaluated', 'reason': 'R0 stop'} for k in ('R1', 'R2', 'R3', 'R4')},
                     'R5': r0['R5']}
        payload.update({'computed_at': common.now_iso(), 'stopped': 'R0: registration not trustworthy; no corner table',
                        'rules': rules_out, 'wording': r0['R5']['wording']})
        common.write_json_atomic(folder / f'{name}.json', payload)
        print(json.dumps({'rules': {k: v['result'] for k, v in rules_out.items()}}))
        return {k: payload[k] for k in ('version', 'judge', 'source', 'other', 'cells', 'null_shift_m', 'stopped', 'rules')}
    split = tj.cell_split(root, source_id)
    corners, per_window = [], []
    for record, twin in pairs:
        wid = record['window']['id']
        if cells != 'all' and split.get(wid) != cells:
            continue
        cap_a, cap_b = capture(tj.load_native(root, record)), capture(tj.load_native(root, twin))
        members = tj.population(root, spec, record, registration, gsd_a, size_a)
        scored = members[:limit or len(members)]
        count = 0
        for row, polygon, shift_px in scored:
            variants = variant_polygons_m(polygon, gsd_a)
            bounds_m = tuple(v * gsd_a for v in polygon.bounds)
            reference_a = building_reference(cap_a, bounds_m, (0., 0.), gsd_a, s)
            shifts = {m: (shift_px[0] * gsd_a + null_vector(m, s)[0], shift_px[1] * gsd_a + null_vector(m, s)[1]) for m in passes}
            references_b = {m: building_reference(cap_b, bounds_m, shifts[m], gsd_b, s) for m in passes}
            for corner in outline_corners(polygon, gsd_a, s):
                located_a = image_corner(cap_a, gsd_a, corner, reference_a, (0., 0.), s)
                entry = {'window': wid, 'index': row['index'], 'corner_index': corner['corner_index'],
                         'area_m2': _num(polygon.area * gsd_a * gsd_a, 1), 'convex': corner['convex'],
                         'turn_deg': _num(corner['turn_deg'], 1), 'edge_m': [_num(v, 3) for v in corner['edge_m']],
                         'vertex_m': _xy(corner['vertex_m']), 'C_A': located_a['C'], 'status_A': located_a['status'],
                         'rms_A': located_a['rms_m'], 'gaps_A': corner_gaps(located_a['C'], corner, variants, s), 'passes': {}}
                for m in passes:
                    located_b = image_corner(cap_b, gsd_b, corner, references_b[m], shifts[m], s)
                    repeat = (math.hypot(located_b['C'][0] - located_a['C'][0], located_b['C'][1] - located_a['C'][1])
                              if located_a['C'] is not None and located_b['C'] is not None else None)
                    entry['passes'][m] = {'C_B': located_b['C'], 'status_B': located_b['status'], 'rms_B': located_b['rms_m'],
                                          'repeat_m': repeat, 'resolved': bool(repeat is not None and repeat <= s['resolved_m']),
                                          'gaps': corner_gaps(located_b['C'], corner, variants, s)}
                corners.append(entry)
                count += 1
        per_window.append({'window': wid, 'split': split.get(wid), 'population': len(members), 'scored': len(scored), 'corners': count})
        del cap_a, cap_b
    building_draws = _cluster_draws([(c['window'], c['index']) for c in corners], s['draws'], s['seed'])
    window_draws = _cluster_draws([c['window'] for c in corners], s['draws'], s['seed'])
    summaries = {m: summarise_pass(corners, m, building_draws, window_draws) for m in passes}
    inputs = rules_inputs(register_summary, summaries[0.], summaries[null_m], corners, null_m, building_draws)
    verdict = evaluate_rules(inputs, rules)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f'{name}_rows.jsonl', 'w') as handle:
        for c in corners:
            handle.write(json.dumps(_row(c, primary)) + '\n')
    payload.update({'computed_at': common.now_iso(), 'rules': verdict, 'rules_inputs': inputs, 'wording': verdict['R5']['wording'],
                    'summary': summaries[primary],
                    'passes': {f'{m:g}': {k: summaries[m][k] for k in ('corners', 'fitted_B', 'resolved_candidates', 'resolved',
                                                                      'resolved_share', 'repeat_m')} for m in passes},
                    'windows': per_window})
    common.write_json_atomic(folder / f'{name}.json', payload)
    print(json.dumps({'rules': {k: {'result': v['result'], 'inputs': v['inputs']} for k, v in verdict.items()}}, default=str))
    return {k: payload[k] for k in ('version', 'judge', 'source', 'other', 'cells', 'null_shift_m', 'rules_sha256', 'rules', 'summary')}
