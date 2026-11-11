"""Road corridor v1 (2026-10-07): a SIDECAR prototype that proposes straight, fixed-width road bands drawn underneath cars.

Reviewer ask (verbatim, 7 Oct ~11:25 AEDT, on road e31f2cb143c1b8dc of win-69468b2b783cd964): "roads need to converge on being
straight lines of a fixed width etc. layer it underneath other objects like cars/trees etc. look at how the SAM model can handle
that better". Design: data/reviews/eyes/experiments/road-shape-2026-10-07/UNDERSTANDING.md section 4a (P0 + P4 + P2 + P1/P3), steps
1-7, guards and windows as listed there. Geometry code ported from the census lens (census/road_shape_census.py: thin,
skeleton_graph, longest_path, polyline_fit, cross-section ends) and the priors lens (priors/prior_overlay.py, reserve_census.py:
road reserve = frame minus Vicmap parcels opened by 0.5 m, its medial line, lot classes).

Per layer of one window (pixel frame; metres through pixel_to_meters):
  1 P0  arbitrate: road / driveway / footpath candidates with IoU >= 0.8 form a twin group. A reviewer-CONFIRMED member decides
        the group's class (and keeps class and geometry); otherwise the group lying >= 0.8 inside the parcel reserve is 'road', a
        group on a numbered lot or CM is 'driveway', else undecided. Every pair is recorded; nothing is dropped. Decided
        (confirmed / needs_adjustment / geometry_reviewed) twins keep their class and geometry (xclass_twin, kept).
  2 P4  gather: the seed road, its same-mask sibling parts (same kind, same score to 4 dp, within gather_m) and its absorbable
        twins; clipped to the reserve + 0.5 m where the seed lies in the reserve. Specks and road candidates mostly inside the
        final band are absorbed after the band exists.
  3 P2  amodal fill: holes filled; live cars within adjacency_m of the mask (and >= half inside the reserve + 0.5 m when the
        mask is clipped) are unioned, buffered by adjacency_m. Trees are NEVER unioned; a cross-section ending at a tree is
        'occluded' and dropped from the width.
  4 P1/P3 axis: skeleton of the amodal mask at skel_m, longest geodesic path (spurs below junction_spur_m ignored = pruned),
        minimax chord polyline with <= 3 segments at segment_tol_m; a segment within snap_deg of the parcel-reserve medial line
        takes the reserve direction and keeps the mask's own lateral offset; consecutive near-parallel segments merge.
  5 width: cross-sections every 1 m; sections ending at a tree, the frame or another linear class are dropped; width = the
        width_pct percentile of the rest, capped at reserve width x reserve_cap_ratio. Offset = median section midpoint
        (offset_source 'mask') or the reserve medial line when snapped (offset_source 'reserve').
  6 band: the axis buffered by width/2, flat caps, mitre joins, clipped to the frame and the reserve + 0.5 m; <= 12 vertices;
        rounded to 2 dp, scene_extract.finalise_app_ring (the app's polygonIssues, capped rules) and make_valid.
  7 gate: no band (keep the modal polygon) when length < 10 m; more than one spur >= 3 m (junction / court); band IoU vs the
        amodal mask < iou_gate; clean-section CV > 0.35; reserve fraction < 0.8 when a reserve exists; the lot class is CM or a
        numbered lot. Plus guard exits (reported apart): a band may not cover a building, pool, water or a reviewer-decided
        object of another class (its own P0 twins excepted: they are the same surface); cars and trees are the only classes a
        band may go under (candidate driveway / footpath / grass a band swallows -> 'covers_other_class').

Guards: confirmed geometry is never rewritten (a decided seed's corridor is a proposal that 'replaces_on_accept'); decided
pieces of another object block; lineage = replaces / replaces_on_accept / absorbs / xclass_twin / derived_from as
linear-merge-v1 records it. ASSUMPTION (recorded in every output): the parking lane counts as road ("underneath cars": 10 of 12
cars on e31f are parked at the kerb); it needs the reviewer's yes.

Read-only on data/scenes. Writes only under data/reviews/eyes/experiments/road-shape-2026-10-07/prototype/ and the session
folder. No model, no network (parcels offline). Run from the repo root (nice -n 19 on the shared Mac):
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.road_corridor sweep
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.road_corridor run [--scenes ...]
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.road_corridor render
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import re
import sys
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from shapely import make_valid
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

from ..scene_extract import APP_RING_RULES, _ring_details, finalise_app_ring
from ..segmentation import iter_polygons

VERSION = 'road-corridor-v1'
ROOT = Path(__file__).resolve().parents[3]
EXP = ROOT/'data/reviews/eyes/experiments/road-shape-2026-10-07'
OUT = EXP/'prototype'
PROGRESS = ROOT/'.run/session-2026-10-07-roads/A/progress.txt'
SESSION = ROOT/'.run/session-2026-10-07-roads'
WINDOWS = ROOT/'data/reviews/eyes/recapture/windows-2026-10-01.jsonl'
LINEAR = ('road', 'driveway', 'footpath')
DEAD = ('rejected', 'superseded')
ASSUMPTION = ('The parking lane counts as road: the reviewer said "layer it underneath other objects like cars" and 10 of the 12 '
              'cars on e31f2cb143c1b8dc are parked at the kerb, so cars parked at the kerb go under the band. Needs the reviewer\'s yes.')

# Sweep default (prototype/sweep.json 'default', 2026-10-07): width_pct 75, adjacency_m 0.5, segment_tol_m 1.0 (no effect on the
# 10 windows: tied, design value kept), offset_source 'reserve', iou_gate 0.5, snap_deg 3. Chosen by the by-eye width error with
# building overlap 0 and every must-exit object exiting; see prototype/README.md.
DESIGN = {
    'version': VERSION,
    'iou_twin': .8, 'reserve_road_frac': .8, 'reserve_open_m': .5, 'reserve_clip_m': .5, 'reserve_min_width_m': 6.,
    'gather_m': 1., 'score_round': 4, 'speck_m2': 5., 'absorb_cover': .6,
    'adjacency_m': .5, 'car_reserve_frac': .5,
    'skel_m': .2, 'junction_spur_m': 3., 'max_segments': 3, 'segment_tol_m': 1., 'fit_samples': 60,
    'snap_deg': 3., 'snap_lateral_m': 8., 'merge_deg': 1.5, 'offset_source': 'reserve',
    'hook_min_m': 8., 'hook_share': .15, 'hook_deg': 10., 'k_iou_slack': .03, 'min_segment_m': 12.,
    'end_trim_max_m': 8., 'min_width_m': 3., 'overlap_share': .2,
    'section_step_m': 1., 'section_probe_m': .3, 'drop_ends': ('tree', 'frame', 'linear'),
    'width_pct': 75, 'reserve_cap_ratio': .9, 'min_clean_sections': 3,
    'max_band_vertices': 12, 'simplify_m': .05, 'ring_decimals': 2, 'multipart_keep': .95,
    'min_len_m': 10., 'iou_gate': .5, 'cv_gate': .35,
    'hard_blockers': ('building', 'pool', 'water'), 'blocker_eps_m2': .25,
    'soft_blockers': ('driveway', 'footpath', 'vegetation'), 'soft_eps_m2': 2., 'soft_share': .25,
    'under_kinds': ('car', 'tree'), 'decided_statuses': ('confirmed', 'needs_adjustment'),
    'lot_exit_classes': ('common_property', 'numbered_lot'),
}
GATE_EXITS = ('short', 'junction', 'low_iou', 'high_cv', 'reserve_fraction', 'lot_class')
# 'narrow' (band width < min_width_m) is an ADDITION to the listed gates: the rail-corridor piece e4daa47c41e0409f (a must-exit
# object, 2.6 m gravel strip inside the parcel gap) passes all six listed gates. Needs the reviewer's yes.
OTHER_EXITS = ('narrow', 'overlaps_proposal', 'p0_driveway', 'p0_decided_other_class', 'p0_conflict', 'no_axis', 'few_sections', 'covers_blocker',
               'covers_decided', 'covers_other_class', 'multipart', 'vertex_cap', 'app_ring_rules', 'empty_band')

# The sweep (UNDERSTANDING 4a): 6 positives and 4 must-exit windows with the named objects.
POSITIVES = {'win-69468b2b783cd964': ['e31f2cb143c1b8dc'], 'win-caec3328e2a28dd0': ['8f31a01f0b2560af'],
             'win-687c9f594f1601ac': ['d1be0d01f09f0c0c'], 'win-eb2dd3478100e8a8': ['9214c2d2f185b414'],
             'win-8567722993f2fc01': ['75b53c1466cd8191'], 'win-d43d38289fa196d1': ['788d6c8e92309962', '6d88adacbe93b77f']}
MUST_EXIT = {'win-75f63a60578722a8': ['93bd69c4a0ab6bb4'], 'lot-29358': ['a26787ee79c7c211'],
             'win-fa2e955f73294188': ['3548b51441cf85d4'],
             'win-2d422aac4db9f40d': ['33962d864280d577', '198f4bf9a8a6aa9b', 'e4daa47c41e0409f']}
SWEEP_GRID = {'width_pct': (50, 75, 90), 'adjacency_m': (.3, .5, 1.), 'segment_tol_m': (.5, 1., 1.5),
              'offset_source': ('reserve', 'mask'), 'iou_gate': (.5, .6, .7),
              # an EXTRA knob beyond UNDERSTANDING 4a: the reserve direction snap on (3 degrees, the design) or off (0); on e31f the
              # reserve medial line runs about 1 degree off the kerbs read by eye
              'snap_deg': (3., 0.)}


def beat(msg):
    try:
        PROGRESS.parent.mkdir(parents=True, exist_ok=True)
        with open(PROGRESS, 'a') as fh:
            fh.write(f'{datetime.now().strftime("%H:%M:%S")} corridor: {msg}\n')
    except OSError:
        pass


def r(value, nd=3):
    return None if value is None else round(float(value), nd)


# --- geometry helpers ------------------------------------------------------------------------------------------------------

def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type in ('Polygon', 'MultiPolygon'):
        return geometry
    parts = [g for g in iter_polygons(geometry) if not g.is_empty]
    return unary_union(parts) if parts else None


def obj_polygon(obj, with_holes=True):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None
    holes = [h for h in (obj.get('holes') or []) if len(h) >= 3] if with_holes else []
    try:
        poly = Polygon(ring, holes)
    except (ValueError, TypeError):
        return None
    if not poly.is_valid:
        poly = polygonal(make_valid(poly))
    return poly if poly is not None and poly.area > 0 else None


def fill_holes(geometry):
    parts = [Polygon(p.exterior) for p in iter_polygons(geometry) if p.area > 0]
    return unary_union(parts) if parts else None


def largest(geometry):
    parts = sorted(iter_polygons(geometry), key=lambda p: -p.area)
    return parts[0] if parts else None


def iou(a, b):
    if a is None or b is None:
        return 0.
    union = a.union(b).area
    return a.intersection(b).area/union if union > 0 else 0.


def area_m2(geometry, mpp):
    return 0. if geometry is None else geometry.area*mpp*mpp


# --- parcels: reserve, lot classes, reserve medial line (priors lens) -------------------------------------------------------

def lot_class(props):
    """Vicmap Parcel lot class: common_property (CM), reserve_lot (R1 ...), numbered_lot (1, 12A ...), else single_lot_or_crown
    (CP / PC / PP plans, crown allotments, unlotted)."""
    lot = str(props.get('lot') or '').strip()
    spi = str(props.get('spi') or '').strip().upper()
    if lot.upper().startswith('CM') or spi.startswith('CM'):
        return 'common_property'
    if re.fullmatch(r'R\d+', lot.upper()):
        return 'reserve_lot'
    if re.fullmatch(r'\d+[A-Za-z]?', lot):
        return 'numbered_lot'
    return 'single_lot_or_crown'


def reserve_and_lots(scene, parcels, design=DESIGN):
    """(reserve polygon in frame px or None, {lot class: polygon}) from a parcels GeoJSON payload; reserve = frame minus the
    parcel union, opened by reserve_open_m (priors lens prior_overlay.py / reserve_census.py)."""
    from ..scene_addresses import image_projection
    width, height = scene['image_width'], scene['image_height']
    mpp = scene_mpp(scene)
    frame = box(0, 0, width, height)
    if not parcels or not parcels.get('features'):
        return None, {}
    project = image_projection(scene)
    by_class = defaultdict(list)
    for feature in parcels['features']:
        geometry = feature.get('geometry') or {}
        coords = [geometry.get('coordinates')] if geometry.get('type') == 'Polygon' else (geometry.get('coordinates') or [])
        for poly in coords:
            if not poly:
                continue
            try:
                pg = Polygon([project(x, y) for x, y in poly[0]], [[project(x, y) for x, y in ring] for ring in poly[1:]])
            except (ValueError, TypeError):
                continue
            pg = pg if pg.is_valid else polygonal(make_valid(pg))
            if pg is not None and not pg.is_empty:
                by_class[lot_class(feature.get('properties') or {})].append(pg)
    lots = {k: polygonal(unary_union(v).intersection(frame)) for k, v in sorted(by_class.items())}
    land = unary_union([g for g in lots.values() if g is not None]) if lots else None
    o = design['reserve_open_m']/mpp
    reserve = frame if land is None else frame.difference(land)
    reserve = polygonal(reserve.buffer(-o).buffer(o).intersection(frame))
    if reserve is None or area_m2(reserve, mpp) < 5.:
        reserve = None
    return reserve, {k: v for k, v in lots.items() if v is not None}


def lot_shares(geometry, reserve, lots):
    """Share of the geometry's area on the reserve and on each lot class; dominant = the largest share."""
    if geometry is None or geometry.area <= 0:
        return {}, None
    shares = {}
    if reserve is not None:
        shares['reserve'] = geometry.intersection(reserve).area/geometry.area
    for k, g in lots.items():
        a = geometry.intersection(g).area/geometry.area
        if a > 0:
            shares[k] = a
    shares = {k: round(v, 3) for k, v in shares.items() if v > 0}
    dominant = max(sorted(shares), key=lambda k: shares[k]) if shares else None
    return shares, dominant


def scene_mpp(scene):
    m = np.asarray(scene['pixel_to_meters'], float)
    return math.sqrt(abs(float(np.linalg.det(m[:2]))))


# --- raster skeleton (census lens) -----------------------------------------------------------------------------------------

def raster(geometry, scale, x0, y0, width, height):
    img = Image.new('L', (width, height), 0)
    draw = ImageDraw.Draw(img)
    for poly in iter_polygons(geometry):
        draw.polygon([(x*scale-x0, y*scale-y0) for x, y in poly.exterior.coords], fill=1)
        for hole in poly.interiors:
            draw.polygon([(x*scale-x0, y*scale-y0) for x, y in hole.coords], fill=0)
    return np.asarray(img, dtype=np.uint8).copy()


def thin(mask):
    """Zhang-Suen thinning, vectorised (census/road_shape_census.py)."""
    img = np.pad(mask.astype(np.uint8), 1)
    while True:
        changed = False
        for step in (0, 1):
            p2, p3, p4 = img[:-2, 1:-1], img[:-2, 2:], img[1:-1, 2:]
            p5, p6, p7 = img[2:, 2:], img[2:, 1:-1], img[2:, :-2]
            p8, p9, c = img[1:-1, :-2], img[:-2, :-2], img[1:-1, 1:-1]
            b = p2.astype(np.int16)+p3+p4+p5+p6+p7+p8+p9
            seq = (p2, p3, p4, p5, p6, p7, p8, p9, p2)
            a = sum(((seq[i] == 0) & (seq[i+1] == 1)).astype(np.int16) for i in range(8))
            if step == 0:
                m = (c == 1) & (b >= 2) & (b <= 6) & (a == 1) & ((p2*p4*p6) == 0) & ((p4*p6*p8) == 0)
            else:
                m = (c == 1) & (b >= 2) & (b <= 6) & (a == 1) & ((p2*p4*p8) == 0) & ((p2*p6*p8) == 0)
            if m.any():
                img[1:-1, 1:-1][m] = 0
                changed = True
        if not changed:
            break
    return img[1:-1, 1:-1].astype(bool)


def skeleton_graph(skel):
    ys, xs = np.nonzero(skel)
    n = len(ys)
    index = -np.ones(skel.shape, dtype=np.int64)
    index[ys, xs] = np.arange(n)
    rows, cols, wts = [], [], []
    hgt, wid = skel.shape
    for dy, dx, wt in ((0, 1, 1.), (1, 0, 1.), (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2))):
        ny, nx = ys+dy, xs+dx
        ok = (ny >= 0) & (ny < hgt) & (nx >= 0) & (nx < wid)
        nb = np.full(n, -1)
        nb[ok] = index[ny[ok], nx[ok]]
        sel = nb >= 0
        rows.append(np.arange(n)[sel])
        cols.append(nb[sel])
        wts.append(np.full(sel.sum(), wt))
    rr, cc, ww = np.concatenate(rows), np.concatenate(cols), np.concatenate(wts)
    graph = coo_matrix((np.r_[ww, ww], (np.r_[rr, cc], np.r_[cc, rr])), shape=(n, n)).tocsr()
    return ys, xs, graph


def longest_path(graph, members):
    d = dijkstra(graph, indices=int(members[0]))
    d[~np.isfinite(d)] = -1
    far = int(np.argmax(d))
    d2, pred = dijkstra(graph, indices=far, return_predecessors=True)
    d2[~np.isfinite(d2)] = -1
    end = int(np.argmax(d2))
    path = [end]
    while path[-1] != far and pred[path[-1]] >= 0:
        path.append(int(pred[path[-1]]))
    return path[::-1], float(d2[end])


def seg_dist(pts, a, b):
    ab = b-a
    denom = float(ab @ ab)
    if denom == 0:
        return np.linalg.norm(pts-a, axis=1)
    t = np.clip(((pts-a) @ ab)/denom, 0, 1)
    return np.linalg.norm(pts-(a+t[:, None]*ab), axis=1)


def polyline_fit(pts):
    """Minimax chord polyline with k = 1, 2, 3 segments on sampled points (census lens). Returns devs[k], breakpoints[k]."""
    n = len(pts)
    dev = np.zeros((n, n))
    for i in range(n):
        for j in range(i+2, n):
            dev[i, j] = seg_dist(pts[i+1:j], pts[i], pts[j]).max()
    devs, bps = {1: float(dev[0, n-1])}, {1: [0, n-1]}
    if n >= 3:
        cand = np.maximum(dev[0, 1:n-1], dev[1:n-1, n-1])
        b = int(np.argmin(cand))+1
        devs[2], bps[2] = float(cand[b-1]), [0, b, n-1]
    else:
        devs[2], bps[2] = devs[1], bps[1]
    if n >= 4:
        b1 = np.arange(1, n-2)[:, None]
        b2 = np.arange(2, n-1)[None, :]
        val = np.maximum(np.maximum(dev[0, b1], dev[b1, b2]), dev[b2, n-1])
        val = np.where(b2 > b1, val, np.inf)
        i, j = np.unravel_index(int(np.argmin(val)), val.shape)
        devs[3], bps[3] = float(val[i, j]), [0, int(b1[i, 0]), int(b2[0, j]), n-1]
    else:
        devs[3], bps[3] = devs[2], bps[2]
    return devs, bps


def resample(points, count):
    line = LineString(points)
    if line.length == 0:
        return np.asarray([points[0], points[0]], float)
    return np.asarray([line.interpolate(t, normalized=True).coords[0] for t in np.linspace(0, 1, count)])


def fold_end_hooks(chord, mpp, design=DESIGN):
    """Drop an end segment shorter than max(hook_min_m, hook_share x the chord length) that turns more than hook_deg from its
    neighbour (an end fork or a bite, not a bend of the street); extend_ends restores the length along the neighbour."""
    chord = [np.asarray(c, float) for c in chord]
    changed = True
    while changed and len(chord) > 2:
        changed = False
        total = sum(float(np.linalg.norm(b-a)) for a, b in zip(chord[:-1], chord[1:], strict=True))
        for end, (i0, i1, i2) in (('start', (0, 1, 2)), ('end', (-1, -2, -3))):
            a, b, c = chord[i0], chord[i1], chord[i2]
            length = float(np.linalg.norm(b-a))*mpp
            turn = math.degrees(math.acos(min(1., max(-1., float(unit(b-a) @ unit(c-b))))))
            if length < max(design['hook_min_m'], design['hook_share']*total*mpp) and turn > design['hook_deg']:
                chord = chord[1:] if end == 'start' else chord[:-1]
                changed = True
                break
    return chord


def fold_jogs(chord, mpp, design=DESIGN):
    """Replace a MIDDLE segment shorter than hook_min_m (a lateral step of the mask edge, e.g. where a shadow line gives way
    to parked cars) by the intersection of its neighbours' lines (or the step's midpoint when that is far)."""
    chord = [np.asarray(c, float) for c in chord]
    changed = True
    while changed and len(chord) > 3:
        changed = False
        for i in range(1, len(chord)-2):
            a, b = chord[i], chord[i+1]
            if float(np.linalg.norm(b-a))*mpp >= design['hook_min_m']:
                continue
            d1, d2 = unit(chord[i]-chord[i-1]), unit(chord[i+2]-chord[i+1])
            m = np.array([d1, -d2]).T
            q = (a+b)/2
            if abs(np.linalg.det(m)) > 1e-6:
                t, _ = np.linalg.solve(m, chord[i+1]-chord[i-1])
                cand = chord[i-1]+d1*t
                if np.linalg.norm(cand-q)*mpp <= 15.:
                    q = cand
            chord = chord[:i]+[q]+chord[i+2:]
            changed = True
            break
    return chord


def refine_lines(chord, pixels):
    """Each chord segment refitted as a least-squares line (cv2.fitLine) on the mask pixels nearest to it; returns segments
    {p (centroid of its pixels), d (oriented like the chord), t0, t1 (the chord ends projected)}."""
    import cv2
    chord = [np.asarray(c, float) for c in chord]
    k = len(chord)-1
    if k < 1 or len(pixels) == 0:
        return []
    dists = np.stack([seg_dist(pixels, chord[i], chord[i+1]) for i in range(k)], axis=1)
    owner = np.argmin(dists, axis=1)
    segs = []
    for i in range(k):
        a, b = chord[i], chord[i+1]
        d0 = unit(b-a)
        pts = pixels[owner == i]
        if len(pts) >= 10:
            vx, vy, cx, cy = cv2.fitLine(pts.astype(np.float32), cv2.DIST_L2, 0, .01, .01).ravel()
            d = unit(np.array([vx, vy], float))
            d = d if d @ d0 >= 0 else -d
            p = np.array([cx, cy], float)
        else:
            d, p = d0, (a+b)/2
        segs.append({'p': p, 'd': d, 't0': float((a-p) @ d), 't1': float((b-p) @ d), 'snapped': False, 'angle_deg': None,
                     'reserve_pt': None})
    return segs


def mask_axis(geometry, mpp, design=DESIGN, frame_size=None, spur_ctx=None):
    """Skeleton axis of a (filled) mask: longest geodesic path at skel_m, trimmed by the local half width at both ends, the
    spurs that protrude >= junction_spur_m beyond the local half width (clustered by attachment), and the minimax chord
    polyline (<= max_segments segments, smallest k with max deviation <= segment_tol_m). None when there is no skeleton.
    spur_ctx (v3.2 only; None = as before) = {'cars', 'car_polys', 'road'}: a spur >= junction_spur_m whose end region is a
    car spur (car_spur_test) is not clustered (not a side street); it is listed in 'car_spurs' ('car_spur_index' = its index
    in 'spurs'), and every spur >= junction_spur_m gets its test in 'spur_tests'."""
    scale = mpp/design['skel_m']
    minx, miny, maxx, maxy = geometry.bounds
    x0, y0 = int(math.floor(minx*scale))-2, int(math.floor(miny*scale))-2
    w, h = int(math.ceil(maxx*scale))-x0+3, int(math.ceil(maxy*scale))-y0+3
    mask = raster(geometry, scale, x0, y0, w, h)
    if mask.sum() < 4:
        return None
    dt = ndimage.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    skel = thin(mask)
    if skel.sum() < 2:
        return None
    ys, xs, graph = skeleton_graph(skel)
    ncomp, labels = connected_components(graph, directed=False)
    best = None
    for comp, size in Counter(labels.tolist()).most_common(4):
        if size < 2:
            continue
        members = np.nonzero(labels == comp)[0]
        path, length = longest_path(graph, members)
        if best is None or length > best[1]:
            best = (path, length, members)
    if best is None:
        return None
    path, plen, members = best
    py, px = ys[path], xs[path]
    pts = np.c_[(px+x0)/scale, (py+y0)/scale]                         # frame px
    steps = np.r_[0, np.hypot(np.diff(px), np.diff(py))]
    s = np.cumsum(steps)
    half = dt[py, px]
    keep = (s >= half) & ((s[-1]-s) >= half)
    if keep.sum() < 2:
        keep = np.ones(len(path), bool)
    tpts = pts[keep]
    # the chord is fitted away from the end forks: 1.5 x the local half width from each end, and a half width + 1 m clear of
    # the frame (a frame cut thins into a Y whose arms hook the end segments; e31f hooked 77 and 29 degrees before this)
    if frame_size is not None:
        fw, fh = frame_size
        fx, fy = pts[:, 0], pts[:, 1]
        frame_d = np.minimum.reduce([fx, fy, fw-fx, fh-fy])*mpp/design['skel_m']
    else:
        frame_d = np.full(len(pts), np.inf)
    fit_keep = (s >= 1.5*half) & ((s[-1]-s) >= 1.5*half) & (frame_d >= half+1./design['skel_m'])
    fit_src = pts[fit_keep] if fit_keep.sum() >= 10 else tpts
    # spurs: skeleton endpoints off the path whose protrusion (geodesic distance from the path minus the half width at the
    # attachment) is >= junction_spur_m ('pruned': shorter branches do not count); endpoints of ONE arm (its end fork) are
    # merged when their geodesic distance is under the larger protrusion + 2 half widths, so a crossroads counts two arms
    sub = graph[members][:, members]
    deg = np.asarray((sub > 0).sum(axis=1)).ravel()
    on_path = set(path)
    ends = [int(members[i]) for i in np.nonzero(deg == 1)[0] if int(members[i]) not in on_path]
    spurs = []
    if ends:
        dist, preds, sources = dijkstra(graph, indices=path, min_only=True, return_predecessors=True)
        pos = {node: k for k, node in enumerate(path)}
        for e in ends:
            if not np.isfinite(dist[e]):
                continue
            attach = int(sources[e])
            hw = float(dt[ys[attach], xs[attach]])
            spurs.append({'node': e, 'hw_m': hw*design['skel_m'], 'end_px': [r((xs[e]+x0)/scale, 1), r((ys[e]+y0)/scale, 1)],
                          'protrusion_m': r((float(dist[e])-hw)*design['skel_m'], 2), 'along_m': r(s[pos[attach]]*design['skel_m'], 2)})
    big = [sp for sp in spurs if sp['protrusion_m'] >= design['junction_spur_m']]
    car_spurs, car_index, spur_tests = [], [], []
    if spur_ctx is not None and big:        # v3.2 (car spur): a spur ending in car pixels only is not a side street
        keep = []
        for sp in big:
            seq, run_px = [sp['node']], 0.
            while run_px < design['car_spur_end_m']/design['skel_m'] and preds[seq[-1]] >= 0:
                nxt = int(preds[seq[-1]])
                run_px += float(dist[seq[-1]]-dist[nxt])
                seq.append(nxt)
            pts = [((xs[q]+x0)/scale, (ys[q]+y0)/scale) for q in seq]
            test = car_spur_test(pts, spur_ctx, mpp, design)
            spur_tests.append({'end_px': sp['end_px'], 'protrusion_m': sp['protrusion_m'], **test})
            if test['car_spur']:
                car_index.append(next(i for i, q in enumerate(spurs) if q is sp))
                car_spurs.append({'end_px': sp['end_px'], 'protrusion_m': sp['protrusion_m'],
                                  'spur_m': r(float(dist[sp['node']])*design['skel_m'], 2), 'hw_m': r(sp['hw_m'], 2),
                                  'along_m': sp['along_m'], 'cars': test['cars'], 'end_region_px': test['region_px']})
            else:
                keep.append(sp)
        big = keep
    parent = list(range(len(big)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    if len(big) > 1:
        geo = dijkstra(graph, indices=[sp['node'] for sp in big])*design['skel_m']
        for i, j in itertools.combinations(range(len(big)), 2):
            if geo[i, big[j]['node']] < max(big[i]['protrusion_m'], big[j]['protrusion_m'])+2*max(big[i]['hw_m'], big[j]['hw_m']):
                parent[root(i)] = root(j)
    clusters = {root(i) for i in range(len(big))}
    for sp in spurs:
        sp.pop('node')
        sp['hw_m'] = r(sp['hw_m'], 2)
    fit_pts = resample([tuple(p) for p in fit_src], min(design['fit_samples'], max(2, len(fit_src))))
    devs, bps = polyline_fit(fit_pts)
    k_used = next((k for k in range(1, design['max_segments']+1) if devs[k]*mpp <= design['segment_tol_m']), design['max_segments'])
    chords = {}
    for k in range(1, k_used+1):
        c = fold_end_hooks(fold_jogs([fit_pts[i] for i in bps[k]], mpp, design), mpp, design)
        chords.setdefault(len(c)-1, c)
    chord = chords[min(chords, key=lambda kk: -kk)]
    ys_m, xs_m = np.nonzero(mask)
    pix = np.c_[(xs_m+x0)/scale, (ys_m+y0)/scale]
    out = {'path': pts, 'trimmed': tpts, 'length_m': plen*design['skel_m'], 'chord': chord, 'k': k_used, 'chords': chords,
           'pixels': pix,
           'maxdev_m': {k: r(devs[k]*mpp, 2) for k in devs}, 'spurs': spurs, 'junction_spurs': len(clusters),
           'half_width_m_median': r(np.median(half)*design['skel_m'], 2)}
    if spur_ctx is not None:
        out.update(car_spurs=car_spurs, car_spur_index=car_index, spur_tests=spur_tests)
    return out


def car_spur_test(pts, spur_ctx, mpp, design):
    """v3.2 car spur test of one spur's end region: the last car_spur_end_m of the spur (pts, frame px, from the skeleton end
    inwards) buffered by car_spur_buffer_m. Pixels = frame pixels whose centre lies in the region. A car spur: every region
    pixel lies inside the unioned car polygons and none lies on the street's road-mask pieces (spur_ctx 'road')."""
    import shapely
    region = (LineString(pts) if len(pts) >= 2 else Point(pts[0])).buffer(design['car_spur_buffer_m']/mpp)
    minx, miny, maxx, maxy = region.bounds
    gx, gy = np.meshgrid(np.arange(math.floor(minx), math.ceil(maxx))+.5, np.arange(math.floor(miny), math.ceil(maxy))+.5)
    gx, gy = gx.ravel(), gy.ravel()
    inside = shapely.contains_xy(region, gx, gy)
    gx, gy = gx[inside], gy[inside]
    none = np.zeros(len(gx), bool)
    car = shapely.contains_xy(spur_ctx['cars'], gx, gy) if spur_ctx.get('cars') is not None else none
    road = shapely.contains_xy(spur_ctx['road'], gx, gy) if spur_ctx.get('road') is not None else none
    cars = sorted(i for i, p in (spur_ctx.get('car_polys') or {}).items() if shapely.contains_xy(p, gx, gy).any())
    n, n_car, n_road = int(len(gx)), int(car.sum()), int(road.sum())
    return {'car_spur': bool(n > 0 and n_car == n and n_road == 0), 'region_px': n, 'car_px': n_car,
            'outside_car_px': n-n_car, 'road_px': n_road, 'cars': cars}


def reserve_medial(reserve, width, height, mpp, design=DESIGN):
    """Reserve medial line: skeleton of the reserve mask at skel_m (the frame edge is not a boundary: padded with ones), with
    the local reserve width; points narrower than reserve_min_width_m (laneways, slivers) are dropped. (N, 3) array x, y, w_m."""
    if reserve is None:
        return np.zeros((0, 3))
    scale = mpp/design['skel_m']
    w, h = int(math.ceil(width*scale)), int(math.ceil(height*scale))
    mask = raster(reserve, scale, 0, 0, w, h)
    pad = 40
    dt = ndimage.distance_transform_edt(np.pad(mask, pad, constant_values=1))[pad:-pad, pad:-pad]
    skel = thin(mask) & (mask > 0)
    ys, xs = np.nonzero(skel)
    wm = 2*dt[ys, xs]*design['skel_m']
    sel = wm >= design['reserve_min_width_m']
    return np.c_[xs[sel]/scale, ys[sel]/scale, wm[sel]]


# --- axis: snap, merge, extend ---------------------------------------------------------------------------------------------

def unit(v):
    n = float(np.linalg.norm(v))
    return v/n if n > 0 else v


def normal(d):
    return np.array([-d[1], d[0]])


def snap_direction(a, b, medial, mpp, design=DESIGN):
    """The reserve medial line near segment a-b (within snap_lateral_m, along the segment +- 5 m): Huber line fit; returns
    (direction oriented like a->b, a point on the reserve line, angle in degrees) or None when too few medial points."""
    if medial is None or len(medial) == 0:
        return None
    d = unit(b-a)
    length = float(np.linalg.norm(b-a))
    rel = medial[:, :2]-a
    along = rel @ d
    lat = rel @ normal(d)
    sel = (along > -5./mpp) & (along < length+5./mpp) & (np.abs(lat) < design['snap_lateral_m']/mpp)
    if sel.sum() < max(10, .3*length*mpp/design['skel_m']):
        return None
    import cv2
    vx, vy, cx, cy = cv2.fitLine(medial[sel, :2].astype(np.float32), cv2.DIST_HUBER, 0, .01, .01).ravel()
    rd = unit(np.array([vx, vy], float))
    if rd @ d < 0:
        rd = -rd
    angle = math.degrees(math.acos(min(1., abs(float(rd @ d)))))
    return rd, np.array([cx, cy], float), angle


def build_segments(chord, medial, mpp, design=DESIGN, pixels=None):
    """Chord points -> segments {p, d, t0, t1, snapped, angle, reserve_pt}: each segment refitted on its mask pixels
    (refine_lines; the chord itself when no pixels), its direction snapped to the reserve medial line within snap_deg (the
    segment keeps its own lateral offset: p is unchanged); consecutive segments within merge_deg merge (length-weighted)."""
    if pixels is not None and len(pixels):
        base = refine_lines(chord, pixels)
    else:
        base = []
        for a, b in zip(chord[:-1], chord[1:], strict=True):
            a, b = np.asarray(a, float), np.asarray(b, float)
            if float(np.linalg.norm(b-a)) > 0:
                base.append({'p': (a+b)/2, 'd': unit(b-a), 't0': -float(np.linalg.norm(b-a))/2, 't1': float(np.linalg.norm(b-a))/2,
                             'snapped': False, 'angle_deg': None, 'reserve_pt': None})
    segs = []
    for seg in base:
        a, b = seg['p']+seg['d']*seg['t0'], seg['p']+seg['d']*seg['t1']
        if seg['t1']-seg['t0'] <= 0:
            continue
        snap = snap_direction(a, b, medial, mpp, design)
        if snap is not None:
            seg['angle_deg'] = r(snap[2], 2)
            if snap[2] <= design['snap_deg']:
                seg.update(d=snap[0], snapped=True, reserve_pt=snap[1])
                seg['t0'], seg['t1'] = float((a-seg['p']) @ seg['d']), float((b-seg['p']) @ seg['d'])
        segs.append(seg)
    merged = True
    while merged and len(segs) > 1:
        merged = False
        for i in range(len(segs)-1):
            s1, s2 = segs[i], segs[i+1]
            if math.degrees(math.acos(min(1., abs(float(s1['d'] @ s2['d']))))) <= design['merge_deg']:
                start, end = s1['p']+s1['d']*s1['t0'], s2['p']+s2['d']*s2['t1']
                l1, l2 = s1['t1']-s1['t0'], s2['t1']-s2['t0']
                d = unit(l1*s1['d']+l2*s2['d'])
                p = (l1*s1['p']+l2*s2['p'])/(l1+l2)
                snapped = s1['snapped'] and s2['snapped']
                segs[i] = {'p': p, 'd': d, 't0': float((start-p) @ d), 't1': float((end-p) @ d), 'snapped': snapped,
                           'angle_deg': s1['angle_deg'] if s1['angle_deg'] is not None else s2['angle_deg'],
                           'reserve_pt': s1['reserve_pt'] if s1['reserve_pt'] is not None else s2['reserve_pt']}
                del segs[i+1]
                merged = True
                break
    return segs


def extend_ends(segs, amodal, mpp):
    """Extend the first / last segment to the extent of the amodal mask along its own direction (inside a lateral strip of
    +- 8 m or the mask's half extent), so the band runs the street's length in the frame."""
    if not segs:
        return segs
    for idx, sign in ((0, -1.), (len(segs)-1, 1.)):
        seg = segs[idx]
        d = seg['d']*sign
        t_end = seg['t0'] if sign < 0 else seg['t1']
        start = seg['p']+seg['d']*t_end
        n = normal(d)
        lat = 8./mpp
        strip = Polygon([tuple(start+n*lat), tuple(start+n*lat+d*4000), tuple(start-n*lat+d*4000), tuple(start-n*lat)])
        part = amodal.intersection(strip)
        best = 0.
        for poly in iter_polygons(part):
            coords = np.asarray(poly.exterior.coords)
            best = max(best, float(((coords-start) @ d).max()))
        if sign < 0:
            seg['t0'] -= best
        else:
            seg['t1'] += best
    return segs


def axis_polyline(segs, offsets=None, max_joint_px=None):
    """Polyline of the segments (each shifted by its lateral offset in px): start of the first, the intersections of
    consecutive lines (a jog to the projected ends when the intersection is far), end of the last."""
    offsets = offsets or [0.]*len(segs)
    lines = [(s['p']+normal(s['d'])*o, s['d'], s['t0'], s['t1']) for s, o in zip(segs, offsets, strict=True)]
    pts = [lines[0][0]+lines[0][1]*lines[0][2]]
    for (p1, d1, _, t1), (p2, d2, t0b, _) in zip(lines[:-1], lines[1:], strict=True):
        m = np.array([d1, -d2]).T
        end1, start2 = p1+d1*t1, p2+d2*t0b
        if abs(np.linalg.det(m)) > 1e-6:
            t, _ = np.linalg.solve(m, p2-p1)
            q = p1+d1*t
            if max_joint_px is None or np.linalg.norm(q-end1) <= max_joint_px:
                pts.append(q)
                continue
        pts.extend([end1, start2])
    pts.append(lines[-1][0]+lines[-1][1]*lines[-1][3])
    return [np.asarray(p, float) for p in pts]


# --- cross-sections --------------------------------------------------------------------------------------------------------

def containing(geometry, point, tol=.75):
    if geometry is None or geometry.is_empty:
        return None
    best = None
    for part in getattr(geometry, 'geoms', [geometry]):
        if part.geom_type == 'LineString':
            dist = part.distance(point)
            if dist <= tol and (best is None or dist < best[0]):
                best = (dist, part)
    return best[1] if best else None


def sections_along(segs, amodal, modal, reserve, ends_ctx, mpp, width, height, design=DESIGN):
    """Every section_step_m along each segment line: the normal through the station; the piece of the amodal mask containing
    the station, its width and its two ends, each classed by what lies section_probe_m beyond it (frame / tree / linear =
    another linear class outside the street / car = a car not unioned / building / reserve_edge / free); the modal (filled)
    width and the reserve width at the same station."""
    step, reach, probe = design['section_step_m']/mpp, 60./mpp, design['section_probe_m']/mpp
    out = []
    reserve_edge = reserve.boundary.buffer(2.) if reserve is not None else None
    for k, seg in enumerate(segs):
        d, n = seg['d'], normal(seg['d'])
        count = int((seg['t1']-seg['t0'])//step)+1
        for i in range(count):
            t = seg['t0']+min(i*step, seg['t1']-seg['t0'])
            p = seg['p']+d*t
            if not (0 <= p[0] <= width and 0 <= p[1] <= height):
                continue
            pt = Point(p)
            line = LineString([tuple(p-n*reach), tuple(p+n*reach)])
            rec = {'seg': k, 't': float(t), 'p': p, 'n': n}
            piece = containing(line.intersection(amodal), pt)
            if piece is None:
                rec.update(outside=True, clean=False, ends=['outside', 'outside'])
                out.append(rec)
                continue
            offs = sorted(((float((np.asarray(c)-p) @ n), np.asarray(c)) for c in (piece.coords[0], piece.coords[-1])), key=lambda x: x[0])
            ends = []
            for side, (_, e) in enumerate(offs):
                outward = -1. if side == 0 else 1.
                beyond = Point(e+n*outward*probe)
                if e[0] <= 1 or e[1] <= 1 or e[0] >= width-1 or e[1] >= height-1:
                    ends.append('frame')
                elif ends_ctx['tree'] is not None and ends_ctx['tree'].contains(beyond):
                    ends.append('tree')
                elif ends_ctx['linear'] is not None and ends_ctx['linear'].contains(beyond):
                    ends.append('linear')
                elif ends_ctx['car'] is not None and ends_ctx['car'].contains(beyond):
                    ends.append('car')
                elif ends_ctx['building'] is not None and ends_ctx['building'].contains(beyond):
                    ends.append('building')
                elif reserve_edge is not None and reserve_edge.contains(Point(e)):
                    ends.append('reserve_edge')
                else:
                    ends.append('free')
            rec.update(outside=False, lo=offs[0][0], hi=offs[1][0], w=(offs[1][0]-offs[0][0])*mpp, ends=ends,
                       clean=not any(e in design['drop_ends'] for e in ends))
            mp = containing(line.intersection(modal), pt) if modal is not None else None
            rec['w_modal'] = mp.length*mpp if mp is not None else None
            rp = containing(line.intersection(reserve), pt) if reserve is not None else None
            rec['w_reserve'] = rp.length*mpp if rp is not None else None
            out.append(rec)
    return out


def cv_of(values):
    values = np.asarray([v for v in values if v is not None], float)
    return float(np.std(values)/np.mean(values)) if len(values) >= 2 and np.mean(values) > 0 else None


# --- one layer -------------------------------------------------------------------------------------------------------------

def layer_context(objects, mpp, width, height, reserve, lots, design=DESIGN):
    live = [o for o in objects if o.get('status') not in DEAD]
    polys = {}
    for o in live:
        p = obj_polygon(o, True)
        if p is not None:
            polys[o['id']] = p
    by_kind = defaultdict(list)
    for o in live:
        if o['id'] in polys:
            by_kind[o['kind']].append(o)
    return {'objects': objects, 'live': live, 'by_id': {o['id']: o for o in objects}, 'polys': polys, 'by_kind': by_kind,
            'mpp': mpp, 'W': width, 'H': height, 'reserve': reserve, 'lots': lots, 'frame': box(0, 0, width, height),
            'medial': reserve_medial(reserve, width, height, mpp, design)}


def is_decided(obj, design=DESIGN):
    return bool(obj.get('status') in design['decided_statuses'] or obj.get('geometry_reviewed'))


def arbitrate(ctx, design=DESIGN, retired=None):
    """P0: union-find over cross-class linear pairs with IoU >= iou_twin. One record per group: members, pairs, reserve
    fraction, lot shares, resolved class and how it was decided. Nothing is dropped. retired = {object id: decision} (the
    --retire option, v3 layers only): a decided member the reviewer retired keeps its place in the group but its class is not
    counted as confirmed, so it causes no 'conflict'; the group then carries 'retired_by_reviewer' and the classes it would have
    read without the retire. Without retired the records are unchanged."""
    retired = retired or {}
    lin = sorted((o for k in LINEAR for o in ctx['by_kind'].get(k, [])), key=lambda o: o['id'])
    polys = ctx['polys']
    parent = {o['id']: o['id'] for o in lin}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    pairs = []
    for i, a in enumerate(lin):
        pa = polys[a['id']]
        for b in lin[i+1:]:
            if a['kind'] == b['kind']:
                continue
            pb = polys[b['id']]
            if not pa.intersects(pb):
                continue
            v = iou(pa, pb)
            if v >= design['iou_twin']:
                pairs.append({'a': a['id'], 'a_kind': a['kind'], 'b': b['id'], 'b_kind': b['kind'], 'iou': r(v)})
                parent[find(a['id'])] = find(b['id'])
    groups = defaultdict(list)
    for o in lin:
        groups[find(o['id'])].append(o)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        ids = sorted(o['id'] for o in members)
        union = unary_union([polys[i] for i in ids])
        shares, dominant = lot_shares(union, ctx['reserve'], ctx['lots'])
        confirmed = sorted({o['kind'] for o in members if o.get('status') == 'confirmed' and o['id'] not in retired})
        decided = [{'id': o['id'], 'kind': o['kind'], 'status': o.get('status')} for o in members if is_decided(o, design)]
        gone = sorted(o['id'] for o in members if o['id'] in retired)
        if len(confirmed) > 1:
            resolved, how = None, 'conflict: confirmed members of different classes'
        elif confirmed:
            resolved, how = confirmed[0], 'confirmed member keeps its class'
        elif ctx['reserve'] is not None and shares.get('reserve', 0.) >= design['reserve_road_frac']:
            resolved, how = 'road', f'reserve fraction {shares.get("reserve", 0.):.3f} >= {design["reserve_road_frac"]}'
        elif dominant in design['lot_exit_classes']:
            resolved, how = 'driveway', f'on {dominant}'
        else:
            resolved, how = None, f'undecided (dominant {dominant})'
        gid = hashlib.sha256(','.join(ids).encode()).hexdigest()[:12]
        out.append({'group': gid, 'members': [{'id': o['id'], 'kind': o['kind'], 'status': o.get('status'),
                                                'decided': is_decided(o, design), 'area_m2': r(area_m2(polys[o['id']], ctx['mpp']), 2)}
                                               for o in sorted(members, key=lambda o: o['id'])],
                    'pairs': [p for p in pairs if p['a'] in ids], 'reserve_frac': r(shares.get('reserve', 0.)),
                    'lot_shares': shares, 'dominant': dominant, 'resolved_class': resolved, 'how': how, 'decided': decided})
        if gone:
            out[-1]['retired_by_reviewer'] = [{'id': i, 'kind': ctx['by_id'][i]['kind'], 'status': ctx['by_id'][i].get('status'),
                                               'decision': retired[i]} for i in gone]
            out[-1]['confirmed_classes_without_retire'] = sorted({o['kind'] for o in members if o.get('status') == 'confirmed'})
    return sorted(out, key=lambda g: g['group'])


def ends_context(ctx, exclude_ids, unioned_car_ids, design=DESIGN):
    def prepped(ids_kinds, buffer_px=0.):
        geoms = [ctx['polys'][o['id']] for o in ids_kinds]
        if not geoms:
            return None
        u = unary_union(geoms)
        return prep(u.buffer(buffer_px) if buffer_px else u)
    halo = .15/ctx['mpp']
    lin = [o for k in ('driveway', 'footpath') for o in ctx['by_kind'].get(k, []) if o['id'] not in exclude_ids]
    return {'tree': prepped(ctx['by_kind'].get('tree', []), halo), 'linear': prepped(lin),
            'car': prepped([o for o in ctx['by_kind'].get('car', []) if o['id'] not in unioned_car_ids], halo),
            'building': prepped(ctx['by_kind'].get('building', []))}


def analyse_seed(seed, ctx, groups_by_member, adjacency_m, segment_tol_m, design=DESIGN):
    """Steps 2-5 up to the sections (the part that does not depend on width percentile, offset source or IoU gate)."""
    mpp, polys = ctx['mpp'], ctx['polys']
    d = {**design, 'adjacency_m': adjacency_m, 'segment_tol_m': segment_tol_m}
    res = {'seed': seed['id'], 'seed_status': seed.get('status'), 'seed_kind': seed['kind']}
    group = groups_by_member.get(seed['id'])
    res['group'] = group['group'] if group else None
    twins = [m for m in (group['members'] if group else []) if m['id'] != seed['id']]
    # gather (P4): same-mask siblings within gather_m, absorbable twins
    key = round(float(seed.get('score') or 0.), d['score_round'])
    seed_poly = polys[seed['id']]
    near = seed_poly.buffer(d['gather_m']/mpp)
    siblings = sorted(o['id'] for o in ctx['by_kind'].get(seed['kind'], []) if o['id'] != seed['id']
                      and round(float(o.get('score') or 0.), d['score_round']) == key and near.intersects(polys[o['id']]))
    absorbable_twins = [m['id'] for m in twins if not m['decided']]
    raw = unary_union([seed_poly, *[polys[i] for i in siblings], *[polys[i] for i in absorbable_twins]])
    shares, dominant = lot_shares(fill_holes(raw), ctx['reserve'], ctx['lots'])
    res.update(siblings=siblings, twins=twins, lot_shares=shares, dominant=dominant,
               reserve_frac=r(shares.get('reserve', 0.)) if ctx['reserve'] is not None else None)
    clip = None
    if ctx['reserve'] is not None and shares.get('reserve', 0.) >= .5:
        clip = ctx['reserve'].buffer(d['reserve_clip_m']/mpp)
    modal = polygonal(raw.intersection(clip)) if clip is not None else raw
    if modal is None:
        res['exit'] = 'empty_band'
        return res
    filled = fill_holes(modal)
    # amodal fill (P2): cars within the adjacency buffer (and >= car_reserve_frac inside the clip), never trees
    cars = []
    for o in ctx['by_kind'].get('car', []):
        cp = polys[o['id']]
        if cp.distance(filled) > d['adjacency_m']/mpp:
            continue
        if clip is not None and cp.intersection(clip).area/cp.area < d['car_reserve_frac']:
            continue
        cars.append(o['id'])
    amodal = unary_union([filled, *[polys[i].buffer(d['adjacency_m']/mpp) for i in cars]]) if cars else filled
    amodal = fill_holes(amodal)
    if clip is not None:
        amodal = polygonal(amodal.intersection(clip))
    amodal = polygonal(amodal.intersection(ctx['frame'])) if amodal is not None else None
    if amodal is None:
        res['exit'] = 'empty_band'
        return res
    main = largest(amodal)
    res.update(modal=modal, filled=filled, amodal=main, amodal_all=amodal, clip=clip, cars_unioned=sorted(cars))
    # axis (P1/P3)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        corners = list(main.minimum_rotated_rectangle.exterior.coords)
    sides = [math.dist(corners[i], corners[i+1]) for i in range(4)] if len(corners) >= 5 else [0.]*4
    res['mrr_length_m'] = r(max(sides)*mpp, 2)
    if max(sides)*mpp < d['min_len_m']:
        res['axis'] = None
        res['length_m'] = r(max(sides)*mpp, 2)
        return res
    axis = mask_axis(main, mpp, d, frame_size=(ctx['W'], ctx['H']))
    res['axis'] = axis
    if axis is None:
        return res
    exclude = {seed['id'], *siblings, *[m['id'] for m in twins]}
    ectx = ends_context(ctx, exclude, set(cars), d)
    modal_filled = fill_holes(modal)
    candidates = []
    for kk in sorted(axis['chords']):
        segs = build_segments(axis['chords'][kk], ctx['medial'], mpp, d, pixels=axis['pixels'])
        if not segs:
            continue
        segs = extend_ends(segs, main, mpp)
        if len(segs) >= 2 and min((sg['t1']-sg['t0'])*mpp for sg in segs) < d['min_segment_m']:
            continue
        secs = sections_along(segs, main, modal_filled, ctx['reserve'], ectx, mpp, ctx['W'], ctx['H'], d)
        clean = [x['w'] for x in secs if not x['outside'] and x['clean']]
        quick = None
        if len(clean) >= d['min_clean_sections']:
            mids = [(x['lo']+x['hi'])/2 for x in secs if not x['outside'] and x['clean']]
            pts = axis_polyline(segs, [float(np.median(mids))]*len(segs), max_joint_px=15./mpp)
            band = LineString([tuple(q) for q in pts]).buffer(float(np.median(clean))/2/mpp, cap_style='flat', join_style='mitre', mitre_limit=3.)
            quick = iou(band.intersection(ctx['frame']), main)
        candidates.append({'k': len(segs), 'chord_k': kk, 'segments': segs, 'sections': secs, 'quick_iou': quick})
    if not candidates:
        res['axis'] = None
        return res
    best = max((c['quick_iou'] or 0.) for c in candidates)
    chosen = next(c for c in candidates if (c['quick_iou'] or 0.) >= best-d['k_iou_slack'])
    res['axis_candidates'] = [{'k': c['k'], 'chord_k': c['chord_k'], 'quick_iou': r(c['quick_iou'])} for c in candidates]
    res['segments'] = chosen['segments']
    res['sections'] = chosen['sections']
    res['exclude'] = exclude
    return res


def build_band(res, ctx, width_pct, offset_source, design=DESIGN):
    """Steps 5-6 from an analysed seed: width, offsets, band polygon and its app ring. Returns a dict (band None on failure)."""
    mpp = ctx['mpp']
    secs = [s for s in res['sections'] if not s['outside']]
    clean = [s for s in secs if s['clean']]
    out = {'sections_n': len(res['sections']), 'sections_inside': len(secs), 'sections_clean': len(clean),
           'section_ends': dict(Counter(e for s in secs for e in s['ends']))}
    if len(clean) < design['min_clean_sections']:
        out['band'] = None
        out['fail'] = 'few_sections'
        return out
    widths = np.asarray([s['w'] for s in clean])
    out['width_clean_median_m'] = r(np.median(widths), 2)
    out['width_clean_cv'] = r(cv_of(widths))
    width = float(np.percentile(widths, width_pct))
    out['width_raw_m'] = r(width, 2)
    rw = [s['w_reserve'] for s in secs if s.get('w_reserve')]
    cap = design['reserve_cap_ratio']*float(np.median(rw)) if (rw and res.get('clip') is not None) else None
    out['reserve_width_m'] = r(np.median(rw), 2) if rw else None
    out['width_cap_m'] = r(cap, 2)
    if cap is not None and width > cap:
        width = cap
        out['width_capped'] = True
    out['width_m'] = r(width, 2)
    segs = res['segments']
    all_mid = np.median([(s['lo']+s['hi'])/2 for s in clean])
    offsets, sources = [], []
    for k, seg in enumerate(segs):
        mids = [(s['lo']+s['hi'])/2 for s in clean if s['seg'] == k]
        mask_off = float(np.median(mids)) if len(mids) >= 3 else float(all_mid)
        if offset_source == 'reserve' and seg['snapped'] and seg['reserve_pt'] is not None:
            offsets.append(float((seg['reserve_pt']-seg['p']) @ normal(seg['d'])))
            sources.append('reserve')
        else:
            offsets.append(mask_off)
            sources.append('mask')
        seg_mask_off = mask_off
        if seg['reserve_pt'] is not None:
            out.setdefault('mask_offset_from_reserve_m', []).append(
                r((seg_mask_off-float((seg['reserve_pt']-seg['p']) @ normal(seg['d'])))*mpp, 2))
    out['offset_sources'] = sources
    pts = axis_polyline(segs, offsets, max_joint_px=15./mpp)
    out['axis_px'] = [[r(p[0], 2), r(p[1], 2)] for p in pts]
    line = LineString([tuple(p) for p in pts])
    return finish_band(out, line, width, res, ctx, design)


def finish_band(out, line, width, res, ctx, design=DESIGN):
    """Step 6 on a given axis line and width: buffer (flat caps, mitre joins), clip to the frame and the reserve + clip
    tolerance, largest part, simplify, round, the app's ring rules, make_valid, vertex cap. Fills `out` and returns it."""
    mpp = ctx['mpp']
    out['axis_px'] = [[r(x, 2), r(y, 2)] for x, y in line.coords]
    out['axis_length_m'] = r(line.intersection(ctx['frame']).length*mpp, 2)
    band = line.buffer(width/2/mpp, cap_style='flat', join_style='mitre', mitre_limit=3.)
    band = band.intersection(ctx['frame'])
    if res.get('clip') is not None:
        band = band.intersection(res['clip'])
    band = polygonal(band)
    if band is None or band.area <= 0:
        out['band'] = None
        out['fail'] = 'empty_band'
        return out
    parts = sorted(iter_polygons(band), key=lambda p: -p.area)
    keep_share = parts[0].area/band.area
    out['multipart_keep_share'] = r(keep_share)
    if keep_share < design['multipart_keep']:
        out['band'] = None
        out['fail'] = 'multipart'
        return out
    poly = Polygon(parts[0].exterior)
    out['band_holes_dropped'] = len(parts[0].interiors)
    poly = poly.simplify(design['simplify_m']/mpp, preserve_topology=True)
    ring = [[min(max(round(float(x), design['ring_decimals']), 0.), float(ctx['W'])),
             min(max(round(float(y), design['ring_decimals']), 0.), float(ctx['H']))] for x, y in poly.exterior.coords[:-1]]
    ring, _, issues, repairs = finalise_app_ring(ring, [], ctx['W'], ctx['H'], rules=APP_RING_RULES)
    out['ring'] = ring
    out['app_issues'] = issues
    out['ring_repairs'] = repairs
    final = Polygon(ring) if len(ring) >= 3 else None
    if final is None or not final.is_valid:
        fixed = largest(make_valid(final)) if final is not None else None
        out['made_valid'] = True
        final = fixed
    if issues or final is None or final.area <= 0:
        out['band'] = None
        out['fail'] = 'app_ring_rules'
        return out
    out['band'] = final
    out['vertices'] = len(ring)
    if len(ring) > design['max_band_vertices']:
        out['fail'] = 'vertex_cap'
    if width < design['min_width_m']:
        out['fail'] = 'narrow'
    return out


def trim_for_blockers(res, band_out, metrics, ctx, design=DESIGN):
    """Decided pieces and hard blockers stop the band: when every blocking overlap lies within end_trim_max_m of a band end
    (along the axis), that end moves inward past the blocker (+ 0.2 m) and the band is rebuilt at the same width. Returns
    (band_out, metrics, trims) or None when a blocker sits mid-band."""
    from shapely.ops import substring
    mpp = ctx['mpp']
    blockers = metrics['blockers_hard']+metrics['blockers_decided']
    if not blockers:
        return None
    line = LineString(band_out['axis_px'])
    band = band_out['band']
    proj = [line.project(Point(c)) for c in band.exterior.coords]
    s0, s1 = min(proj), max(proj)
    lim, pad = design['end_trim_max_m']/mpp, .2/mpp
    new0, new1, trims = s0, s1, []
    for b in blockers:
        ov = ctx['polys'][b['id']].intersection(band)
        bp = [line.project(Point(c)) for poly in iter_polygons(ov) for c in poly.exterior.coords]
        if not bp:
            continue
        b0, b1 = min(bp), max(bp)
        if b1-s0 <= lim:
            new0 = max(new0, b1+pad)
            trims.append({'end': 'start', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r((b1+pad-s0)*mpp, 2)})
        elif s1-b0 <= lim:
            new1 = min(new1, b0-pad)
            trims.append({'end': 'end', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r((s1-b0+pad)*mpp, 2)})
        else:
            return None
    if new1-new0 <= 0:
        return None
    out = {k: v for k, v in band_out.items() if k not in ('band', 'ring', 'fail', 'vertices', 'app_issues', 'ring_repairs', 'made_valid')}
    out = finish_band(out, substring(line, new0, new1), band_out['width_m'], res, ctx, design)
    if out.get('band') is None:
        return None
    out['trimmed_ends'] = trims
    return out, band_metrics(res, out, ctx, design), trims


def band_metrics(res, band_out, ctx, design=DESIGN):
    """Metrics of a band against the seed's masks, cars, buildings and the reserve; the guard checks (blockers)."""
    mpp, band = ctx['mpp'], band_out['band']
    m = {'iou_modal': r(iou(band, res['modal'])), 'iou_amodal': r(iou(band, res['amodal'])),
         'band_area_m2': r(area_m2(band, mpp), 2)}
    if ctx['reserve'] is not None:
        m['outside_reserve_m2'] = r(area_m2(band.difference(ctx['reserve']), mpp), 2)
    cars = []
    car_area = car_under = 0.
    for o in ctx['by_kind'].get('car', []):
        cp = ctx['polys'][o['id']]
        if not cp.intersects(band):
            continue
        cover = cp.intersection(band).area/cp.area
        if cover >= .5:
            c = np.asarray(cp.centroid.coords[0])
            ax = LineString(band_out['axis_px'])
            lateral = ax.distance(Point(c))*mpp
            cars.append({'id': o['id'], 'status': o.get('status'), 'cover': r(cover),
                         'where': 'parked' if lateral > band_out['width_m']/2-3. else 'in_lane', 'lateral_m': r(lateral, 2)})
    for cid in res['cars_unioned']:
        cp = ctx['polys'][cid]
        car_area += cp.area
        car_under += cp.intersection(band).area
    m['cars_under'] = cars
    m['cars_under_n'] = len(cars)
    m['cars_under_parked_n'] = sum(1 for c in cars if c['where'] == 'parked')
    m['unioned_car_area_under_share'] = r(car_under/car_area) if car_area else None
    trees = [o['id'] for o in ctx['by_kind'].get('tree', []) if ctx['polys'][o['id']].intersects(band)]
    m['trees_under_n'] = len(trees)
    b_area = sum(ctx['polys'][o['id']].intersection(band).area for o in ctx['by_kind'].get('building', []))
    m['building_overlap_m2'] = r(b_area*mpp*mpp, 3)
    # guards
    hard, decided_hits, soft = [], [], []
    for o in ctx['live']:
        if o['id'] not in ctx['polys'] or o['id'] in res['exclude'] or o['kind'] in design['under_kinds']:
            continue
        p = ctx['polys'][o['id']]
        if not p.intersects(band):
            continue
        ov = p.intersection(band).area*mpp*mpp
        if ov <= 0:
            continue
        if o['kind'] in design['hard_blockers'] and ov > design['blocker_eps_m2']:
            hard.append({'id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': r(ov, 2)})
        elif is_decided(o, design) and o['kind'] != res['seed_kind'] and ov > design['blocker_eps_m2']:
            decided_hits.append({'id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': r(ov, 2)})
        elif is_decided(o, design) and o['kind'] == res['seed_kind'] and ov > design['blocker_eps_m2']:
            # a decided road of ANOTHER mask (not a sibling): decided pieces block
            decided_hits.append({'id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': r(ov, 2)})
        elif o['kind'] in design['soft_blockers'] and ov > design['soft_eps_m2'] and ov/(p.area*mpp*mpp) > design['soft_share']:
            soft.append({'id': o['id'], 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': r(ov, 2),
                         'share_of_object': r(ov/(p.area*mpp*mpp))})
    m['blockers_hard'] = hard
    m['blockers_decided'] = decided_hits
    m['blockers_soft'] = soft
    return m


def seed_order(ctx, groups, design=DESIGN):
    """Seeds: every live road (largest filled area first, then id), then twin groups resolved 'road' that have no road member
    (their largest member seeds the corridor)."""
    roads = sorted(ctx['by_kind'].get('road', []), key=lambda o: (-ctx['polys'][o['id']].area, o['id']))
    seeds = list(roads)
    road_ids = {o['id'] for o in roads}
    for g in groups:
        if g['resolved_class'] == 'road' and not any(m['id'] in road_ids for m in g['members']):
            members = [ctx['by_id'][m['id']] for m in g['members'] if not m['decided']]
            if members:
                seeds.append(max(members, key=lambda o: (ctx['polys'][o['id']].area, o['id'])))
    return seeds


def gate(res, band_out, metrics, iou_gate, design=DESIGN):
    """Step 7 + the guard exits. Returns (reasons in a fixed order, first reason or None)."""
    reasons = []
    length = band_out.get('axis_length_m') if band_out else None
    if length is None:
        length = res.get('length_m') or (res['axis']['length_m'] if res.get('axis') else 0.)
    if (length or 0.) < design['min_len_m']:
        reasons.append('short')
    if res.get('axis') and res['axis']['junction_spurs'] > 1:
        reasons.append('junction')
    if metrics and (metrics['iou_amodal'] or 0.) < iou_gate:
        reasons.append('low_iou')
    if band_out and band_out.get('width_clean_cv') is not None and band_out['width_clean_cv'] > design['cv_gate']:
        reasons.append('high_cv')
    if res.get('reserve_frac') is not None and res['reserve_frac'] < design['reserve_road_frac']:
        reasons.append('reserve_fraction')
    if res.get('dominant') in design['lot_exit_classes']:
        reasons.append('lot_class')
    if band_out and band_out.get('fail'):
        reasons.append(band_out['fail'])
    if metrics:
        if metrics['blockers_hard']:
            reasons.append('covers_blocker')
        if metrics['blockers_decided']:
            reasons.append('covers_decided')
        if metrics['blockers_soft']:
            reasons.append('covers_other_class')
    return reasons, (reasons[0] if reasons else None)


def corridor_layer(objects, *, width, height, mpp, reserve=None, lots=None, design=DESIGN, affine=None, cache=None,
                   layer_ctx=None, image=None, xwin=None, retired=None):
    """Steps 1-7 on one layer (pure: nothing written, the input objects are not modified). Returns a report dict with
    proposals (scene-object-like dicts, status 'proposal', never applied), exits, twin groups and counts. image / xwin /
    retired are read by v3 only (the frame for the kerb profiles; the cross-window overrides; the reviewer-retired twins of
    the --retire option, {object id: decision})."""
    if design.get('generation', 1) >= 3:
        return corridor_layer_v3(objects, width=width, height=height, mpp=mpp, reserve=reserve, lots=lots, design=design,
                                 affine=affine, cache=cache, layer_ctx=layer_ctx, image=image, xwin=xwin, retired=retired)
    if retired:
        raise ValueError('retired twins (--retire) are read by design v3 / v3.1 only')
    if design.get('generation', 1) >= 2:
        return corridor_layer_v2(objects, width=width, height=height, mpp=mpp, reserve=reserve, lots=lots, design=design,
                                 affine=affine, cache=cache, layer_ctx=layer_ctx)
    ctx = layer_ctx or layer_context(objects, mpp, width, height, reserve, lots or {}, design)
    groups = arbitrate(ctx, design)
    groups_by_member = {m['id']: g for g in groups for m in g['members']}
    seeds = seed_order(ctx, groups, design)
    consumed, proposals, exits, accepted = {}, [], [], []
    cache = {} if cache is None else cache
    for seed in seeds:
        if seed['id'] in consumed:
            continue
        group = groups_by_member.get(seed['id'])
        pre = None
        if group is not None and group['resolved_class'] is None and group['how'].startswith('conflict'):
            pre = 'p0_conflict'
        elif group is not None and group['resolved_class'] not in (None, 'road') and group['how'].startswith('confirmed'):
            pre = 'p0_decided_other_class'
        elif group is not None and group['resolved_class'] == 'driveway':
            pre = 'p0_driveway'
        key = (seed['id'], design['adjacency_m'], design['segment_tol_m'], design['snap_deg'])
        if key not in cache:
            cache[key] = analyse_seed(seed, ctx, groups_by_member, design['adjacency_m'], design['segment_tol_m'], design)
            cache[key]['_key'] = key
        res = cache[key]
        record = {'seed': seed['id'], 'seed_kind': seed['kind'], 'seed_status': seed.get('status'),
                  'seed_area_m2': r(area_m2(ctx['polys'][seed['id']], mpp), 2), 'group': res.get('group'),
                  'reserve_frac': res.get('reserve_frac'), 'lot_shares': res.get('lot_shares'), 'dominant': res.get('dominant'),
                  'siblings': res.get('siblings', []), 'cars_unioned': res.get('cars_unioned', []),
                  'vertices_before': len(seed.get('ring') or [])+sum(len(h) for h in seed.get('holes') or [])}
        if res.get('axis'):
            ax = res['axis']
            record.update(skeleton_length_m=r(ax['length_m'], 2), k=ax['k'], maxdev_m=ax['maxdev_m'],
                          junction_spurs=ax['junction_spurs'], spurs_ge_3m=[s for s in ax['spurs'] if s['protrusion_m'] >= design['junction_spur_m']],
                          snapped=[s['snapped'] for s in res['segments']], snap_angle_deg=[s['angle_deg'] for s in res['segments']],
                          segments=len(res['segments']), axis_candidates=res.get('axis_candidates'))
        band_out, metrics = None, None
        if res.get('exit'):
            reasons, first = [res['exit']], res['exit']
        elif pre is not None and not res.get('sections'):
            reasons, first = [pre], pre
        else:
            if res.get('sections'):
                band_out = build_band(res, ctx, design['width_pct'], design['offset_source'], design)
                if band_out.get('band') is not None:
                    metrics = band_metrics(res, band_out, ctx, design)
                    if metrics['blockers_hard'] or metrics['blockers_decided']:
                        trimmed = trim_for_blockers(res, band_out, metrics, ctx, design)
                        if trimmed is not None:
                            band_out, metrics, _ = trimmed
            if res.get('axis') is None and (res.get('mrr_length_m') or 0) >= design['min_len_m']:
                reasons, first = ['no_axis'], 'no_axis'
            else:
                reasons, first = gate(res, band_out, metrics, design['iou_gate'], design)
            if pre is not None:
                reasons, first = [pre, *reasons], pre
        if band_out:
            record.update({('band_ring' if k == 'ring' else k): v for k, v in band_out.items() if k != 'band'})
            modal_w = [s.get('w_modal') for s in res['sections'] if not s['outside'] and 'frame' not in s['ends'] and s.get('w_modal')]
            record['width_cv_before'] = r(cv_of(modal_w))
        if metrics:
            record.update(metrics)
        if metrics and band_out and band_out.get('band') is not None:
            overlaps = [{'proposal': q['proposal']['id'], 'seed': q['seed'], 'share': r(band_out['band'].intersection(qb).area/band_out['band'].area)}
                        for q, qb in accepted if band_out['band'].intersects(qb)]
            overlaps = [o for o in overlaps if o['share'] >= design['overlap_share']]
            if overlaps:
                record['overlaps'] = overlaps
                reasons = [*reasons, 'overlaps_proposal']
                first = first or 'overlaps_proposal'
        record['exit_reasons'] = reasons
        if first is not None:
            record['exit'] = first
            exits.append(record)
            continue
        band = band_out['band']
        # absorption (P4) and lineage
        absorbs, replaces, on_accept = [], [], []
        decided_seed = is_decided(seed, design)
        (on_accept if decided_seed else replaces).append(seed['id'])
        for sid in res['siblings']:
            o = ctx['by_id'][sid]
            absorbs.append({'id': sid, 'why': 'same-mask sibling', 'status': o.get('status'), 'area_m2': r(area_m2(ctx['polys'][sid], mpp), 3)})
            (on_accept if is_decided(o, design) else replaces).append(sid)
        for o in ctx['by_kind'].get('road', []):
            if o['id'] in replaces or o['id'] in on_accept or o['id'] in consumed:
                continue
            p = ctx['polys'][o['id']]
            cover = p.intersection(band).area/p.area if p.intersects(band) else 0.
            a = area_m2(p, mpp)
            same_mask = round(float(o.get('score') or 0.), design['score_round']) == round(float(seed.get('score') or 0.), design['score_round'])
            if cover >= design['absorb_cover'] or (a < design['speck_m2'] and p.distance(band) <= .5/mpp):
                if is_decided(o, design) and not same_mask:
                    continue                       # decided pieces of another mask are never absorbed (they block instead)
                why = 'speck' if a < design['speck_m2'] else 'road candidate inside the band'
                absorbs.append({'id': o['id'], 'why': why + (' (same mask)' if same_mask else ''), 'status': o.get('status'),
                                'area_m2': r(a, 3), 'cover': r(cover)})
                (on_accept if is_decided(o, design) else replaces).append(o['id'])
        twins = []
        group = groups_by_member.get(seed['id'])
        for m in (group['members'] if group else []):
            if m['id'] == seed['id']:
                continue
            action = ('kept: a decided twin keeps its class and geometry' if m['decided']
                      else f'absorbed: proposed {group["resolved_class"] or seed["kind"]} ({group["how"]})')
            twins.append({'id': m['id'], 'kind': m['kind'], 'status': m['status'], 'decided': m['decided'], 'action': action,
                          'iou_with_seed': r(iou(ctx['polys'][m['id']], ctx['polys'][seed['id']])),
                          'band_cover': r(ctx['polys'][m['id']].intersection(band).area/ctx['polys'][m['id']].area)})
            if not m['decided']:
                replaces.append(m['id'])
        lineage = sorted(set(replaces) | set(on_accept))
        pid = hashlib.sha256((VERSION+':'+','.join(lineage)).encode()).hexdigest()[:16]
        model = _ring_details([tuple(p) for p in band_out['ring']], [], np.asarray(affine, float), 'road') if affine is not None else {}
        seed_model = (seed.get('model') or {})
        if seed_model.get('color'):
            model['color'] = seed_model['color']
        proposal = {'id': pid, 'kind': 'road', 'label': 'Road', 'ring': band_out['ring'], 'status': 'proposal',
                    'source': VERSION, 'geometry_reviewed': False, 'score': seed.get('score'), 'model': model,
                    'replaces': sorted(set(replaces)), 'replaces_on_accept': sorted(set(on_accept)),
                    'absorbs': absorbs, 'xclass_twin': twins, 'derived_from': lineage,
                    'cars_under': [c['id'] for c in record.get('cars_under', [])], 'assumption': 'parking lane counts as road'}
        record['proposal'] = proposal
        record['vertices_after'] = len(band_out['ring'])
        band_secs = []
        for s in res['sections']:
            if s['outside'] or 'frame' in s['ends']:
                continue
            piece = containing(LineString([tuple(s['p']-s['n']*60./mpp), tuple(s['p']+s['n']*60./mpp)]).intersection(band), Point(s['p']), tol=60./mpp)
            if piece is not None:
                band_secs.append(piece.length*mpp)
        record['width_cv_after'] = r(cv_of(band_secs))
        for i in lineage:
            consumed[i] = pid
        proposals.append(record)
        accepted.append((record, band))
    counts = {'seeds': len(seeds), 'proposals': len(proposals), 'exits': dict(Counter(e['exit'] for e in exits)),
              'twin_groups': len(groups), 'twin_pairs': sum(len(g['pairs']) for g in groups),
              'twin_groups_by_class': dict(Counter(str(g['resolved_class']) for g in groups)),
              'specks_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'].startswith('speck')),
              'siblings_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'] == 'same-mask sibling'),
              'cars_under': sum(p.get('cars_under_n', 0) for p in proposals)}
    return {'design': {k: (list(v) if isinstance(v, tuple) else v) for k, v in design.items()}, 'counts': counts,
            'proposals': proposals, 'exits': exits, 'twin_groups': groups, 'assumption': ASSUMPTION}, ctx, cache


# === road corridor v2 (design name 'v2', 2026-10-07 afternoon) =============================================================
# v1 above is untouched: DESIGN and every v1 function are unchanged, so `--design v1` reproduces prototype/run-v1. v2 is a
# separate layer function (corridor_layer dispatches on design['generation']) that reuses the v1 geometry helpers. What v2
# changes, each from the by-eye skeptic (eye-tally-roads.md) or the code skeptic (skeptic/notes.md), numbered as in the ask:
#   1 slice      a band whose long edge runs along the frame edge (within slice_frame_tol_m over >= slice_frame_share of its
#                length), or whose width is under slice_street_ratio x the street width (median over stations of the
#                kerb-to-kerb extent of ALL road-like pieces: every live road, car and the seed's amodal mask and twins, closed
#                by street_close_m, clipped to the reserve + 0.5 m) exits 'slice' (#10, #21, #33).
#   2 car park   no reserve in the frame at all -> no band ('no_reserve'), or 'car_park' when car-density evidence holds
#                (>= car_park_min_cars cars within car_park_near_m of the seed, long axis >= car_park_perp_deg off the seed
#                axis, at least one pair side by side within car_park_pair_m); the same evidence on a large single-lot / CP / PC
#                site (single_lot_or_crown >= large_lot_m2) exits 'car_park'. ADDITION: the same evidence inside the reserve
#                exits 'car_park' too when the perpendicular cars outnumber the parallel ones (#15 and #31 lie 100 % inside the
#                parcel gap, so the parcel test alone misses them). Painted bay lines are NOT detected (no imagery here).
#   3 curved     one straight segment per band (max_segments 1); 'curved' when the kerb-side mask edge or the reserve edge
#                (verge width) deviates > curve_dev_m from the band edge (median over a curve_run_m run with >= curve_min_cover
#                of its stations measured), or the fitted axis is > curve_axis_deg off the reserve medial line (#5, #11, #30).
#   4 blockers   candidate footpath / vegetation outside the twin group: <= footpath_eps_m2 each under a band; more trims the
#                band edge back (<= edge_trim_max_m) or an end (<= end_trim_max_m), else 'covers_footpath'. Decided objects
#                of any class but car / tree block, INCLUDING decided P0 twins ('covers_decided'), unless the twin is the same
#                street (group reserve fraction >= retire_reserve_frac and IoU with the seed >= retire_iou): it is then listed
#                in 'retires_on_accept' (e31f's needs_adjustment driveway twin d5875542ec9e47a2) instead of being covered.
#   5 join       before fitting, collinear road pieces in the same reserve segment join the seed (P5: <= join_deg,
#                <= join_offset_m, the gap made of road / car / tree pixels only); one width per reserve segment (the median
#                of the pooled station widths of every band in the segment, second pass) (#7 / #32).
#   6 canopy     the same join carries a band through a tree gap to the next road piece on the axis; parts of the seed's own
#                mask beyond a canopy join the same way; nothing extends past the last road piece or the frame.
#   7 width      cars unioned UNBUFFERED (the buffer is the adjacency test only), seams closed by car_close_m; width = the
#                median of the clean kerb-to-kerb stations (p75 recorded); ends at a tree, the frame, another linear class, a
#                building, a car or a junction cut are dropped; when the two-kerb stations are a minority and one kerb shows
#                beside a canopy, width = 2 x the kerb's distance from the reserve medial line (mirrored), centred on it.
#   8 junction   a seed with a junction (two or more spurs, or one spur >= arm_spur_m) gets a band on its longest straight arm
#                (>= arm_min_m); the other arms stay modal ('remainder_on_accept'); 'junction' only when no arm qualifies.

VERSION_V2 = 'road-corridor-v2'
DESIGN_V2 = {
    **DESIGN, 'version': VERSION_V2, 'design_name': 'v2', 'generation': 2,
    'max_segments': 1, 'width_pct': 50, 'offset_source': 'reserve',
    'soft_blockers': ('driveway',),
    # 7 width
    'car_close_m': .3, 'drop_ends': ('tree', 'frame', 'linear', 'building', 'car', 'cut', 'bite'), 'kerb_ends': ('free', 'reserve_edge'),
    'bite_m': 1., 'bite_pct': 20, 'bite_min_ends': 5, 'bite_max_run_m': 10., 'verge_m': 1.,
    'mirror_max_two_kerb_share': .5, 'medial_along_m': .75,
    # 1 slice
    'slice_frame_tol_m': 1., 'slice_frame_share': .5, 'slice_frame_end_share': .5, 'slice_street_ratio': .75, 'street_close_m': .5,
    # 2 car park / no reserve
    'car_park_near_m': 3., 'car_park_perp_deg': 45., 'car_park_min_cars': 3, 'car_park_pair_m': 4., 'large_lot_m2': 2000.,
    # 3 curvature
    'curve_dev_m': 1., 'curve_run_m': 15., 'curve_axis_deg': 3., 'curve_min_cover': .6, 'curve_reserve_open_ratio': 1.5,
    # 4 blockers
    'footpath_kinds': ('footpath', 'vegetation'), 'footpath_eps_m2': 2., 'edge_trim_max_m': 1.,
    'retire_reserve_frac': .95, 'retire_iou': .9,
    # 5 / 6 join
    'join_deg': 10., 'join_offset_m': 1.5, 'join_gap_kinds': ('road', 'car', 'tree'), 'join_gap_other_share': .1,
    'join_gap_other_m2': 1., 'join_max_gap_m': 40., 'join_min_len_m': 3., 'join_reserve_frac': .8, 'segment_branch_min_m': 5.,
    'join_unlabelled_max_m': 5., 'join_corridor_pad_m': 1.,
    # 8 junction
    'arm_min_m': 20., 'arm_spur_m': 8., 'arm_strip_scale': 1.25, 'arm_strip_pad_m': .5,
}
DESIGNS = {'v1': DESIGN, 'v2': DESIGN_V2}
V2_EXITS = ('no_reserve', 'car_park', 'slice', 'curved', 'covers_footpath')


def mrr_axis(poly):
    """(centre, unit long direction (canonical sign), long side px, short side px) of a polygon's minimum rotated rectangle."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        rect = poly.minimum_rotated_rectangle
    c = np.asarray(rect.exterior.coords) if rect.geom_type == 'Polygon' else np.asarray(rect.coords)
    if len(c) < 3:
        b = poly.bounds
        c = np.asarray([[b[0], b[1]], [b[2], b[1]], [b[2], b[3]]])
    e1, e2 = c[1]-c[0], c[2]-c[1]
    l1, l2 = float(np.linalg.norm(e1)), float(np.linalg.norm(e2))
    d = unit(e1) if l1 >= l2 else unit(e2)
    if d[0] < 0 or (d[0] == 0 and d[1] < 0):
        d = -d
    centre = np.asarray(poly.centroid.coords[0], float)
    return centre, d, max(l1, l2), min(l1, l2)


def angle_between(d1, d2):
    return math.degrees(math.acos(min(1., abs(float(np.asarray(d1) @ np.asarray(d2))))))


def strip_rect(c, d, s0, s1, l0, l1):
    n = normal(d)
    return Polygon([tuple(c+d*s0+n*l0), tuple(c+d*s1+n*l0), tuple(c+d*s1+n*l1), tuple(c+d*s0+n*l1)])


def closing(geometry, dist_px):
    if geometry is None or geometry.is_empty or dist_px <= 0:
        return geometry
    out = polygonal(geometry.buffer(dist_px, join_style='mitre').buffer(-dist_px, join_style='mitre'))
    return out if out is not None and out.area > 0 else polygonal(geometry)


# --- 5: reserve segments ---------------------------------------------------------------------------------------------------

def reserve_segments(ctx, design=DESIGN_V2):
    """Label the reserve medial points by reserve segment: the medial skeleton is cut at its branch points (every point within
    half the local reserve width of a point with >= 3 neighbours), branches shorter than segment_branch_min_m are dropped, and
    collinear branches (<= join_deg, both centre offsets <= join_offset_m) merge into one segment. Returns labels (-1 = none)."""
    med, mpp = ctx['medial'], ctx['mpp']
    n = len(med)
    if n < 2:
        return np.full(n, -1)
    from scipy.spatial import cKDTree
    step = design['skel_m']/mpp
    tree = cKDTree(med[:, :2])
    pairs = tree.query_pairs(1.5*step, output_type='ndarray')
    deg = np.bincount(pairs.ravel(), minlength=n) if len(pairs) else np.zeros(n, int)
    cut = np.zeros(n, bool)
    for j in np.nonzero(deg >= 3)[0]:
        cut[tree.query_ball_point(med[j, :2], r=max(step, med[j, 2]/2/mpp))] = True
    keep = ~cut
    sel = pairs[keep[pairs[:, 0]] & keep[pairs[:, 1]]] if len(pairs) else pairs
    graph = coo_matrix((np.ones(len(sel)), (sel[:, 0], sel[:, 1])), shape=(n, n)) if len(sel) else coo_matrix((n, n))
    _, comp = connected_components(graph, directed=False)
    import cv2
    branches = []
    for c in sorted(set(comp[keep].tolist())):
        idx = np.nonzero((comp == c) & keep)[0]
        if len(idx)*design['skel_m'] < design['segment_branch_min_m']:
            continue
        vx, vy, cx, cy = cv2.fitLine(med[idx, :2].astype(np.float32), cv2.DIST_L2, 0, .01, .01).ravel()
        branches.append({'idx': idx, 'd': unit(np.array([vx, vy], float)), 'c': np.array([cx, cy], float)})
    parent = list(range(len(branches)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    lim = design['join_offset_m']/mpp
    for i, j in itertools.combinations(range(len(branches)), 2):
        a, b = branches[i], branches[j]
        if angle_between(a['d'], b['d']) <= design['join_deg'] and abs((b['c']-a['c']) @ normal(a['d'])) <= lim \
                and abs((a['c']-b['c']) @ normal(b['d'])) <= lim:
            parent[root(i)] = root(j)
    labels = np.full(n, -1)
    roots = sorted({root(i) for i in range(len(branches))})
    for i, br in enumerate(branches):
        labels[br['idx']] = roots.index(root(i))
    return labels


def segment_of(geometry, ctx, design=DESIGN_V2):
    """Reserve segment of a geometry: the majority label of the medial points inside it (+ 2 m), else the nearest labelled
    medial point within 10 m; None when there is none."""
    med, labels, mpp = ctx['medial'], ctx['segment_labels'], ctx['mpp']
    ok = labels >= 0
    if geometry is None or not ok.any():
        return None
    from shapely import contains_xy
    zone = geometry.buffer(2./mpp)
    inside = contains_xy(zone, med[:, 0], med[:, 1]) & ok
    if inside.any():
        vals = Counter(labels[inside].tolist())
        return int(max(sorted(vals), key=lambda k: vals[k]))
    c = np.asarray(geometry.centroid.coords[0])
    dist = np.linalg.norm(med[ok, :2]-c, axis=1)
    k = int(np.argmin(dist))
    return int(labels[ok][k]) if dist[k]*mpp <= 10. else None


# --- 5 / 6: join collinear road pieces across road / car / tree gaps --------------------------------------------------------

def join_pieces(base, direction, candidates, ctx, gap_union, design=DESIGN_V2, clip=None):
    """P5 join: candidates [(id, polygon)] join the base when the piece is >= join_min_len_m long, within join_deg of the base
    direction, both centre lines within join_offset_m, the gap along the axis <= join_max_gap_m, and the bridge lies
    >= join_reserve_frac inside the clip (reserve + 0.5 m). The gap: objects of any class other than road / car / tree (the
    linear-join blocker rule) may cover at most join_gap_other_m2 or join_gap_other_share of it; pixels no object covers (plain
    pavement SAM missed) are road-like only in gaps up to join_unlabelled_max_m, longer gaps must be road / car / tree pixels
    to the same tolerance. Greedy, repeated until nothing joins. Returns (union, joined ids, bridges, log)."""
    mpp = ctx['mpp']
    cur = base
    c0 = np.asarray(base.centroid.coords[0], float)
    d = unit(np.asarray(direction, float))
    n = normal(d)
    _, _, _, w0 = mrr_axis(base)
    joined, bridges, log = [], [], []
    changed = True
    while changed:
        changed = False
        for oid, poly in candidates:
            if oid in joined or poly is None or poly.is_empty:
                continue
            cb, db, lb, wb = mrr_axis(poly)
            if lb*mpp < design['join_min_len_m'] or angle_between(d, db) > design['join_deg']:
                continue
            off = max(abs((cb-c0) @ n), abs((c0-cb) @ normal(db)))*mpp
            if off > design['join_offset_m']:
                # 6: a piece of the same street seen beside a canopy (half its width) has its centre off the seed's; it still
                # continues the street when its centre lies inside the seed's lateral extent and its edges stay within
                # join_corridor_pad_m of it
                la = (np.asarray([c for g in iter_polygons(base) for c in g.exterior.coords], float)-c0) @ n
                lb_ = (np.asarray([c for g in iter_polygons(poly) for c in g.exterior.coords], float)-c0) @ n
                pad, mid = design['join_corridor_pad_m']/mpp, float((cb-c0) @ n)
                if not (la.min() <= mid <= la.max() and lb_.min() >= la.min()-1.5*pad and lb_.max() <= la.max()+1.5*pad):
                    continue
            ca = np.asarray(cur.exterior.coords if cur.geom_type == 'Polygon' else
                            [c for p in iter_polygons(cur) for c in p.exterior.coords], float)
            cpb = np.asarray([c for g in iter_polygons(poly) for c in g.exterior.coords], float)
            pa, pb = (ca-c0) @ d, (cpb-c0) @ d
            if pb.min() > pa.max():
                s0, s1 = float(pa.max()), float(pb.min())
            elif pb.max() < pa.min():
                s0, s1 = float(pb.max()), float(pa.min())
            else:
                s0 = s1 = None
            gap_m = 0. if s0 is None else (s1-s0)*mpp
            if gap_m > design['join_max_gap_m']:
                continue
            bridge, other_m2, gap_m2 = None, 0., 0.
            if s0 is not None and gap_m > 0:
                lc = ((cb-c0) @ n)/2
                hw = min(w0, wb)/2
                bridge = strip_rect(c0, d, s0-.5/mpp, s1+.5/mpp, lc-hw, lc+hw)
                g = bridge.difference(cur.union(poly))
                gap_m2 = area_m2(g, mpp)
                if gap_m2 > .05:
                    rest = g.difference(gap_union) if gap_union is not None else g
                    blockers = ctx.get('gap_blockers')
                    other_m2 = area_m2(rest.intersection(blockers), mpp) if blockers is not None else 0.
                    unlabelled_m2 = area_m2(rest, mpp)-other_m2
                    limit = max(design['join_gap_other_m2'], design['join_gap_other_share']*gap_m2)
                    why = None
                    if other_m2 > limit:
                        why = 'gap has other-class objects'
                    elif gap_m > design['join_unlabelled_max_m'] and other_m2+unlabelled_m2 > limit:
                        why = 'long gap not made of road / car / tree pixels'
                    if why:
                        log.append({'id': oid, 'joined': False, 'why': why, 'gap_m': r(gap_m, 2), 'gap_other_m2': r(other_m2, 2),
                                    'gap_unlabelled_m2': r(unlabelled_m2, 2)})
                        continue
                if clip is not None and bridge.intersection(clip).area/bridge.area < design['join_reserve_frac']:
                    log.append({'id': oid, 'joined': False, 'why': 'bridge outside the reserve', 'gap_m': r(gap_m, 2)})
                    continue
            joined.append(oid)
            parts = [cur, poly] + ([bridge] if bridge is not None else [])
            cur = polygonal(unary_union(parts))
            if bridge is not None:
                bridges.append(bridge)
            log.append({'id': oid, 'joined': True, 'gap_m': r(gap_m, 2), 'gap_m2': r(gap_m2, 2), 'gap_other_m2': r(other_m2, 2),
                        'angle_deg': r(angle_between(d, db), 2), 'offset_m': r(off, 2)})
            changed = True
    return cur, joined, bridges, log


# --- 2: car park evidence --------------------------------------------------------------------------------------------------

def car_park_evidence(poly, direction, ctx, design=DESIGN_V2):
    """Cars within car_park_near_m of the polygon: how many lie with their long axis >= car_park_perp_deg off the direction
    (perpendicular / angle bays) and how many perpendicular pairs stand side by side (along <= car_park_pair_m, lateral
    <= 3 m). Evidence = >= car_park_min_cars perpendicular and >= 1 side-by-side pair."""
    mpp = ctx['mpp']
    d = unit(np.asarray(direction, float))
    near = poly.buffer(design['car_park_near_m']/mpp)
    perp, para = [], 0
    for o in sorted(ctx['by_kind'].get('car', []), key=lambda o: o['id']):
        cp = ctx['polys'][o['id']]
        if not near.intersects(cp):
            continue
        cc, cd, cl, cw = mrr_axis(cp)
        if cl <= cw*1.15:
            continue                                   # square blob: no orientation
        if angle_between(d, cd) >= design['car_park_perp_deg']:
            perp.append((float(cc @ d), float(cc @ normal(d)), o['id']))
        else:
            para += 1
    perp.sort()
    pairs = sum(1 for a, b in zip(perp[:-1], perp[1:], strict=True)
                if (b[0]-a[0])*mpp <= design['car_park_pair_m'] and abs(b[1]-a[1])*mpp <= 3.)
    ok = len(perp) >= design['car_park_min_cars'] and pairs >= 1
    return {'perpendicular': len(perp), 'parallel': para, 'side_by_side_pairs': pairs, 'evidence': bool(ok),
            'cars': [p[2] for p in perp]}


def site_area_m2(poly, lots, cls, mpp):
    """Area of the connected part of the lot-class union that holds most of the polygon (a site; touching parcels of one
    class merge, which over-reads the site: recorded as a limitation)."""
    g = lots.get(cls)
    if g is None:
        return 0.
    best = max(iter_polygons(g), key=lambda p: p.intersection(poly).area, default=None)
    return area_m2(best, mpp) if best is not None and best.intersects(poly) else 0.


# --- 8: longest straight arm -----------------------------------------------------------------------------------------------

def longest_straight_run(pts, tol_px):
    """Longest run of consecutive points whose chord deviates <= tol_px (two pointers). Returns (i, j) or None."""
    n = len(pts)
    best, i = None, 0
    for j in range(1, n):
        while i < j-1 and seg_dist(pts[i+1:j], pts[i], pts[j]).max() > tol_px:
            i += 1
        if best is None or np.linalg.norm(pts[j]-pts[i]) > np.linalg.norm(pts[best[1]]-pts[best[0]]):
            best = (i, j)
    return best


def straight_arm(geometry, mpp, design=DESIGN_V2):
    """The longest straight run over the skeleton paths between the main path ends and the ends of spurs >= junction_spur_m.
    Returns {a, b (frame px), length_m, half_width_m} or None."""
    scale = mpp/design['skel_m']
    minx, miny, maxx, maxy = geometry.bounds
    x0, y0 = int(math.floor(minx*scale))-2, int(math.floor(miny*scale))-2
    w, h = int(math.ceil(maxx*scale))-x0+3, int(math.ceil(maxy*scale))-y0+3
    mask = raster(geometry, scale, x0, y0, w, h)
    if mask.sum() < 4:
        return None
    dt = ndimage.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
    skel = thin(mask)
    ys, xs, graph = skeleton_graph(skel)
    if len(ys) < 2:
        return None
    _, labels = connected_components(graph, directed=False)
    comp = Counter(labels.tolist()).most_common(1)[0][0]
    members = np.nonzero(labels == comp)[0]
    path, _ = longest_path(graph, members)
    sub = graph[members][:, members]
    deg = np.asarray((sub > 0).sum(axis=1)).ravel()
    ends = sorted({int(members[i]) for i in np.nonzero(deg == 1)[0]} | {path[0], path[-1]})
    if len(ends) > 12:
        dist = dijkstra(graph, indices=path, min_only=True)
        ends = sorted(sorted(ends, key=lambda e: -dist[e])[:10]+[path[0], path[-1]])
        ends = sorted(set(ends))
    dist_all, pred = dijkstra(graph, indices=ends, return_predecessors=True)
    tol = design['segment_tol_m']/design['skel_m']
    best = None
    for i, j in itertools.combinations(range(len(ends)), 2):
        if not np.isfinite(dist_all[i, ends[j]]):
            continue
        node, seq = ends[j], [ends[j]]
        while node != ends[i] and pred[i, node] >= 0:
            node = int(pred[i, node])
            seq.append(node)
        pts = np.c_[xs[seq], ys[seq]].astype(float)
        if len(pts) < 2:
            continue
        stepn = max(1, int(round(1./design['skel_m'])))
        sp = pts[::stepn] if len(pts) > 2*stepn else pts
        run = longest_straight_run(sp, tol)
        if run is None:
            continue
        a, b = sp[run[0]], sp[run[1]]
        length = float(np.linalg.norm(b-a))*design['skel_m']
        if best is None or length > best[0]:
            ia = int(np.argmin(np.linalg.norm(pts-a, axis=1)))
            ib = int(np.argmin(np.linalg.norm(pts-b, axis=1)))
            seg = pts[min(ia, ib):max(ia, ib)+1].astype(int)
            hw = float(np.median(dt[seg[:, 1], seg[:, 0]]))*design['skel_m']
            best = (length, a, b, hw)
    if best is None:
        return None
    length, a, b, hw = best
    return {'a': np.array([(a[0]+x0)/scale, (a[1]+y0)/scale]), 'b': np.array([(b[0]+x0)/scale, (b[1]+y0)/scale]),
            'length_m': length, 'half_width_m': hw}


# --- 7 / 1 / 3: sections with mirrored widths, street width and reserve edges -----------------------------------------------

def medial_offset(p, d, n, medial, mpp, design=DESIGN_V2):
    """Lateral offset (px, along n from p) of the reserve medial line at the station, or None."""
    if medial is None or len(medial) == 0:
        return None
    rel = medial[:, :2]-p
    along, lat = rel @ d, rel @ n
    sel = np.abs(along) <= design['medial_along_m']/mpp
    if not sel.any():
        return None
    k = int(np.argmin(np.abs(lat[sel])))
    return float(lat[sel][k])


def sections_along_v2(segs, amodal, modal, reserve, ends_ctx, mpp, width, height, design=DESIGN_V2, medial=None, street=None):
    """v1 sections_along plus: a 'cut' end (the junction arm strip edge where the mask goes on), the reserve piece's ends
    (r_lo, r_hi, r_open), the reserve medial offset and the mirrored width (one kerb beside a tree end), and the street width
    across all road-like pieces at the station."""
    step, reach, probe = design['section_step_m']/mpp, 60./mpp, design['section_probe_m']/mpp
    out = []
    reserve_edge = reserve.boundary.buffer(2.) if reserve is not None else None
    for k, seg in enumerate(segs):
        d, n = seg['d'], normal(seg['d'])
        count = int((seg['t1']-seg['t0'])//step)+1
        for i in range(count):
            t = seg['t0']+min(i*step, seg['t1']-seg['t0'])
            p = seg['p']+d*t
            if not (0 <= p[0] <= width and 0 <= p[1] <= height):
                continue
            pt = Point(p)
            line = LineString([tuple(p-n*reach), tuple(p+n*reach)])
            rec = {'seg': k, 't': float(t), 'p': p, 'n': n}
            piece = containing(line.intersection(amodal), pt)
            if piece is None:
                rec.update(outside=True, clean=False, ends=['outside', 'outside'])
                out.append(rec)
                continue
            offs = sorted(((float((np.asarray(c)-p) @ n), np.asarray(c)) for c in (piece.coords[0], piece.coords[-1])), key=lambda x: x[0])
            ends = []
            for side, (_, e) in enumerate(offs):
                outward = -1. if side == 0 else 1.
                beyond = Point(e+n*outward*probe)
                if e[0] <= 1 or e[1] <= 1 or e[0] >= width-1 or e[1] >= height-1:
                    ends.append('frame')
                elif ends_ctx['tree'] is not None and ends_ctx['tree'].contains(beyond):
                    ends.append('tree')
                elif ends_ctx.get('cut') is not None and ends_ctx['cut'][0].contains(Point(e)) and ends_ctx['cut'][1].contains(beyond):
                    ends.append('cut')
                elif ends_ctx['linear'] is not None and ends_ctx['linear'].contains(beyond):
                    ends.append('linear')
                elif ends_ctx['car'] is not None and ends_ctx['car'].contains(beyond):
                    ends.append('car')
                elif ends_ctx['building'] is not None and ends_ctx['building'].contains(beyond):
                    ends.append('building')
                elif reserve_edge is not None and reserve_edge.contains(Point(e)):
                    ends.append('reserve_edge')
                else:
                    ends.append('free')
            rec.update(outside=False, lo=offs[0][0], hi=offs[1][0], w=(offs[1][0]-offs[0][0])*mpp, ends=ends,
                       clean=not any(e in design['drop_ends'] for e in ends))
            mp = containing(line.intersection(modal), pt) if modal is not None else None
            rec['w_modal'] = mp.length*mpp if mp is not None else None
            rp = containing(line.intersection(reserve), pt) if reserve is not None else None
            rec['w_reserve'] = rp.length*mpp if rp is not None else None
            if rp is not None:
                rends = sorted(((float((np.asarray(c)-p) @ n), np.asarray(c)) for c in (rp.coords[0], rp.coords[-1])), key=lambda x: x[0])
                fr = [bool(e[0] <= 1 or e[1] <= 1 or e[0] >= width-1 or e[1] >= height-1) for _, e in rends]
                rec.update(r_lo=rends[0][0], r_hi=rends[1][0], r_frame=any(fr))
                # a mask end within verge_m of a (non-frame) reserve boundary fills the verge to the property line: it is not a
                # kerb ('verge'); it stays in the width as in v1 but is no kerb evidence for the curvature test
                for side in (0, 1):
                    if ends[side] in ('free', 'reserve_edge') and not fr[side] and abs(offs[side][0]-rends[side][0])*mpp <= design['verge_m']:
                        ends[side] = 'verge'
            rec['medial_off'] = medial_offset(p, d, n, medial, mpp, design)
            if street is not None:
                sp = containing(line.intersection(street), pt)
                rec['w_street'] = sp.length*mpp if sp is not None else None
            out.append(rec)
    mark_bites(out, mpp, design)
    return out


def mark_bites(secs, mpp, design=DESIGN_V2):
    """7 (addition): a SHORT run (<= bite_max_run_m along) of kerb-class ends lying more than bite_m inside their side's
    outer kerb line (the bite_pct outward percentile of that side's kerb-class ends on the segment) is a bite of the mask (a
    notch, a canopy edge that is not a tree object, a patch of shadow), not a kerb: those ends become 'bite' and their stations
    are not clean. Longer inward runs are kept: a curve or a real narrowing shows there. Then the mirrored width: one kerb end
    beside a tree end, doubled about the reserve medial line."""
    kerb = design['kerb_ends']
    for k in sorted({s['seg'] for s in secs}):
        ss = sorted((s for s in secs if s['seg'] == k and not s['outside']), key=lambda s: s['t'])
        for side in (0, 1):
            ks = [s for s in ss if s['ends'][side] in kerb]
            if len(ks) < design['bite_min_ends']:
                continue
            vals = [(s['lo'] if side == 0 else s['hi']) for s in ks]
            outer = float(np.percentile(vals, design['bite_pct'] if side == 0 else 100-design['bite_pct']))
            flag = [((s['lo']-outer) if side == 0 else (outer-s['hi']))*mpp > design['bite_m'] for s in ks]
            i = 0
            while i < len(ks):
                if not flag[i]:
                    i += 1
                    continue
                j = i
                while j+1 < len(ks) and flag[j+1]:
                    j += 1
                if (ks[j]['t']-ks[i]['t'])*mpp+design['section_step_m'] <= design['bite_max_run_m']:
                    for s in ks[i:j+1]:
                        s['ends'] = [('bite' if q == side else e) for q, e in enumerate(s['ends'])]
                        s['clean'] = False
                i = j+1
    for s in secs:
        # mirror only about a medial line of a reserve the frame does not cut, and only when the visible part ends short of
        # the far side of the medial line (the mirrored street is at least as wide as what is seen)
        if s['outside'] or s.get('medial_off') is None or s['ends'].count('tree') != 1 or s.get('r_frame'):
            continue
        side = 0 if s['ends'][1] == 'tree' else 1
        if s['ends'][side] in design['drop_ends']:
            continue
        kerb_off = s['lo'] if side == 0 else s['hi']
        if (kerb_off-s['medial_off'])*(1 if side == 1 else -1) > 0 and 2*abs(kerb_off-s['medial_off'])*mpp >= s['w']:
            s['w_mirror'] = 2*abs(kerb_off-s['medial_off'])*mpp


# --- v2 seed analysis ------------------------------------------------------------------------------------------------------

def analyse_seed_v2(seed, ctx, groups_by_member, joined, bridges, design=DESIGN_V2):
    """v1 analyse_seed with: joined pieces and their bridges in the mask (5 / 6), cars unioned unbuffered and seams closed (7),
    parts of the mask beyond a canopy joined (6), the junction arm restriction (8), sections_along_v2 (mirror, street width,
    reserve edges)."""
    mpp, polys = ctx['mpp'], ctx['polys']
    d = design
    res = {'seed': seed['id'], 'seed_status': seed.get('status'), 'seed_kind': seed['kind'], 'joined': list(joined)}
    group = groups_by_member.get(seed['id'])
    res['group'] = group['group'] if group else None
    twins = [m for m in (group['members'] if group else []) if m['id'] != seed['id']]
    key = round(float(seed.get('score') or 0.), d['score_round'])
    seed_poly = polys[seed['id']]
    near = seed_poly.buffer(d['gather_m']/mpp)
    siblings = sorted(o['id'] for o in ctx['by_kind'].get(seed['kind'], []) if o['id'] != seed['id'] and o['id'] not in joined
                      and round(float(o.get('score') or 0.), d['score_round']) == key and near.intersects(polys[o['id']]))
    absorbable_twins = [m['id'] for m in twins if not m['decided']]
    raw = unary_union([seed_poly, *[polys[i] for i in siblings], *[polys[i] for i in absorbable_twins], *[polys[i] for i in joined]])
    shares, dominant = lot_shares(fill_holes(raw), ctx['reserve'], ctx['lots'])
    res.update(siblings=siblings, twins=twins, lot_shares=shares, dominant=dominant,
               reserve_frac=r(shares.get('reserve', 0.)) if ctx['reserve'] is not None else None)
    clip = None
    if ctx['reserve'] is not None and shares.get('reserve', 0.) >= .5:
        clip = ctx['reserve'].buffer(d['reserve_clip_m']/mpp)
    modal = polygonal(raw.intersection(clip)) if clip is not None else raw
    if modal is None:
        res['exit'] = 'empty_band'
        return res
    filled = fill_holes(modal)
    cars = []
    for o in ctx['by_kind'].get('car', []):
        cp = polys[o['id']]
        if cp.distance(filled) > d['adjacency_m']/mpp:             # the buffer is the adjacency test only
            continue
        if clip is not None and cp.intersection(clip).area/cp.area < d['car_reserve_frac']:
            continue
        cars.append(o['id'])
    amodal = unary_union([filled, *[polys[i] for i in cars], *bridges])  # UNBUFFERED cars
    amodal = fill_holes(closing(amodal, d['car_close_m']/mpp) or amodal)
    if clip is not None and amodal is not None:
        amodal = polygonal(amodal.intersection(clip))
    amodal = polygonal(amodal.intersection(ctx['frame'])) if amodal is not None else None
    if amodal is None:
        res['exit'] = 'empty_band'
        return res
    parts = sorted(iter_polygons(amodal), key=lambda p: (-p.area, p.centroid.x, p.centroid.y))
    main = parts[0]
    part_joins = []
    if len(parts) > 1:
        _, pdir, _, _ = mrr_axis(main)
        cand = [(f'part-{i}', p) for i, p in enumerate(parts[1:], 1)]
        gap_union = ctx.get('gap_union')
        main_j, pj, pbr, plog = join_pieces(main, pdir, cand, ctx, gap_union, d, clip=clip)
        if pj:
            main = largest(main_j)
            part_joins = plog
    res.update(modal=modal, filled=filled, amodal=main, amodal_all=amodal, clip=clip, cars_unioned=sorted(cars), part_joins=part_joins)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        corners = list(main.minimum_rotated_rectangle.exterior.coords)
    sides = [math.dist(corners[i], corners[i+1]) for i in range(4)] if len(corners) >= 5 else [0.]*4
    res['mrr_length_m'] = r(max(sides)*mpp, 2)
    if max(sides)*mpp < d['min_len_m']:
        res['axis'] = None
        res['length_m'] = r(max(sides)*mpp, 2)
        return res
    spur_ctx = None
    if d.get('car_spur_rule'):           # v3.2 (car spur); DESIGN_V2 / V3 / V31 have no such key
        road_ids = [seed['id'], *siblings, *[m['id'] for m in twins], *joined]
        spur_ctx = {'cars': unary_union([polys[i] for i in cars]) if cars else None,
                    'car_polys': {i: polys[i] for i in sorted(cars)}, 'road': unary_union([polys[i] for i in road_ids])}
    axis = mask_axis(main, mpp, d, frame_size=(ctx['W'], ctx['H']), spur_ctx=spur_ctx)
    if axis is None:
        res['axis'] = None
        return res
    # 8: a junction (two spur clusters, or one long arm) -> the longest straight arm, the rest stays modal
    car_index = set(axis.get('car_spur_index') or [])
    big = [s for i, s in enumerate(axis['spurs']) if s['protrusion_m'] >= d['junction_spur_m'] and i not in car_index]
    is_junction = axis['junction_spurs'] > 1 or any(s['protrusion_m'] >= d['arm_spur_m'] for s in big)
    res['junction'] = {'spur_clusters': axis['junction_spurs'], 'spurs_ge_3m': len(big), 'is_junction': bool(is_junction)}
    if spur_ctx is not None:
        res['junction'].update(car_spurs=axis['car_spurs'], spur_tests=axis['spur_tests'])
    cut = None
    if is_junction:
        arm = straight_arm(main, mpp, d)
        res['junction']['arm_m'] = r(arm['length_m'], 2) if arm else None
        if arm is None or arm['length_m'] < d['arm_min_m']:
            res['axis'] = axis
            res['junction_exit'] = True
            return res
        ad = unit(arm['b']-arm['a'])
        hw = (d['arm_strip_scale']*arm['half_width_m']+d['arm_strip_pad_m'])/mpp
        strip = strip_rect(arm['a'], ad, -4000., 4000.+float(np.linalg.norm(arm['b']-arm['a'])), -hw, hw)
        restricted = polygonal(main.intersection(strip))
        restricted = largest(restricted) if restricted is not None else None
        if restricted is None or area_m2(restricted, mpp) < 5.:
            res['axis'] = axis
            res['junction_exit'] = True
            return res
        res['junction'].update(arm_a=[r(v, 1) for v in arm['a']], arm_b=[r(v, 1) for v in arm['b']], strip_half_width_m=r(hw*mpp, 2))
        cut = (prep(strip.boundary.buffer(.2/mpp)), prep(main))
        res['amodal_full'] = main
        main = restricted
        res['amodal'] = main
        axis = mask_axis(main, mpp, d, frame_size=(ctx['W'], ctx['H']))
        if axis is None:
            res['axis'] = None
            return res
        axis['junction_spurs'] = 0
    res['axis'] = axis
    # section ends ignore every twin (the same surface); the guards ignore only the undecided ones (4: decided twins block
    # unless they retire on accept)
    ectx = ends_context(ctx, {seed['id'], *siblings, *[m['id'] for m in twins], *joined}, set(cars), d)
    exclude = {seed['id'], *siblings, *[m['id'] for m in twins if not m['decided']], *joined}
    ectx['cut'] = cut
    modal_filled = fill_holes(modal)
    street = street_union(ctx, main, [polys[m['id']] for m in twins], clip, d)
    res['street'] = street
    segs = build_segments(axis['chords'][1], ctx['medial'], mpp, d, pixels=axis['pixels'])
    if not segs:
        res['axis'] = None
        return res
    segs = extend_ends(segs, main, mpp)
    secs = sections_along_v2(segs, main, modal_filled, ctx['reserve'], ectx, mpp, ctx['W'], ctx['H'], d, medial=ctx['medial'], street=street)
    res['axis_candidates'] = [{'k': 1, 'chord_k': 1}]
    res['segments'] = segs
    res['sections'] = secs
    res['exclude'] = exclude
    return res


def street_union(ctx, main, extra, clip, design=DESIGN_V2):
    """All road-like pieces of the layer (every live road and car, the seed's amodal mask and its twins), closed by
    street_close_m and clipped to the reserve + 0.5 m: the street the band should span."""
    base = ctx.get('roadlike')
    u = unary_union([g for g in (base, main, *extra) if g is not None])
    u = closing(u, design['street_close_m']/ctx['mpp'])
    if clip is not None and u is not None:
        u = polygonal(u.intersection(clip))
    return u


# --- v2 band, metrics, trims, gate -----------------------------------------------------------------------------------------

def build_band_v2(res, ctx, design=DESIGN_V2, width_override=None):
    """Width (7): the median of the clean two-kerb stations, or of the mirrored stations when the two-kerb ones are a minority
    (canopy half); p75 recorded; a shared segment width overrides (5). Offset: the median clean midpoint (mask) or, for a
    mirrored width, the reserve medial line."""
    mpp = ctx['mpp']
    secs = [s for s in res['sections'] if not s['outside']]
    clean = [s for s in secs if s['clean']]
    mirrored = [s for s in secs if s.get('w_mirror') is not None]
    out = {'sections_n': len(res['sections']), 'sections_inside': len(secs), 'sections_clean': len(clean),
           'sections_mirrored': len(mirrored), 'section_ends': dict(Counter(e for s in secs for e in s['ends']))}
    if len(clean) >= design['min_clean_sections'] and len(clean) >= design['mirror_max_two_kerb_share']*(len(clean)+len(mirrored)):
        source, used = 'two_kerb', clean
        widths = np.asarray([s['w'] for s in clean])
    elif len(mirrored) >= design['min_clean_sections']:
        source, used = 'mirrored', mirrored
        widths = np.asarray([s['w_mirror'] for s in mirrored])
    else:
        out['band'] = None
        out['fail'] = 'few_sections'
        return out
    out['width_source'] = source
    out['width_samples'] = [r(w, 3) for w in widths]
    out['width_clean_median_m'] = r(np.median(widths), 2)
    out['width_median_m'] = r(np.median(widths), 2)
    out['width_p75_m'] = r(np.percentile(widths, 75), 2)
    out['width_clean_cv'] = r(cv_of(widths))
    width = float(np.percentile(widths, design['width_pct']))
    out['width_own_m'] = r(width, 2)
    if width_override is not None:
        width = float(width_override)
        out['width_shared'] = True
    out['width_raw_m'] = r(width, 2)
    rw = [s['w_reserve'] for s in secs if s.get('w_reserve')]
    cap = design['reserve_cap_ratio']*float(np.median(rw)) if (rw and res.get('clip') is not None) else None
    out['reserve_width_m'] = r(np.median(rw), 2) if rw else None
    out['width_cap_m'] = r(cap, 2)
    if cap is not None and width > cap:
        width = cap
        out['width_capped'] = True
    out['width_m'] = r(width, 2)
    segs = res['segments']
    offsets, sources = [], []
    # the reserve medial line is the street's reference only where the reserve lies inside the frame: where the frame cuts
    # the reserve at half the stations or more, the medial line is not the street centre and the mask midpoints are used
    r_cut = sum(1 for s in secs if s.get('r_frame'))/len(secs) if secs else 1.
    out['reserve_frame_cut_share'] = r(r_cut, 3)
    for k, seg in enumerate(segs):
        if source == 'mirrored':
            mids = [s['medial_off'] for s in used if s['seg'] == k]
            offsets.append(float(np.median(mids)) if mids else 0.)
            sources.append('reserve_medial')
            continue
        mids = [(s['lo']+s['hi'])/2 for s in used if s['seg'] == k]
        mask_off = float(np.median(mids)) if mids else 0.
        if design['offset_source'] == 'reserve' and seg['snapped'] and seg['reserve_pt'] is not None and r_cut < .5:
            offsets.append(float((seg['reserve_pt']-seg['p']) @ normal(seg['d'])))
            sources.append('reserve')
        else:
            offsets.append(mask_off)
            sources.append('mask')
    out['offset_sources'] = sources
    out['offsets_px'] = offsets
    pts = axis_polyline(segs, offsets, max_joint_px=15./mpp)
    return finish_band(out, LineString([tuple(p) for p in pts]), width, res, ctx, design)


def band_edges(res, band_out, ctx):
    """Per inside station: the band's lateral edges (px, relative to the station's segment line)."""
    w = band_out['width_m']/ctx['mpp']
    offs = band_out.get('offsets_px') or [0.]*len(res['segments'])
    return {k: (o-w/2, o+w/2) for k, o in enumerate(offs)}


def slice_check(res, band_out, ctx, design=DESIGN_V2):
    """1: frame-edge share of the band's long edges and the street width ratio."""
    mpp, frame = ctx['mpp'], ctx['frame']
    out = {}
    axis = LineString(band_out['axis_px']).intersection(frame)
    shares = []
    secs = [s for s in res['sections'] if not s['outside']]
    if not axis.is_empty and axis.length > 0 and secs:
        inner = frame.buffer(-design['slice_frame_tol_m']/mpp)
        hw = band_out['width_m']/2/mpp
        d = unit(np.asarray(axis.coords[-1])-np.asarray(axis.coords[0])) if axis.geom_type == 'LineString' else res['segments'][0]['d']
        for side in (1, -1):
            try:
                edge = axis.offset_curve(side*hw)
            except Exception:                              # noqa: BLE001 - shapely raises GEOSException on odd input
                continue
            if edge.is_empty or edge.length <= 0:
                continue
            # the side of the sections this edge lies on: offset_curve(+) is left of the travel direction = +normal(d)
            sec_side = 1 if side*float(normal(d) @ normal(res['segments'][0]['d'])) > 0 else 0
            frame_ends = sum(1 for s in secs if s['ends'][sec_side] == 'frame')/len(secs)
            shares.append((edge.difference(inner).length/edge.length, frame_ends))
    # a frame slice: one long band edge within slice_frame_tol_m of the frame over >= slice_frame_share of its length AND the
    # street mask running into the frame on that side at >= slice_frame_end_share of the stations (the street goes on beyond)
    out['frame_edge_share'] = r(max(a for a, _ in shares), 3) if shares else None
    out['frame_edge'] = [[r(a, 3), r(b, 3)] for a, b in shares]
    ws = [s['w_street'] for s in res['sections'] if not s['outside'] and s.get('w_street')]
    out['street_width_m'] = r(np.median(ws), 2) if ws else None
    out['street_ratio'] = r(band_out['width_m']/np.median(ws), 3) if ws else None
    out['frame_slice'] = any(a >= design['slice_frame_share'] and b >= design['slice_frame_end_share'] for a, b in shares)
    out['narrow_slice'] = bool(ws and band_out['width_m'] < design['slice_street_ratio']*float(np.median(ws)))
    return out


def runs_exceed(values, ts, run_px, cover, limit):
    """values[i] (or None) at stations ts[i] (px along); True and the worst window when a run_px window with >= cover of its
    stations measured has |median| > limit."""
    worst = None
    ts = np.asarray(ts, float)
    for i in range(len(ts)):
        sel = (ts >= ts[i]) & (ts <= ts[i]+run_px)
        if ts[sel].max()-ts[i] < run_px*.95:
            continue
        vals = [values[j] for j in np.nonzero(sel)[0] if values[j] is not None]
        if len(vals) < cover*sel.sum() or not vals:
            continue
        m = float(np.median(vals))
        if worst is None or abs(m) > abs(worst[0]):
            worst = (m, float(ts[i]))
    return (worst is not None and abs(worst[0]) > limit), worst


def _verge(s, side, lo_e, hi_e, rmed, mpp, design):
    """Distance (m) from a band edge to the reserve edge on that side at a station; None at the frame or a junction mouth."""
    if s.get('r_lo') is None or s.get('r_frame') or rmed is None or s['w_reserve'] > design['curve_reserve_open_ratio']*rmed:
        return None
    return (lo_e-s['r_lo'])*mpp if side == 0 else (s['r_hi']-hi_e)*mpp


def curvature_check(res, band_out, ctx, design=DESIGN_V2):
    """3: kerb-side mask edges and reserve edges (verge width about its median) against the band edges over curve_run_m runs,
    and the fitted axis angle against the reserve medial line."""
    mpp = ctx['mpp']
    edges = band_edges(res, band_out, ctx)
    secs = [s for s in res['sections'] if not s['outside']]
    out = {'axis_deg': [s['angle_deg'] for s in res['segments']]}
    out['axis_off'] = any(a is not None and a > design['curve_axis_deg'] for a in out['axis_deg'])
    hits = []
    for k in sorted({s['seg'] for s in secs}):
        ss = sorted((s for s in secs if s['seg'] == k), key=lambda s: s['t'])
        if not ss:
            continue
        lo_e, hi_e = edges[k]
        ts = [s['t'] for s in ss]
        kerb = design['kerb_ends']
        # mask edges: OUTWARD deviation only (the street leaves the band); an edge inside the band is a shadow or a bite
        dev_lo = [max(0., (lo_e-s['lo'])*mpp) if s['ends'][0] in kerb else None for s in ss]
        dev_hi = [max(0., (s['hi']-hi_e)*mpp) if s['ends'][1] in kerb else None for s in ss]
        rw = [s['w_reserve'] for s in ss if s.get('w_reserve')]
        rmed = float(np.median(rw)) if rw else None

        v_lo, v_hi = [_verge(s, 0, lo_e, hi_e, rmed, mpp, design) for s in ss], [_verge(s, 1, lo_e, hi_e, rmed, mpp, design) for s in ss]
        for name, vals in (('mask_lo', dev_lo), ('mask_hi', dev_hi), ('reserve_lo', v_lo), ('reserve_hi', v_hi)):
            if name.startswith('reserve'):
                got = [v for v in vals if v is not None]
                if not got:
                    continue
                med = float(np.median(got))
                vals = [None if v is None else v-med for v in vals]
            bad, worst = runs_exceed(vals, ts, design['curve_run_m']/mpp, design['curve_min_cover'], design['curve_dev_m'])
            if worst is not None:
                out[f'{name}_worst_m'] = r(worst[0], 2)
            if bad:
                hits.append(name)
    out['edge_runs'] = hits
    out['curved'] = bool(out['axis_off'] or hits)
    return out


def band_metrics_v2(res, band_out, ctx, design=DESIGN_V2):
    """v1 band_metrics with the v2 guards (4): decided objects of any class but car / tree block, decided P0 twins included
    unless they retire on accept (same street); candidate footpath / vegetation over footpath_eps_m2 each are 'footpath'
    blockers; candidate driveways keep the v1 soft rule."""
    m = band_metrics({**res, 'exclude': set()}, band_out, ctx, {**design, 'hard_blockers': (), 'soft_blockers': ()})
    for k in ('blockers_hard', 'blockers_decided', 'blockers_soft'):
        m.pop(k, None)
    mpp, band = ctx['mpp'], band_out['band']
    retire = {t['id'] for t in res.get('retires', [])}
    hard, decided_hits, soft, foot = [], [], [], []
    for o in ctx['live']:
        oid = o['id']
        if oid not in ctx['polys'] or oid in res['exclude'] or oid in retire or o['kind'] in design['under_kinds']:
            continue
        p = ctx['polys'][oid]
        if not p.intersects(band):
            continue
        ov = p.intersection(band).area*mpp*mpp
        if ov <= 0:
            continue
        rec = {'id': oid, 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': r(ov, 2)}
        if o['kind'] in design['hard_blockers'] and ov > design['blocker_eps_m2']:
            hard.append(rec)
        elif is_decided(o, design) and ov > design['blocker_eps_m2']:
            decided_hits.append(rec)
        elif not is_decided(o, design) and o['kind'] in design['footpath_kinds'] and ov > design['footpath_eps_m2']:
            foot.append(rec)
        elif o['kind'] in design['soft_blockers'] and ov > design['soft_eps_m2'] and ov/(p.area*mpp*mpp) > design['soft_share']:
            soft.append({**rec, 'share_of_object': r(ov/(p.area*mpp*mpp))})
    m['blockers_hard'] = hard
    m['blockers_decided'] = decided_hits
    m['blockers_footpath'] = foot
    m['blockers_soft'] = soft
    return m


def trim_footpaths(res, band_out, metrics, ctx, design=DESIGN_V2):
    """4: each footpath / vegetation blocker lying on one side of the axis within edge_trim_max_m of that edge pulls the edge
    back past it (+ 0.05 m); one within end_trim_max_m of an end trims the end (as v1). Returns (band_out, metrics, trims) or
    None when a blocker cannot be trimmed away."""
    from shapely.ops import substring
    mpp = ctx['mpp']
    w = band_out['width_m']/mpp
    segs = res['segments']
    line = LineString(band_out['axis_px'])
    band = band_out['band']
    proj = [line.project(Point(c)) for c in band.exterior.coords]
    s0, s1 = min(proj), max(proj)
    lim, pad = design['end_trim_max_m']/mpp, .2/mpp
    pen = {1: 0., -1: 0.}
    new0, new1, trims = s0, s1, []
    d = segs[0]['d']
    n = normal(d)
    off = band_out['offsets_px'][0]
    base = segs[0]['p']+n*off
    for b in metrics['blockers_footpath']:
        ov = ctx['polys'][b['id']].intersection(band)
        pts = np.asarray([c for poly in iter_polygons(ov) for c in poly.exterior.coords], float)
        if not len(pts):
            continue
        lat = (pts-base) @ n
        along = [line.project(Point(c)) for c in pts]
        b0, b1 = min(along), max(along)
        if lat.min() >= 0 or lat.max() <= 0:
            side = 1 if lat.min() >= 0 else -1
            depth = w/2-float(np.min(np.abs(lat)))
            if depth*mpp <= design['edge_trim_max_m']:
                pen[side] = max(pen[side], depth+.05/mpp)
                trims.append({'edge': 'hi' if side == 1 else 'lo', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r(depth*mpp+.05, 2)})
                continue
        if b1-s0 <= lim:
            new0 = max(new0, b1+pad)
            trims.append({'end': 'start', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r((b1+pad-s0)*mpp, 2)})
        elif s1-b0 <= lim:
            new1 = min(new1, b0-pad)
            trims.append({'end': 'end', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r((s1-b0+pad)*mpp, 2)})
        else:
            return None
    if new1-new0 <= 0 or (pen[1]+pen[-1]) >= w:
        return None
    shift = (pen[-1]-pen[1])/2
    width = (w-pen[1]-pen[-1])*mpp
    out = {k: v for k, v in band_out.items() if k not in ('band', 'ring', 'fail', 'vertices', 'app_issues', 'ring_repairs', 'made_valid')}
    out['offsets_px'] = [o+shift for o in band_out['offsets_px']]
    out['width_before_edge_trim_m'] = band_out['width_m']
    out['width_m'] = r(width, 2)
    shifted = LineString([tuple(np.asarray(c)+n*shift) for c in substring(line, new0, new1).coords])
    out = finish_band(out, shifted, width, res, ctx, design)
    if out.get('band') is None:
        return None
    out['footpath_trims'] = trims
    return out, band_metrics_v2(res, out, ctx, design), trims


def gate_v2(res, band_out, metrics, design=DESIGN_V2, slc=None, curv=None):
    """v2 exits in a fixed order: short, junction, slice, curved, then the v1 gates, the band fails and the guards."""
    reasons = []
    length = band_out.get('axis_length_m') if band_out else None
    if length is None:
        length = res.get('length_m') or (res['axis']['length_m'] if res.get('axis') else 0.)
    if (length or 0.) < design['min_len_m']:
        reasons.append('short')
    if res.get('junction_exit'):
        reasons.append('junction')
    if slc and (slc['frame_slice'] or slc['narrow_slice']):
        reasons.append('slice')
    if curv and curv['curved']:
        reasons.append('curved')
    if metrics and (metrics['iou_amodal'] or 0.) < design['iou_gate']:
        reasons.append('low_iou')
    if band_out and band_out.get('width_clean_cv') is not None and band_out['width_clean_cv'] > design['cv_gate']:
        reasons.append('high_cv')
    if res.get('reserve_frac') is not None and res['reserve_frac'] < design['reserve_road_frac']:
        reasons.append('reserve_fraction')
    if res.get('dominant') in design['lot_exit_classes']:
        reasons.append('lot_class')
    if band_out and band_out.get('fail'):
        reasons.append(band_out['fail'])
    if metrics:
        if metrics['blockers_hard']:
            reasons.append('covers_blocker')
        if metrics['blockers_decided']:
            reasons.append('covers_decided')
        if metrics['blockers_footpath']:
            reasons.append('covers_footpath')
        if metrics['blockers_soft']:
            reasons.append('covers_other_class')
    return reasons, (reasons[0] if reasons else None)


# --- v2 layer --------------------------------------------------------------------------------------------------------------

def layer_context_v2(objects, mpp, width, height, reserve, lots, design=DESIGN_V2):
    ctx = layer_context(objects, mpp, width, height, reserve, lots, design)
    ctx['segment_labels'] = reserve_segments(ctx, design)
    gap = [ctx['polys'][o['id']] for k in design['join_gap_kinds'] for o in ctx['by_kind'].get(k, [])]
    ctx['gap_union'] = unary_union(gap) if gap else None
    other = [ctx['polys'][o['id']] for o in ctx['live'] if o['id'] in ctx['polys'] and o['kind'] not in design['join_gap_kinds']]
    ctx['gap_blockers'] = unary_union(other) if other else None
    rl = [ctx['polys'][o['id']] for k in ('road', 'car') for o in ctx['by_kind'].get(k, [])]
    ctx['roadlike'] = unary_union(rl) if rl else None
    return ctx


def corridor_layer_v2(objects, *, width, height, mpp, reserve=None, lots=None, design=DESIGN_V2, affine=None, cache=None,
                      layer_ctx=None):
    """v2 steps on one layer (pure). Two passes: the second applies one width per reserve segment (5) to every band whose
    segment holds two or more bands in the first pass."""
    ctx = layer_ctx if (layer_ctx is not None and 'segment_labels' in layer_ctx) else \
        layer_context_v2(objects, mpp, width, height, reserve, lots or {}, design)
    ctx['affine'] = affine
    groups = arbitrate(ctx, design)
    groups_by_member = {m['id']: g for g in groups for m in g['members']}
    seeds = seed_order(ctx, groups, design)
    cache = {} if cache is None else cache
    first = _v2_pass(ctx, groups, groups_by_member, seeds, design, cache, shared=None)
    pooled = defaultdict(list)
    for p in first['proposals']:
        if p.get('reserve_segment') is not None and p.get('width_samples'):
            pooled[p['reserve_segment']].append(p)
    shared = {}
    for seg, recs in sorted(pooled.items()):
        if len(recs) >= 2:
            shared[seg] = float(np.median([w for p in recs for w in p['width_samples']]))
    rep = _v2_pass(ctx, groups, groups_by_member, seeds, design, cache, shared=shared) if shared else first
    rep['shared_widths'] = {str(k): r(v, 2) for k, v in shared.items()}
    rep['twin_groups'] = groups
    rep['design'] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in design.items()}
    rep['assumption'] = ASSUMPTION
    return rep, ctx, cache


def _v2_seed_record(seed, ctx, res, mpp):
    record = {'seed': seed['id'], 'seed_kind': seed['kind'], 'seed_status': seed.get('status'),
              'seed_area_m2': r(area_m2(ctx['polys'][seed['id']], mpp), 2), 'group': res.get('group'),
              'reserve_frac': res.get('reserve_frac'), 'lot_shares': res.get('lot_shares'), 'dominant': res.get('dominant'),
              'siblings': res.get('siblings', []), 'cars_unioned': res.get('cars_unioned', []), 'joined': res.get('joined', []),
              'vertices_before': len(seed.get('ring') or [])+sum(len(h) for h in seed.get('holes') or [])}
    if res.get('junction'):
        record['junction'] = res['junction']
    if res.get('part_joins'):
        record['part_joins'] = res['part_joins']
    if res.get('axis'):
        ax = res['axis']
        record.update(skeleton_length_m=r(ax['length_m'], 2), k=ax['k'], maxdev_m=ax['maxdev_m'], junction_spurs=ax['junction_spurs'],
                      spurs_ge_3m=[s for s in ax['spurs'] if s['protrusion_m'] >= DESIGN_V2['junction_spur_m']])
        if res.get('segments'):
            record.update(snapped=[s['snapped'] for s in res['segments']], snap_angle_deg=[s['angle_deg'] for s in res['segments']],
                          segments=len(res['segments']))
    return record


def _v2_pass(ctx, groups, groups_by_member, seeds, design, cache, shared):
    mpp = ctx['mpp']
    consumed, proposals, exits, accepted = {}, [], [], []
    for seed in seeds:
        if seed['id'] in consumed:
            continue
        seed_poly = ctx['polys'][seed['id']]
        seed_filled = fill_holes(seed_poly)
        _, sdir, _, _ = mrr_axis(seed_filled)
        base = {'seed': seed['id'], 'seed_kind': seed['kind'], 'seed_status': seed.get('status'),
                'seed_area_m2': r(area_m2(seed_poly, mpp), 2), 'group': (groups_by_member.get(seed['id']) or {}).get('group'),
                'vertices_before': len(seed.get('ring') or [])+sum(len(h) for h in seed.get('holes') or [])}
        # 2: no reserve in the frame -> no band; car park evidence names it
        evidence = car_park_evidence(seed_filled, sdir, ctx, design)
        if ctx['reserve'] is None:
            exit_ = 'car_park' if evidence['evidence'] else 'no_reserve'
            exits.append({**base, 'car_park': evidence, 'exit': exit_, 'exit_reasons': [exit_]})
            continue
        shares, dominant = lot_shares(seed_filled, ctx['reserve'], ctx['lots'])
        site = site_area_m2(seed_filled, ctx['lots'], 'single_lot_or_crown', mpp) if dominant == 'single_lot_or_crown' else 0.
        evidence['site'] = {'dominant': dominant, 'single_lot_site_m2': r(site, 1)}
        on_site = dominant == 'single_lot_or_crown' and site >= design['large_lot_m2']
        in_reserve = evidence['evidence'] and evidence['perpendicular'] > evidence['parallel']
        if evidence['evidence'] and (on_site or in_reserve):
            evidence['why'] = 'large single-lot site' if on_site else 'perpendicular cars outnumber parallel ones (in the reserve)'
            exits.append({**base, 'car_park': evidence, 'reserve_frac': r(shares.get('reserve', 0.)), 'dominant': dominant,
                          'exit': 'car_park', 'exit_reasons': ['car_park']})
            continue
        group = groups_by_member.get(seed['id'])
        pre = None
        if group is not None and group['resolved_class'] is None and group['how'].startswith('conflict'):
            pre = 'p0_conflict'
        elif group is not None and group['resolved_class'] not in (None, 'road') and group['how'].startswith('confirmed'):
            pre = 'p0_decided_other_class'
        elif group is not None and group['resolved_class'] == 'driveway':
            pre = 'p0_driveway'
        # 5 / 6: join collinear road pieces of the same reserve segment across road / car / tree gaps
        seg_id = segment_of(seed_filled, ctx, design)
        joined, bridges, join_log = [], [], []
        if pre is None and seg_id is not None:
            key_score = round(float(seed.get('score') or 0.), design['score_round'])
            cand = []
            for o in sorted(ctx['by_kind'].get('road', []), key=lambda o: o['id']):
                if o['id'] == seed['id'] or o['id'] in consumed:
                    continue
                same_mask = round(float(o.get('score') or 0.), design['score_round']) == key_score
                if is_decided(o, design) and not same_mask:
                    continue                                       # decided pieces of another mask block, never join
                g = groups_by_member.get(o['id'])
                if g is not None and g['resolved_class'] not in (None, 'road'):
                    continue
                p = fill_holes(ctx['polys'][o['id']])
                if ctx['reserve'] is None or p.intersection(ctx['reserve']).area/p.area < design['join_reserve_frac']:
                    continue
                if segment_of(p, ctx, design) not in (seg_id, None):
                    continue                                       # another reserve segment (None: no medial near it)
                cand.append((o['id'], p))
            if cand:
                clip = ctx['reserve'].buffer(design['reserve_clip_m']/mpp)
                _, joined, bridges, join_log = join_pieces(seed_filled, sdir, cand, ctx, ctx['gap_union'], design, clip=clip)
        key = ('v2', seed['id'], tuple(joined), design['adjacency_m'], design['segment_tol_m'], design['snap_deg'])
        if key not in cache:
            cache[key] = analyse_seed_v2(seed, ctx, groups_by_member, joined, bridges, design)
            cache[key]['_key'] = key
        res = cache[key]
        # 4: decided twins: same street -> retires on accept; otherwise they block
        retires = []
        for m in (group['members'] if group else []):
            if m['id'] == seed['id'] or not m['decided']:
                continue
            tw_iou = iou(ctx['polys'][m['id']], seed_poly)
            if (group.get('reserve_frac') or 0.) >= design['retire_reserve_frac'] and tw_iou >= design['retire_iou']:
                retires.append({'id': m['id'], 'kind': m['kind'], 'status': m['status'], 'iou_with_seed': r(tw_iou),
                                'reserve_frac': group.get('reserve_frac')})
        res['retires'] = retires
        record = {**base, **_v2_seed_record(seed, ctx, res, mpp), 'reserve_segment': seg_id, 'join_log': join_log,
                  'car_park': evidence}
        band_out = metrics = slc = curv = None
        if res.get('exit'):
            reasons, first = [res['exit']], res['exit']
        elif pre is not None and not res.get('sections'):
            reasons, first = [pre], pre
        else:
            if res.get('sections'):
                width_override = shared.get(seg_id) if shared else None
                band_out = build_band_v2(res, ctx, design, width_override=width_override)
                if band_out.get('band') is not None:
                    slc = slice_check(res, band_out, ctx, design)
                    curv = curvature_check(res, band_out, ctx, design)
                    metrics = band_metrics_v2(res, band_out, ctx, design)
                    if metrics['blockers_hard'] or metrics['blockers_decided']:
                        trimmed = trim_for_blockers(res, band_out, metrics, ctx, design)
                        if trimmed is not None:
                            band_out, _, _ = trimmed
                            metrics = band_metrics_v2(res, band_out, ctx, design)
                    if metrics['blockers_footpath'] and not metrics['blockers_hard'] and not metrics['blockers_decided']:
                        trimmed = trim_footpaths(res, band_out, metrics, ctx, design)
                        if trimmed is not None:
                            band_out, metrics, _ = trimmed
            if band_out is not None and band_out.get('fail') == 'few_sections' and res.get('sections'):
                inside = [x for x in res['sections'] if not x['outside']]
                shares = [sum(1 for x in inside if x['ends'][q] == 'frame')/len(inside) for q in (0, 1)] if inside else [0., 0.]
                if max(shares) >= design['slice_frame_end_share']:
                    slc = {'frame_slice': True, 'narrow_slice': False, 'frame_end_share': r(max(shares), 3),
                           'why': 'the street runs into the frame along one side; too few clean sections for a width'}
            if res.get('axis') is None and not res.get('junction_exit') and (res.get('mrr_length_m') or 0) >= design['min_len_m']:
                reasons, first = ['no_axis'], 'no_axis'
            else:
                reasons, first = gate_v2(res, band_out, metrics, design, slc, curv)
            if pre is not None:
                reasons, first = [pre, *reasons], pre
        if band_out:
            record.update({('band_ring' if k == 'ring' else k): v for k, v in band_out.items() if k != 'band'})
            modal_w = [s.get('w_modal') for s in res['sections'] if not s['outside'] and 'frame' not in s['ends'] and s.get('w_modal')]
            record['width_cv_before'] = r(cv_of(modal_w))
        if slc:
            record['slice'] = slc
        if curv:
            record['curvature'] = curv
        if metrics:
            record.update(metrics)
        if retires:
            record['retires_on_accept'] = retires
        if metrics and band_out and band_out.get('band') is not None:
            overlaps = [{'proposal': q['proposal']['id'], 'seed': q['seed'], 'share': r(band_out['band'].intersection(qb).area/band_out['band'].area)}
                        for q, qb in accepted if band_out['band'].intersects(qb)]
            overlaps = [o for o in overlaps if o['share'] >= design['overlap_share']]
            if overlaps:
                record['overlaps'] = overlaps
                reasons = [*reasons, 'overlaps_proposal']
                first = first or 'overlaps_proposal'
        record['exit_reasons'] = reasons
        if first is not None:
            record['exit'] = first
            exits.append(record)
            continue
        band = band_out['band']
        absorbs, replaces, on_accept = [], [], []
        (on_accept if is_decided(seed, design) else replaces).append(seed['id'])
        for sid in res['siblings']:
            o = ctx['by_id'][sid]
            absorbs.append({'id': sid, 'why': 'same-mask sibling', 'status': o.get('status'), 'area_m2': r(area_m2(ctx['polys'][sid], mpp), 3)})
            (on_accept if is_decided(o, design) else replaces).append(sid)
        for jid in joined:
            o = ctx['by_id'][jid]
            absorbs.append({'id': jid, 'why': 'joined collinear piece', 'status': o.get('status'), 'area_m2': r(area_m2(ctx['polys'][jid], mpp), 3)})
            (on_accept if is_decided(o, design) else replaces).append(jid)
        for o in ctx['by_kind'].get('road', []):
            if o['id'] in replaces or o['id'] in on_accept or o['id'] in consumed:
                continue
            p = ctx['polys'][o['id']]
            cover = p.intersection(band).area/p.area if p.intersects(band) else 0.
            a = area_m2(p, mpp)
            same_mask = round(float(o.get('score') or 0.), design['score_round']) == round(float(seed.get('score') or 0.), design['score_round'])
            if cover >= design['absorb_cover'] or (a < design['speck_m2'] and p.distance(band) <= .5/mpp):
                if is_decided(o, design) and not same_mask:
                    continue
                why = 'speck' if a < design['speck_m2'] else 'road candidate inside the band'
                absorbs.append({'id': o['id'], 'why': why + (' (same mask)' if same_mask else ''), 'status': o.get('status'),
                                'area_m2': r(a, 3), 'cover': r(cover)})
                (on_accept if is_decided(o, design) else replaces).append(o['id'])
        twins = []
        for m in (group['members'] if group else []):
            if m['id'] == seed['id']:
                continue
            retiring = any(t['id'] == m['id'] for t in retires)
            action = ('retires on accept: a decided twin of the same street' if retiring else
                      'kept: a decided twin keeps its class and geometry' if m['decided'] else
                      f'absorbed: proposed {group["resolved_class"] or seed["kind"]} ({group["how"]})')
            twins.append({'id': m['id'], 'kind': m['kind'], 'status': m['status'], 'decided': m['decided'], 'action': action,
                          'iou_with_seed': r(iou(ctx['polys'][m['id']], seed_poly)),
                          'band_cover': r(ctx['polys'][m['id']].intersection(band).area/ctx['polys'][m['id']].area)})
            if not m['decided']:
                replaces.append(m['id'])
        remainder = []
        if (res.get('junction') or {}).get('arm_a') is not None:
            # 8: the other arms stay modal: the seed outside the band is kept as it is (recorded, applied only on accept)
            rest = polygonal(fill_holes(seed_poly).difference(band.buffer(.1/mpp)))
            for part in sorted(iter_polygons(rest) if rest is not None else [], key=lambda p: -p.area):
                if area_m2(part, mpp) >= design['speck_m2']:
                    remainder.append({'from': seed['id'], 'area_m2': r(area_m2(part, mpp), 2),
                                      'ring': [[r(x, 2), r(y, 2)] for x, y in part.exterior.coords[:-1]]})
        retire_ids = [t['id'] for t in retires]
        lineage = sorted(set(replaces) | set(on_accept) | set(retire_ids))
        pid = hashlib.sha256((design['version']+':'+','.join(lineage)).encode()).hexdigest()[:16]
        model = _ring_details([tuple(p) for p in band_out['ring']], [], np.asarray(affine_of(ctx), float), 'road') if affine_of(ctx) is not None else {}
        seed_model = (seed.get('model') or {})
        if seed_model.get('color'):
            model['color'] = seed_model['color']
        proposal = {'id': pid, 'kind': 'road', 'label': 'Road', 'ring': band_out['ring'], 'status': 'proposal',
                    'source': design['version'], 'geometry_reviewed': False, 'score': seed.get('score'), 'model': model,
                    'replaces': sorted(set(replaces)), 'replaces_on_accept': sorted(set(on_accept)),
                    'retires_on_accept': sorted(retire_ids), 'remainder_on_accept': remainder,
                    'absorbs': absorbs, 'xclass_twin': twins, 'derived_from': lineage,
                    'cars_under': [c['id'] for c in record.get('cars_under', [])], 'assumption': 'parking lane counts as road'}
        record['proposal'] = proposal
        record['vertices_after'] = len(band_out['ring'])
        band_secs = []
        for s in res['sections']:
            if s['outside'] or 'frame' in s['ends']:
                continue
            piece = containing(LineString([tuple(s['p']-s['n']*60./mpp), tuple(s['p']+s['n']*60./mpp)]).intersection(band), Point(s['p']), tol=60./mpp)
            if piece is not None:
                band_secs.append(piece.length*mpp)
        record['width_cv_after'] = r(cv_of(band_secs))
        for i in lineage:
            consumed[i] = pid
        proposals.append(record)
        accepted.append((record, band))
    counts = {'seeds': len(seeds), 'proposals': len(proposals), 'exits': dict(Counter(e['exit'] for e in exits)),
              'twin_groups': len(groups), 'twin_pairs': sum(len(g['pairs']) for g in groups),
              'twin_groups_by_class': dict(Counter(str(g['resolved_class']) for g in groups)),
              'specks_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'].startswith('speck')),
              'siblings_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'] == 'same-mask sibling'),
              'joined_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'] == 'joined collinear piece'),
              'retires_on_accept': sum(len(p['proposal']['retires_on_accept']) for p in proposals),
              'cars_under': sum(p.get('cars_under_n', 0) for p in proposals)}
    return {'counts': counts, 'proposals': proposals, 'exits': exits}


def affine_of(ctx):
    return ctx.get('affine')


# --- scenes ----------------------------------------------------------------------------------------------------------------

def windows():
    return sorted({json.loads(line)['window_scene_id'] for line in open(WINDOWS) if line.strip()})


def load_scene(scene_id):
    d = ROOT/'data/scenes'/scene_id
    scene = json.load(open(d/'latest.json'))
    review = json.load(open(d/'review.json')) if (d/'review.json').exists() else None
    return scene, review


def scene_inputs(scene):
    from ..scene_lot_tags import parcels_for_scene
    parcels, sha, src = parcels_for_scene(scene, ROOT, offline=True)
    reserve, lots = reserve_and_lots(scene, parcels)
    return reserve, lots, {'parcels_source': src, 'parcels_sha256': sha, 'parcels_n': len((parcels or {}).get('features') or [])}


def layers(scene, review):
    out = [('model', [o for o in scene['objects'] if o.get('status') != 'superseded'])]
    if review is not None:
        out.append(('review', review['objects']))
    return out


def jsonable(value):
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items() if k not in ('band',)}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else round(float(value), 4)
    if isinstance(value, (np.integer,)):
        return int(value)
    return value


# --- by-eye width error ----------------------------------------------------------------------------------------------------

def load_byeye():
    path = OUT/'kerb-widths-by-eye.json'
    return json.load(open(path)) if path.exists() else None


def byeye_errors(report, scene_id, byeye):
    """Per by-eye object in this scene: the record (proposal or exit) whose seed or lineage holds it; band width minus by-eye
    width per station (mean absolute), and the kerb-line distance to the band edge (|lateral(kerb)| - width/2)."""
    if not byeye:
        return []
    rows = [s for s in byeye['stations'] if s['window'] == scene_id and s.get('width_m') is not None]
    out = []
    for oid in sorted({s['object'] for s in rows}):
        rec = next((p for p in report['proposals'] if p['seed'] == oid or oid in p['proposal']['derived_from']), None)
        exited = rec is None
        if rec is None:
            rec = next((e for e in report['exits'] if e['seed'] == oid), None)
        if rec is None or rec.get('width_m') is None:
            out.append({'object': oid, 'proposal': False, 'exit': rec.get('exit') if rec else 'not_seeded'})
            continue
        w = rec['width_m']
        st = [s for s in rows if s['object'] == oid]
        errs = [w-s['width_m'] for s in st]
        edge = []
        if rec.get('axis_px'):
            ax = LineString(rec['axis_px'])
            for s in st:
                for side in ('left', 'right'):
                    kp = s.get(f'{side}_kerb_px')
                    if kp:
                        edge.append(abs(ax.distance(Point(kp))*s['mpp']-w/2))
        out.append({'object': oid, 'proposal': not exited, 'exit': rec.get('exit'), 'width_m': w,
                    'byeye_median_m': r(np.median([s['width_m'] for s in st]), 2), 'width_error_m': r(np.mean(errs), 2),
                    'width_abs_error_m': r(np.mean(np.abs(errs)), 2), 'kerb_edge_abs_error_m': r(np.mean(edge), 2) if edge else None})
    return out


# --- sweep -----------------------------------------------------------------------------------------------------------------

def sweep(args):
    byeye = load_byeye()
    names = sorted({**POSITIVES, **MUST_EXIT})
    beat(f'sweep start: {len(names)} windows')
    rows = []
    prepared = {}
    for scene_id in names:
        scene, review = load_scene(scene_id)
        reserve, lots, meta = scene_inputs(scene)
        mpp = scene_mpp(scene)
        objects = review['objects'] if review is not None else scene['objects']
        ctx = layer_context(objects, mpp, scene['image_width'], scene['image_height'], reserve, lots)
        prepared[scene_id] = (scene, objects, reserve, lots, mpp, ctx, {})
    combos = list(itertools.product(SWEEP_GRID['width_pct'], SWEEP_GRID['adjacency_m'], SWEEP_GRID['segment_tol_m'],
                                    SWEEP_GRID['offset_source'], SWEEP_GRID['iou_gate'], SWEEP_GRID['snap_deg']))
    for n, (pct, adj, tol, off, gate_v, snap) in enumerate(combos):
        setting = {'width_pct': pct, 'adjacency_m': adj, 'segment_tol_m': tol, 'offset_source': off, 'iou_gate': gate_v, 'snap_deg': snap}
        design = {**DESIGN, **setting}
        agg = {'setting': setting, 'windows': {}}
        for scene_id in names:
            scene, objects, reserve, lots, mpp, ctx, cache = prepared[scene_id]
            rep, _, _ = corridor_layer(objects, width=scene['image_width'], height=scene['image_height'], mpp=mpp, reserve=reserve,
                                       lots=lots, design=design, affine=scene['pixel_to_meters'], cache=cache, layer_ctx=ctx)
            named = POSITIVES.get(scene_id) or MUST_EXIT.get(scene_id)
            named_out = {}
            for oid in named:
                p = next((x for x in rep['proposals'] if x['seed'] == oid or oid in x['proposal']['derived_from']), None)
                e = next((x for x in rep['exits'] if x['seed'] == oid), None)
                named_out[oid] = {'proposal': p['proposal']['id'] if p else None, 'exit': e['exit'] if e else None,
                                  'exit_reasons': e['exit_reasons'] if e else None}
            agg['windows'][scene_id] = {
                'role': 'positive' if scene_id in POSITIVES else 'must_exit', 'named': named_out,
                'proposals': len(rep['proposals']), 'exits': rep['counts']['exits'],
                'vertices_after': [p['vertices_after'] for p in rep['proposals']],
                'iou_modal': [p['iou_modal'] for p in rep['proposals']], 'iou_amodal': [p['iou_amodal'] for p in rep['proposals']],
                'cars_under': sum(p['cars_under_n'] for p in rep['proposals']),
                'cars_under_parked': sum(p['cars_under_parked_n'] for p in rep['proposals']),
                'unioned_car_area_under_share': [p['unioned_car_area_under_share'] for p in rep['proposals']],
                'building_overlap_m2': r(sum(p['building_overlap_m2'] for p in rep['proposals']), 3),
                'building_overlap_m2_exited': r(sum(e.get('building_overlap_m2') or 0 for e in rep['exits']), 3),
                'outside_reserve_m2': r(sum(p.get('outside_reserve_m2') or 0 for p in rep['proposals']), 2),
                'byeye': byeye_errors(rep, scene_id, byeye)}
        rows.append(summarise_setting(agg))
        if n % 27 == 26:
            beat(f'sweep {n+1}/{len(combos)} settings')
    best = pick_default(rows)
    doc = {'version': VERSION, 'written_at': datetime.now(UTC).isoformat(), 'layer': 'review', 'grid': SWEEP_GRID,
           'windows': {'positives': POSITIVES, 'must_exit': MUST_EXIT}, 'assumption': ASSUMPTION,
           'byeye_source': 'prototype/kerb-widths-by-eye.json', 'settings': rows, 'default': best}
    OUT.mkdir(parents=True, exist_ok=True)
    json.dump(jsonable(doc), open(OUT/'sweep.json', 'w'), indent=1)
    beat(f'sweep done; default {best["setting"] if best else None}')
    print(json.dumps(jsonable(best), indent=1)[:3000])


def summarise_setting(agg):
    w = agg['windows']
    by = [b for v in w.values() for b in v['byeye']]
    have = [b for b in by if b.get('proposal')]
    pos_named = [(sid, oid, x) for sid, v in w.items() if v['role'] == 'positive' for oid, x in v['named'].items()]
    neg_named = [(sid, oid, x) for sid, v in w.items() if v['role'] == 'must_exit' for oid, x in v['named'].items()]
    verts = [x for v in w.values() for x in v['vertices_after']]
    s = {'setting': agg['setting'],
         'positives_with_proposal': sum(1 for *_, x in pos_named if x['proposal']), 'positives_named': len(pos_named),
         'positive_exits': {f'{sid}:{oid}': x['exit'] for sid, oid, x in pos_named if not x['proposal']},
         'must_exit_ok': all(not x['proposal'] for *_, x in neg_named),
         'must_exit_leaks': [f'{sid}:{oid}' for sid, oid, x in neg_named if x['proposal']],
         'must_exit_reasons': {f'{sid}:{oid}': x['exit'] for sid, oid, x in neg_named},
         'proposals_total': sum(v['proposals'] for v in w.values()),
         'proposals_in_must_exit_windows': sum(v['proposals'] for v in w.values() if v['role'] == 'must_exit'),
         'exits_by_reason': dict(sum((Counter(v['exits']) for v in w.values()), Counter())),
         'vertices_median': r(np.median(verts), 1) if verts else None, 'vertices_max': max(verts) if verts else None,
         'iou_modal_median': r(np.median([x for v in w.values() for x in v['iou_modal']])) if verts else None,
         'iou_amodal_median': r(np.median([x for v in w.values() for x in v['iou_amodal']])) if verts else None,
         'cars_under': sum(v['cars_under'] for v in w.values()), 'cars_under_parked': sum(v['cars_under_parked'] for v in w.values()),
         'unioned_car_area_under_share_median': r(np.median([x for v in w.values() for x in v['unioned_car_area_under_share'] if x is not None]))
         if any(x is not None for v in w.values() for x in v['unioned_car_area_under_share']) else None,
         'building_overlap_m2': r(sum(v['building_overlap_m2'] for v in w.values()), 3),
         'building_overlap_m2_exited_bands': r(sum(v['building_overlap_m2_exited'] for v in w.values()), 3),
         'outside_reserve_m2': r(sum(v['outside_reserve_m2'] for v in w.values()), 2),
         'byeye_objects': len(by), 'byeye_with_proposal': len(have),
         'byeye_width_abs_error_m': r(np.mean([b['width_abs_error_m'] for b in have])) if have else None,
         'byeye_width_signed_error_m': r(np.mean([b['width_error_m'] for b in have])) if have else None,
         'byeye_kerb_edge_abs_error_m': r(np.mean([b['kerb_edge_abs_error_m'] for b in have if b.get('kerb_edge_abs_error_m') is not None])) if have else None,
         'byeye': by, 'per_window': {sid: {k: v[k] for k in ('proposals', 'exits', 'named', 'building_overlap_m2')} for sid, v in w.items()}}
    return s


def pick_default(rows):
    """Eligible: building overlap of proposals ~0 (<= 0.25 m2), every must-exit object exits, all 5 by-eye roads proposed.
    Rank: by-eye width abs error, then kerb-edge error, then more positives proposed, then fewer vertices, then the design
    value of segment_tol_m (1.0 m) on a tie."""
    ok = [s for s in rows if s['building_overlap_m2'] <= .25 and s['must_exit_ok'] and s['byeye_with_proposal'] == s['byeye_objects']]
    pool = ok or [s for s in rows if s['building_overlap_m2'] <= .25 and s['must_exit_ok']] or rows
    best = min(pool, key=lambda s: (s['byeye_width_abs_error_m'] if s['byeye_width_abs_error_m'] is not None else 99.,
                                    s['byeye_kerb_edge_abs_error_m'] if s['byeye_kerb_edge_abs_error_m'] is not None else 99.,
                                    -s['positives_with_proposal'], s['vertices_median'] or 99,
                                    abs(s['setting']['segment_tol_m']-1.)))
    return {'setting': best['setting'], 'eligible_settings': len(ok), 'fully_eligible': bool(ok),
            'summary': {k: v for k, v in best.items() if k not in ('byeye', 'per_window')}, 'byeye': best['byeye']}


# --- full run --------------------------------------------------------------------------------------------------------------

def run(args):
    name = getattr(args, 'design', None) or 'v1'
    if name == 'v3':
        return run_v3(args)
    if name in ('v3.1', 'v3.2'):
        return run_v31(args)
    design = DESIGNS[name]
    out = Path(args.out) if getattr(args, 'out', None) else OUT/f'run-{name}'
    out.mkdir(parents=True, exist_ok=True)
    names = args.scenes or windows()
    byeye = load_byeye()
    byeye_new = load_byeye_new() if name != 'v1' else None
    totals = defaultdict(Counter)
    lists = defaultdict(lambda: defaultdict(list))
    per_scene = {}
    beat(f'run-{name} start: {len(names)} windows, setting {json.dumps({k: design[k] for k in SWEEP_GRID})}')
    for n, scene_id in enumerate(names):
        scene, review = load_scene(scene_id)
        reserve, lots, meta = scene_inputs(scene)
        mpp = scene_mpp(scene)
        for layer, objects in layers(scene, review):
            rep, ctx, _ = corridor_layer(objects, width=scene['image_width'], height=scene['image_height'], mpp=mpp,
                                         reserve=reserve, lots=lots, design=design, affine=scene['pixel_to_meters'])
            rep.update(scene=scene_id, layer=layer, mpp=r(mpp, 5), parcels=meta,
                       reserve_area_m2=r(area_m2(reserve, mpp), 1) if reserve is not None else None,
                       byeye=byeye_errors(rep, scene_id, byeye) if layer == 'review' else [])
            if byeye_new is not None:
                rep['byeye_new'] = byeye_errors(rep, scene_id, byeye_new) if layer == 'review' else []
            json.dump(jsonable(rep), open(out/f'{scene_id}.{layer}.json', 'w'), indent=1)
            c = rep['counts']
            t = totals[layer]
            t['windows'] += 1
            t['seeds'] += c['seeds']
            t['proposals'] += c['proposals']
            t['twin_groups'] += c['twin_groups']
            t['twin_pairs'] += c['twin_pairs']
            t['specks_absorbed'] += c['specks_absorbed']
            t['siblings_absorbed'] += c['siblings_absorbed']
            t['cars_under'] += c['cars_under']
            t['windows_with_proposal'] += bool(c['proposals'])
            for k, v in c['exits'].items():
                t[f'exit:{k}'] += v
            for k, v in c['twin_groups_by_class'].items():
                t[f'twin_class:{k}'] += v
            L = lists[layer]
            for p in rep['proposals']:
                L['vertices_before'].append(p['vertices_before'])
                L['vertices_after'].append(p['vertices_after'])
                L['width_cv_before'].append(p.get('width_cv_before'))
                L['width_cv_after'].append(p.get('width_cv_after'))
                L['width_m'].append(p['width_m'])
                L['iou_modal'].append(p['iou_modal'])
                L['iou_amodal'].append(p['iou_amodal'])
                L['building_overlap_m2'].append(p['building_overlap_m2'])
                L['outside_reserve_m2'].append(p.get('outside_reserve_m2') or 0.)
                L['cars_under_parked'].append(p['cars_under_parked_n'])
                t['confirmed_seed_proposals'] += p['seed_status'] == 'confirmed'
                t['replaces_on_accept_ids'] += len(p['proposal']['replaces_on_accept'])
                t['replaces_ids'] += len(p['proposal']['replaces'])
                t['xclass_twins_kept'] += sum(1 for x in p['proposal']['xclass_twin'] if x['decided'])
                t['xclass_twins_absorbed'] += sum(1 for x in p['proposal']['xclass_twin'] if not x['decided'])
            for e in rep['exits']:
                if e.get('width_clean_cv') is not None:
                    L['exit_width_cv_before'].append(e.get('width_cv_before'))
            per_scene[f'{scene_id}.{layer}'] = {'proposals': c['proposals'], 'exits': c['exits'], 'twin_groups': c['twin_groups']}
        beat(f'run-{name} {n+1}/{len(names)} {scene_id}')

    def dist(values):
        v = [x for x in values if x is not None]
        return {'n': len(v), 'median': r(np.median(v)), 'p10': r(np.percentile(v, 10)), 'p90': r(np.percentile(v, 90)),
                'sum': r(sum(v), 2)} if v else {'n': 0}
    metrics = {'version': design['version'], 'written_at': datetime.now(UTC).isoformat(), 'assumption': ASSUMPTION,
               'design': {k: (list(v) if isinstance(v, tuple) else v) for k, v in design.items()},
               'totals': {layer: dict(t) for layer, t in totals.items()},
               'distributions': {layer: {k: dist(v) for k, v in L.items()} for layer, L in lists.items()},
               'per_scene': per_scene}
    if name != 'v1':
        metrics.update(v2_summary(out, names, totals))
    json.dump(jsonable(metrics), open(out/'metrics.json', 'w'), indent=1)
    beat(f'run-{name} wrote metrics.json')
    print(json.dumps(jsonable(metrics['totals']), indent=1)[:2500])


# --- renders and marks -----------------------------------------------------------------------------------------------------

def _outline(draw, ring, x0, y0, colour, width=1, closed=True):
    if len(ring) >= 2:
        pts = [(x-x0, y-y0) for x, y in ring]
        draw.line(pts+([pts[0]] if closed else []), fill=colour, width=width)


def _geom_outline(draw, geometry, x0, y0, colour, width=1):
    for poly in iter_polygons(geometry):
        for ring in [poly.exterior, *poly.interiors]:
            _outline(draw, list(ring.coords), x0, y0, colour, width, closed=False)


def render_proposal(scene_id, record, objects, scene, reserve, medial, path, ctx_res=None):
    """Raw panel (imagery + the band, 1 px, no fill) above/beside the annotated panel (modal polygon yellow, band blue, axis
    red, kept sections cyan, dropped sections magenta (tree) / white (frame) / orange (other linear), cars under the band green,
    other cars dark green, trees olive, reserve boundary orange-brown, reserve medial line orange)."""
    d = ROOT/'data/scenes'/scene_id
    image = Image.open(d/f"{scene['image_sha256']}.png").convert('RGB')
    W, H = image.size
    by_id = {o['id']: o for o in objects}
    p = record.get('proposal') or {'id': 'exit-'+str(record.get('exit')), 'ring': record.get('band_ring') or [], 'cars_under': [
        c['id'] for c in record.get('cars_under') or []], 'replaces': [], 'replaces_on_accept': []}
    ring = p['ring']
    seed = by_id[record['seed']]
    xs = [x for x, _ in ring]+[x for x, _ in seed['ring']]
    ys = [y for _, y in ring]+[y for _, y in seed['ring']]
    m = 30
    x0, y0 = max(0, int(min(xs))-m), max(0, int(min(ys))-m)
    x1, y1 = min(W, int(math.ceil(max(xs)))+m), min(H, int(math.ceil(max(ys)))+m)
    crop = image.crop((x0, y0, x1, y1))
    raw = crop.copy()
    _outline(ImageDraw.Draw(raw), ring, x0, y0, (0, 255, 255))
    ann = crop.copy()
    da = ImageDraw.Draw(ann)
    if reserve is not None:
        _geom_outline(da, reserve, x0, y0, (190, 120, 40))
    for x, y, _ in medial:
        if x0 <= x < x1 and y0 <= y < y1:
            da.point((x-x0, y-y0), fill=(255, 150, 0))
    under = set(p['cars_under'])
    for o in objects:
        if o.get('status') in DEAD:
            continue
        if o['kind'] == 'car':
            _outline(da, o['ring'], x0, y0, (0, 255, 0) if o['id'] in under else (0, 110, 0), 2 if o['id'] in under else 1)
        elif o['kind'] == 'tree':
            _outline(da, o['ring'], x0, y0, (150, 160, 40))
    for oid in [record['seed'], *record['siblings']]:
        o = by_id[oid]
        _outline(da, o['ring'], x0, y0, (255, 255, 0))
        for h in o.get('holes') or []:
            _outline(da, h, x0, y0, (255, 200, 0))
    if ctx_res is not None:
        for j, s in enumerate(ctx_res.get('sections') or []):
            if s['outside'] or j % 2:
                continue
            a, b = s['p']+s['n']*s['lo'], s['p']+s['n']*s['hi']
            colour = (0, 220, 255) if s['clean'] else ((255, 0, 255) if 'tree' in s['ends'] else (255, 255, 255) if 'frame' in s['ends'] else (255, 140, 0))
            da.line([(a[0]-x0, a[1]-y0), (b[0]-x0, b[1]-y0)], fill=colour, width=1)
    _outline(da, ring, x0, y0, (60, 120, 255), 2)
    axis = record.get('axis_px') or []
    if len(axis) >= 2:
        da.line([(x-x0, y-y0) for x, y in axis], fill=(255, 0, 0), width=2)
    w, h = crop.size
    header = 34
    layout = 'stacked' if w >= h else 'side'
    if w >= h:
        sheet = Image.new('RGB', (w, 2*h+header+4), (20, 20, 20))
        sheet.paste(raw, (0, header))
        sheet.paste(ann, (0, header+h+4))
    else:
        sheet = Image.new('RGB', (2*w+4, h+header), (20, 20, 20))
        sheet.paste(raw, (0, header))
        sheet.paste(ann, (w+4, header))
    ds = ImageDraw.Draw(sheet)
    ds.text((4, 2), f'{scene_id} {p["id"]} seed {record["seed"]} [{record["seed_status"]}] crop x{x0}-{x1} y{y0}-{y1}', fill=(255, 255, 255))
    ds.text((4, 17), f'width {record.get("width_m")} m  vertices {record["vertices_before"]}->{len(ring)}  IoU modal {record.get("iou_modal")} '
                     f'amodal {record.get("iou_amodal")}  cars under {record.get("cars_under_n")}  replaces {len(p["replaces"])}+{len(p["replaces_on_accept"])} on accept'
                     + (f'  EXIT {record.get("exit_reasons")}' if record.get('exit') else ''),
            fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return sheet, layout, (w, h)


def render(args):
    if (getattr(args, 'design', None) or 'v1') == 'v3':
        return render_v3(args)
    if (getattr(args, 'design', None) or 'v1') == 'v3.1':
        return render_v31(args)
    if (getattr(args, 'design', None) or 'v1') != 'v1':
        return render_v2(args)
    run_dir = OUT/'run-v1'
    rdir = OUT/'renders'
    rdir.mkdir(parents=True, exist_ok=True)
    files = sorted(run_dir.glob('*.review.json'))
    entries = []
    beat(f'render start: {len(files)} review files')
    for f in files:
        rep = json.load(open(f))
        if not rep['proposals']:
            continue
        scene_id = rep['scene']
        scene, review = load_scene(scene_id)
        reserve, lots, _ = scene_inputs(scene)
        mpp = scene_mpp(scene)
        objects = review['objects']
        _, ctx, cache = corridor_layer(objects, width=scene['image_width'], height=scene['image_height'], mpp=mpp,
                                       reserve=reserve, lots=lots, design=DESIGN, affine=scene['pixel_to_meters'])
        for rec in rep['proposals']:
            res = cache.get((rec['seed'], DESIGN['adjacency_m'], DESIGN['segment_tol_m'], DESIGN['snap_deg']))
            path = rdir/scene_id/f'{rec["proposal"]["id"]}.png'
            _, layout, size = render_proposal(scene_id, rec, objects, scene, reserve, ctx['medial'], path, res)
            entries.append({'layout': layout, 'crop_size': list(size), 'scene': scene_id, 'proposal': rec['proposal']['id'], 'seed': rec['seed'],
                            'seed_status': rec['seed_status'], 'file': str(path.relative_to(OUT)), 'width_m': rec['width_m'],
                            'vertices': f'{rec["vertices_before"]}->{rec["vertices_after"]}', 'iou_modal': rec['iou_modal'],
                            'iou_amodal': rec['iou_amodal'], 'cars_under': rec['cars_under_n'],
                            'replaces': rec['proposal']['replaces'], 'replaces_on_accept': rec['proposal']['replaces_on_accept'],
                            'xclass_twin': [f'{x["kind"]} {x["id"]} ({x["status"]}, {"kept" if x["decided"] else "absorbed"})' for x in rec['proposal']['xclass_twin']]})
        beat(f'render {scene_id}: {len(rep["proposals"])}')
    order = marks_order(entries)
    contact_sheets(order, rdir)
    write_marks(order)
    json.dump(order, open(rdir/'index.json', 'w'), indent=1)
    beat(f'render done: {len(order)} proposals')
    print(len(order), 'renders')


def marks_order(entries):
    pos_ids = {oid for ids in POSITIVES.values() for oid in ids}

    def key(e):
        if e['seed'] == 'e31f2cb143c1b8dc':
            return (0, '', '')
        if e['seed'] in pos_ids or any(i in pos_ids for i in e['replaces']):
            return (1, e['scene'], e['proposal'])
        return (2, e['scene'], e['proposal'])
    return sorted(entries, key=key)


def contact_sheets(order, rdir, per_sheet=12, thumb=300):
    for k in range(0, len(order), per_sheet):
        chunk = order[k:k+per_sheet]
        cols = 4
        rows = math.ceil(len(chunk)/cols)
        sheet = Image.new('RGB', (cols*(thumb+6), rows*(thumb+22)), (15, 15, 15))
        ds = ImageDraw.Draw(sheet)
        for i, e in enumerate(chunk):
            im = Image.open(OUT/e['file'])
            # the raw panel only (top or left half)
            cw, ch = e['crop_size']
            raw = im.crop((0, 34, cw, 34+ch))
            raw.thumbnail((thumb, thumb))
            cx, cy = (i % cols)*(thumb+6), (i//cols)*(thumb+22)
            sheet.paste(raw, (cx, cy+18))
            ds.text((cx+2, cy+2), f'{k+i+1}. {e["scene"][:16]} w{e["width_m"]}', fill=(255, 255, 255))
        sheet.save(rdir/f'contact-{k//per_sheet+1:02d}.png')


def write_marks(order):
    lines = ['# MARKS: road corridor v1 (sidecar, 2026-10-07)', '',
             'Reviewer ask (7 Oct ~11:25): "roads need to converge on being straight lines of a fixed width etc. layer it underneath '
             'other objects like cars/trees etc." Each line is one proposed straight, fixed-width road band on the review layer. '
             'Nothing is applied: a band only replaces its road objects if you accept it (a confirmed road is replaced only on accept).',
             '', f'Assumption to confirm: {ASSUMPTION}', '',
             'How to answer in chat: one word per line number, e.g. `1 right, 2 wrong (too wide), 3 unclear`. right = the band is the '
             'carriageway (kerb to kerb, parking lanes included) and may replace the listed objects; wrong = the band is misplaced, too '
             'wide or narrow, or this is not a street; unclear = cannot tell from the render.',
             '', 'Render: top/left panel = imagery with the band outline (cyan, 1 px); other panel = modal polygon (yellow), band (blue), '
             'axis (red), kept sections (cyan), dropped sections (magenta tree / white frame / orange other class), cars under the band '
             '(bright green), reserve edge (brown) and medial line (orange).', '']
    for n, e in enumerate(order, 1):
        twins = f'; twins {", ".join(e["xclass_twin"])}' if e['xclass_twin'] else ''
        acc = f'; replaces on accept {", ".join(e["replaces_on_accept"])}' if e['replaces_on_accept'] else ''
        lines.append(f'{n}. `{e["scene"]}` band `{e["proposal"]}` from road `{e["seed"]}` ({e["seed_status"]}): width {e["width_m"]} m, '
                     f'vertices {e["vertices"]}, cars under {e["cars_under"]}; replaces {", ".join(e["replaces"]) or "-"}{acc}{twins}. '
                     f'Render `prototype/{e["file"]}`. Mark: ____')
    (OUT/'MARKS-roads-v1.md').write_text('\n'.join(lines)+'\n')


# --- v2: by-eye, v1 vs v2, renders, marks ----------------------------------------------------------------------------------

# The by-eye skeptic's marks on the 33 v1 review-layer proposals (.run/session-2026-10-07-roads/eye-tally-roads.md), keyed by the
# first 6 hex of the v1 band id: (MARKS-roads-v1 number, mark, note). 'partial' = right where it is but stops at a canopy.
V1_EYE_MARKS = {
    '70f54d': (1, 'marginal', 'west edge 0.6-1.0 m onto the shadowed footpath; band 12.31 m vs 11.2 m by eye'),
    '79d88b': (2, 'right', 'kerb to kerb incl. the hatched island'), 'fa044c': (3, 'right', 'both kerbs within 0.4 m'),
    '632248': (4, 'right', 'arterial kerb to kerb'), '34019e': (5, 'wrong', 'curved street forced straight'),
    'b3144b': (6, 'partial', 'stops at the canopy 14 m before the frame'), '9df48d': (7, 'right', '8.76 m vs 8.77 m asphalt'),
    '6121c7': (8, 'marginal', 'offset 0.6 m west; footpath and vegetation inside'),
    'af3e3c': (9, 'wrong', 'canopy hides half the street; axis jogs'), '1a6885': (10, 'wrong', 'slice of a 12 m carriageway'),
    'b4db3b': (11, 'wrong', 'rotated about 5 degrees from the kerbs'), 'f4b343': (12, 'unclear', 'SW kerb under canopy'),
    '059b5b': (13, 'right', 'kerbs -4.0 / +4.2 vs band +-3.88'), '7d06ef': (14, 'partial', '30 m fragment between canopies'),
    '18658c': (15, 'wrong', 'car-park aisle'), '64e7ce': (16, 'right', 'kerb to kerb, double centre line'),
    '42d681': (17, 'right', 'rear laneway (road or driveway: reviewer call)'), '4372a4': (18, 'right', 'rear laneway'),
    '6fccb4': (19, 'right', 'rear laneway; overshoots 5 m across the footpath'), '8fb835': (20, 'right', 'rear laneway'),
    '1b4a30': (21, 'wrong', 'slice at the frame bottom'), '4a47b1': (22, 'right', 'kerb to kerb incl. parked cars'),
    'a87bd5': (23, 'marginal', 'west edge 0.8-1.1 m onto the footpath at the south'),
    '8ebe7e': (24, 'unclear', 'one lane: service lane or slice'), 'a694e0': (25, 'partial', 'stops at the NW canopy'),
    '1e89d4': (26, 'partial', '3.5 m lane between canopies'), 'b2a4a0': (27, 'partial', 'stops under the canopy'),
    'c2189a': (28, 'unclear', 'unsealed right-of-way'), '9c5456': (29, 'unclear', 'flared lane mouth, no reserve'),
    'd0e02d': (30, 'wrong', 'curved street forced straight'), '42fe08': (31, 'wrong', 'the car park of #15 again'),
    '3fc23b': (32, 'wrong', '1.06 m narrower than #7 on the same street'), '4adbf2': (33, 'wrong', 'axis 20 degrees off; slice at the frame top'),
}


def load_byeye_new():
    path = SESSION/'v2/byeye/new-readings.json'
    return json.load(open(path)) if path.exists() else None


def _band_poly(rec):
    ring = (rec.get('proposal') or {}).get('ring') or rec.get('band_ring') or []
    return Polygon(ring) if len(ring) >= 3 else None


def compare_v1_v2(run_v2, names):
    """For every v1 review-layer proposal: identical / changed / gone in v2 (band IoU >= 0.99 and width within 0.01 m =
    identical; a v2 band whose lineage holds the v1 seed or overlapping the v1 band at IoU >= 0.3 = changed), with the v1 eye
    mark; v2 proposals matching no v1 proposal are 'new'."""
    rows, new = [], []
    for scene_id in names:
        f1, f2 = OUT/'run-v1'/f'{scene_id}.review.json', run_v2/f'{scene_id}.review.json'
        if not (f1.exists() and f2.exists()):
            continue
        r1, r2 = json.load(open(f1)), json.load(open(f2))
        used = set()
        for p1 in r1['proposals']:
            b1 = _band_poly(p1)
            pid1 = p1['proposal']['id']
            mark = V1_EYE_MARKS.get(pid1[:6], (None, None, None))
            best = None
            for p2 in r2['proposals']:
                b2 = _band_poly(p2)
                v = iou(b1, b2) if (b1 is not None and b2 is not None) else 0.
                lineage = p1['seed'] in p2['proposal']['derived_from']
                if (lineage or v >= .3) and (best is None or v > best[0]):
                    best = (v, p2)
            row = {'scene': scene_id, 'v1_number': mark[0], 'v1_band': pid1, 'v1_seed': p1['seed'], 'v1_width_m': p1['width_m'],
                   'eye_mark': mark[1], 'eye_note': mark[2]}
            if best is None:
                ex = next((e for e in r2['exits'] if e['seed'] == p1['seed']), None)
                row.update(v2='gone', v2_exit=ex['exit'] if ex else 'not_seeded', v2_exit_reasons=ex.get('exit_reasons') if ex else None)
            else:
                v, p2 = best
                used.add(p2['proposal']['id'])
                same = v >= .99 and abs((p2['width_m'] or 0)-(p1['width_m'] or 0)) <= .01
                row.update(v2='identical' if same else 'changed', v2_band=p2['proposal']['id'], v2_seed=p2['seed'], v2_width_m=p2['width_m'],
                           band_iou_v1_v2=r(v), v2_width_source=p2.get('width_source'), v2_joined=p2.get('joined'),
                           v2_retires_on_accept=p2['proposal'].get('retires_on_accept'))
            rows.append(row)
        for p2 in r2['proposals']:
            if p2['proposal']['id'] not in used:
                new.append({'scene': scene_id, 'v2_band': p2['proposal']['id'], 'v2_seed': p2['seed'], 'v2_width_m': p2['width_m'],
                            'joined': p2.get('joined'), 'junction_arm': bool((p2.get('junction') or {}).get('arm_a'))})
    by_mark = defaultdict(Counter)
    for row in rows:
        by_mark[str(row['eye_mark'])][row['v2']] += 1
    return {'rows': rows, 'new_v2_proposals': new, 'counts_by_v1_eye_mark': {k: dict(v) for k, v in sorted(by_mark.items())},
            'counts': dict(Counter(row['v2'] for row in rows)), 'new_count': len(new),
            'gone_exits': dict(Counter(row['v2_exit'] for row in rows if row['v2'] == 'gone'))}


def v2_summary(out, names, totals):
    """metrics.json additions for a v2 run: exits by reason per layer, the v1 vs v2 table and the by-eye width errors
    (in-sample on the 5 roads of kerb-widths-by-eye.json; out of sample on the new readings)."""
    exits = {layer: {k.split(':', 1)[1]: v for k, v in sorted(t.items()) if k.startswith('exit:')} for layer, t in totals.items()}
    byeye_rows, new_rows = [], []
    for scene_id in names:
        f = out/f'{scene_id}.review.json'
        if f.exists():
            rep = json.load(open(f))
            byeye_rows += [{'scene': scene_id, **b} for b in rep.get('byeye') or []]
            new_rows += [{'scene': scene_id, **b} for b in rep.get('byeye_new') or []]

    def agg(rows):
        have = [b for b in rows if b.get('proposal')]
        return {'objects': len(rows), 'with_band': len(have),
                'width_abs_error_m': r(np.mean([b['width_abs_error_m'] for b in have])) if have else None,
                'width_signed_error_m': r(np.mean([b['width_error_m'] for b in have])) if have else None,
                'kerb_edge_abs_error_m': r(np.mean([b['kerb_edge_abs_error_m'] for b in have if b.get('kerb_edge_abs_error_m') is not None]))
                if any(b.get('kerb_edge_abs_error_m') is not None for b in have) else None, 'rows': rows}
    return {'exits_by_reason': exits, 'v1_vs_v2': compare_v1_v2(out, names),
            'byeye_in_sample': {**agg(byeye_rows), 'note': 'IN-SAMPLE: these 5 roads informed the v1 default and were read before v2 was designed'},
            'byeye_new': {**agg(new_rows), 'note': 'out of sample: 5 readings on 2 roads taken before any v2 band was computed (v2/byeye/new-readings.json)'}}


def render_proposal_v2(scene_id, record, objects, scene, reserve, medial, path, ctx_res=None, ghost=None, title=None):
    """Raw panel LEFT (imagery + the band outline, cyan 1 px, no fill) and the annotated panel RIGHT: as v1 (modal yellow, band
    blue, axis red, kept sections cyan, dropped magenta tree / white frame / orange other, cars under green, reserve brown and
    medial orange) plus: retires-on-accept objects ORANGE (thick), joined pieces light yellow, the junction remainder pink, and
    (for an exit sheet) the v1 band dashed magenta."""
    d = ROOT/'data/scenes'/scene_id
    image = Image.open(d/f"{scene['image_sha256']}.png").convert('RGB')
    W, H = image.size
    by_id = {o['id']: o for o in objects}
    p = record.get('proposal') or {'id': 'exit-'+str(record.get('exit')), 'ring': record.get('band_ring') or [],
                                   'cars_under': [c['id'] for c in record.get('cars_under') or []], 'replaces': [], 'replaces_on_accept': [],
                                   'retires_on_accept': [t['id'] for t in record.get('retires_on_accept') or []], 'remainder_on_accept': []}
    ring = p['ring']
    seed = by_id[record['seed']]
    pts = list(ring)+list(seed['ring'])+list(ghost or [])
    for jid in record.get('joined') or []:
        pts += by_id[jid]['ring']
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    m = 30
    x0, y0 = max(0, int(min(xs))-m), max(0, int(min(ys))-m)
    x1, y1 = min(W, int(math.ceil(max(xs)))+m), min(H, int(math.ceil(max(ys)))+m)
    crop = image.crop((x0, y0, x1, y1))
    raw = crop.copy()
    if ring:
        _outline(ImageDraw.Draw(raw), ring, x0, y0, (0, 255, 255))
    ann = crop.copy()
    da = ImageDraw.Draw(ann)
    if reserve is not None:
        _geom_outline(da, reserve, x0, y0, (190, 120, 40))
    for x, y, _ in medial:
        if x0 <= x < x1 and y0 <= y < y1:
            da.point((x-x0, y-y0), fill=(255, 150, 0))
    under = set(p['cars_under'])
    for o in objects:
        if o.get('status') in DEAD:
            continue
        if o['kind'] == 'car':
            _outline(da, o['ring'], x0, y0, (0, 255, 0) if o['id'] in under else (0, 110, 0), 2 if o['id'] in under else 1)
        elif o['kind'] == 'tree':
            _outline(da, o['ring'], x0, y0, (150, 160, 40))
    for oid in [record['seed'], *record.get('siblings', [])]:
        o = by_id[oid]
        _outline(da, o['ring'], x0, y0, (255, 255, 0))
    for jid in record.get('joined') or []:
        _outline(da, by_id[jid]['ring'], x0, y0, (255, 255, 160))
    for rem in p.get('remainder_on_accept') or []:
        _outline(da, rem['ring'], x0, y0, (255, 120, 200), 2)
    if ctx_res is not None:
        for j, s in enumerate(ctx_res.get('sections') or []):
            if s['outside'] or j % 2:
                continue
            a, b = s['p']+s['n']*s['lo'], s['p']+s['n']*s['hi']
            colour = (0, 220, 255) if s['clean'] else ((255, 0, 255) if 'tree' in s['ends'] else (255, 255, 255) if 'frame' in s['ends'] else (255, 140, 0))
            da.line([(a[0]-x0, a[1]-y0), (b[0]-x0, b[1]-y0)], fill=colour, width=1)
    if ghost:
        g = [(x-x0, y-y0) for x, y in ghost]+[(ghost[0][0]-x0, ghost[0][1]-y0)]
        for k in range(len(g)-1):
            seg_len = math.dist(g[k], g[k+1])
            for t in np.arange(0, seg_len, 8.):
                a = np.asarray(g[k])+(np.asarray(g[k+1])-np.asarray(g[k]))*t/max(seg_len, 1e-6)
                b = np.asarray(g[k])+(np.asarray(g[k+1])-np.asarray(g[k]))*min(t+4., seg_len)/max(seg_len, 1e-6)
                da.line([tuple(a), tuple(b)], fill=(255, 0, 255), width=1)
    if ring:
        _outline(da, ring, x0, y0, (60, 120, 255), 2)
    for tid in p.get('retires_on_accept') or []:
        if tid in by_id:
            _outline(da, by_id[tid]['ring'], x0, y0, (255, 140, 0), 3)
    axis = record.get('axis_px') or []
    if len(axis) >= 2 and ring:
        da.line([(x-x0, y-y0) for x, y in axis], fill=(255, 0, 0), width=2)
    w, h = crop.size
    header = 34
    sheet = Image.new('RGB', (2*w+4, h+header), (20, 20, 20))
    sheet.paste(raw, (0, header))
    sheet.paste(ann, (w+4, header))
    ds = ImageDraw.Draw(sheet)
    ds.text((4, 2), title or f'{scene_id} {p["id"]} seed {record["seed"]} [{record["seed_status"]}] crop x{x0}-{x1} y{y0}-{y1}', fill=(255, 255, 255))
    line2 = (f'width {record.get("width_m")} m ({record.get("width_source")}, p75 {record.get("width_p75_m")})  vertices '
             f'{record["vertices_before"]}->{len(ring)}  cars under {record.get("cars_under_n")}  joined {len(record.get("joined") or [])}  '
             f'retires {len(p.get("retires_on_accept") or [])}  remainder {len(p.get("remainder_on_accept") or [])}')
    if record.get('exit'):
        line2 = f'EXIT {record.get("exit_reasons")}  ' + line2
    ds.text((4, 17), line2, fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return sheet, (w, h)


def render_v2(args):
    """Renders of every v2 review-layer proposal (renders-v2/<scene>/<band>.png), exit sheets of every v1 proposal gone in v2
    (renders-v2/gone/<scene>-<v1 band>.png, the v1 band dashed magenta), contact sheets, index.json and MARKS-roads-v2.md."""
    run_dir = OUT/'run-v2'
    rdir = OUT/'renders-v2'
    rdir.mkdir(parents=True, exist_ok=True)
    metrics = json.load(open(run_dir/'metrics.json'))
    table = metrics['v1_vs_v2']
    hints = {row['v2_band']: row for row in table['rows'] if row.get('v2_band')}
    gone = defaultdict(list)
    for row in table['rows']:
        if row['v2'] == 'gone':
            gone[row['scene']].append(row)
    files = sorted(run_dir.glob('*.review.json'))
    entries, gone_entries = [], []
    beat(f'render-v2 start: {len(files)} review files')
    for f in files:
        rep = json.load(open(f))
        scene_id = rep['scene']
        if not rep['proposals'] and not gone.get(scene_id):
            continue
        scene, review = load_scene(scene_id)
        reserve, lots, _ = scene_inputs(scene)
        mpp = scene_mpp(scene)
        objects = review['objects']
        _, ctx, cache = corridor_layer(objects, width=scene['image_width'], height=scene['image_height'], mpp=mpp,
                                       reserve=reserve, lots=lots, design=DESIGN_V2, affine=scene['pixel_to_meters'])
        for rec in rep['proposals']:
            res = cache.get(('v2', rec['seed'], tuple(rec.get('joined') or []), DESIGN_V2['adjacency_m'], DESIGN_V2['segment_tol_m'], DESIGN_V2['snap_deg']))
            path = rdir/scene_id/f'{rec["proposal"]["id"]}.png'
            _, size = render_proposal_v2(scene_id, rec, objects, scene, reserve, ctx['medial'], path, res)
            entries.append({'crop_size': list(size), 'scene': scene_id, 'proposal': rec['proposal']['id'], 'seed': rec['seed'],
                            'seed_status': rec['seed_status'], 'file': str(path.relative_to(OUT)), 'width_m': rec['width_m'],
                            'width_source': rec.get('width_source'), 'width_p75_m': rec.get('width_p75_m'), 'width_shared': bool(rec.get('width_shared')),
                            'vertices': f'{rec["vertices_before"]}->{rec["vertices_after"]}', 'cars_under': rec['cars_under_n'],
                            'replaces': rec['proposal']['replaces'], 'replaces_on_accept': rec['proposal']['replaces_on_accept'],
                            'retires_on_accept': rec['proposal']['retires_on_accept'], 'joined': rec.get('joined') or [],
                            'remainder': len(rec['proposal']['remainder_on_accept']), 'junction_arm': bool((rec.get('junction') or {}).get('arm_a')),
                            'hint': hints.get(rec['proposal']['id'])})
        for row in gone.get(scene_id, []):
            ex = next((e for e in rep['exits'] if e['seed'] == row['v1_seed']), None)
            if ex is None:
                continue
            v1rep = json.load(open(OUT/'run-v1'/f'{scene_id}.review.json'))
            ghost = next(p['proposal']['ring'] for p in v1rep['proposals'] if p['proposal']['id'] == row['v1_band'])
            res = None
            path = rdir/'gone'/f'{scene_id}-{row["v1_band"]}.png'
            title = f'{scene_id} v1 #{row["v1_number"]} band {row["v1_band"]} (eye: {row["eye_mark"]}) GONE in v2: {row["v2_exit"]}'
            _, size = render_proposal_v2(scene_id, ex, objects, scene, reserve, ctx['medial'], path, res, ghost=ghost, title=title)
            gone_entries.append({**row, 'file': str(path.relative_to(OUT))})
        beat(f'render-v2 {scene_id}: {len(rep["proposals"])}')
    order = marks_order_v2(entries)
    contact_sheets_v2(order, rdir)
    write_marks_v2(order, gone_entries, metrics)
    json.dump({'proposals': order, 'gone': gone_entries}, open(rdir/'index.json', 'w'), indent=1, default=str)
    beat(f'render-v2 done: {len(order)} proposals, {len(gone_entries)} gone sheets')
    print(len(order), 'renders', len(gone_entries), 'gone sheets')


def marks_order_v2(entries):
    pos_ids = {oid for ids in POSITIVES.values() for oid in ids}

    def key(e):
        if e['seed'] == 'e31f2cb143c1b8dc' or 'e31f2cb143c1b8dc' in e['replaces_on_accept']:
            return (0, '', '')
        if e['seed'] in pos_ids or any(i in pos_ids for i in e['replaces']):
            return (1, e['scene'], e['proposal'])
        return (2, e['scene'], e['proposal'])
    return sorted(entries, key=key)


def contact_sheets_v2(order, rdir, per_sheet=12, thumb=300):
    for k in range(0, len(order), per_sheet):
        chunk = order[k:k+per_sheet]
        cols = 4
        rows = math.ceil(len(chunk)/cols)
        sheet = Image.new('RGB', (cols*(thumb+6), rows*(thumb+22)), (15, 15, 15))
        ds = ImageDraw.Draw(sheet)
        for i, e in enumerate(chunk):
            im = Image.open(OUT/e['file'])
            cw, ch = e['crop_size']
            raw = im.crop((0, 34, cw, 34+ch))
            raw.thumbnail((thumb, thumb))
            cx, cy = (i % cols)*(thumb+6), (i//cols)*(thumb+22)
            sheet.paste(raw, (cx, cy+18))
            ds.text((cx+2, cy+2), f'{k+i+1}. {e["scene"][:16]} w{e["width_m"]}', fill=(255, 255, 255))
        sheet.save(rdir/f'contact-{k//per_sheet+1:02d}.png')


def write_marks_v2(order, gone_entries, metrics):
    t = metrics['v1_vs_v2']
    lines = ['# MARKS: road corridor v2 (sidecar, 2026-10-07)', '',
             'Reviewer ask (7 Oct ~11:25): "roads need to converge on being straight lines of a fixed width etc. layer it underneath '
             'other objects like cars/trees etc." Each numbered line is one proposed straight, fixed-width road band on the review '
             'layer. Nothing is applied: a band replaces its road objects only if you accept it (a confirmed road is replaced only '
             'on accept). SAM3 cannot give this shape (probe-v1: masks stay modal; lower thresholds bleed 7-24x onto the footpath), '
             'so these are geometry proposals.', '',
             f'Assumption to confirm: {ASSUMPTION}', '',
             'What changed since v1 (MARKS-roads-v1.md): slices, car parks and curved streets now exit; no band where the frame has '
             'no road reserve (this removes the four rear laneways of v1 #17-20); bands go on under a canopy to the next road piece '
             'on the same axis; collinear pieces of one street join and one street gets one width; width = the median kerb-to-kerb '
             'section with the cars unioned unbuffered (mirrored from the reserve centre line where a canopy hides one kerb); a '
             'junction seed gets a band on its longest straight arm and the other arms stay as they are; candidate footpath or '
             'grass inside a band trims its edge or stops it; a decided object blocks a band, except a decided twin of the same '
             'street, which "retires on accept" (outlined ORANGE).', '',
             'How to answer in chat: one word per line number, e.g. `1 right, 2 wrong (too wide), 3 unclear`. right = the band is '
             'the carriageway (kerb to kerb, parking lanes included) and may replace the listed objects (and retire the orange '
             'ones); wrong = misplaced, too wide or narrow, or not a street; unclear = cannot tell from the render. For the "gone '
             'since v1" list, answer `G<n> agree` or `G<n> should have a band`.', '',
             'HINT = the agent by-eye skeptic mark on the matching v1 band (eye-tally-roads.md), not your mark.', '',
             'Render: LEFT panel = imagery with the band outline (cyan, 1 px, no fill); RIGHT panel = modal polygon (yellow), joined '
             'pieces (light yellow), band (blue), axis (red), kept sections (cyan), dropped sections (magenta tree / white frame / '
             'orange other), cars under the band (bright green), retires-on-accept objects (ORANGE, thick), junction remainder kept '
             'as it is (pink), reserve edge (brown) and medial line (orange dots).', '',
             f'v1 -> v2: {t["counts"]}; new in v2: {t["new_count"]}. By v1 eye mark: {t["counts_by_v1_eye_mark"]}.', '']
    for n, e in enumerate(order, 1):
        h = e.get('hint')
        hint = (f' HINT v1 #{h["v1_number"]} eye {h["eye_mark"]} ({h["eye_note"]}); v1 width {h["v1_width_m"]} m.' if h else ' HINT: new in v2 (no v1 band).')
        extra = []
        if e['replaces_on_accept']:
            extra.append(f'replaces on accept {", ".join(e["replaces_on_accept"])}')
        if e['retires_on_accept']:
            extra.append(f'RETIRES on accept {", ".join(e["retires_on_accept"])} (orange)')
        if e['joined']:
            extra.append(f'joins {len(e["joined"])} piece(s) {", ".join(e["joined"])}')
        if e['junction_arm']:
            extra.append(f'junction: longest straight arm, {e["remainder"]} other arm piece(s) stay as they are')
        if e['width_shared']:
            extra.append('width shared with the other band(s) of this street')
        lines.append(f'{n}. `{e["scene"]}` band `{e["proposal"]}` from road `{e["seed"]}` ({e["seed_status"]}): width {e["width_m"]} m '
                     f'({e["width_source"]}, p75 {e["width_p75_m"]}), vertices {e["vertices"]}, cars under {e["cars_under"]}; replaces '
                     f'{", ".join(e["replaces"]) or "-"}' + (f'; {"; ".join(extra)}' if extra else '') + f'.{hint} '
                     f'Render `prototype/{e["file"]}`. Mark: ____')
    lines += ['', '## Gone since v1 (no v2 band; the v1 band is drawn dashed magenta on the exit sheet)', '']
    for n, g in enumerate(gone_entries, 1):
        lines.append(f'G{n}. `{g["scene"]}` v1 #{g["v1_number"]} band `{g["v1_band"]}` (HINT eye {g["eye_mark"]}: {g["eye_note"]}): '
                     f'v2 exit `{g["v2_exit"]}` ({", ".join(g.get("v2_exit_reasons") or [])}). Sheet `prototype/{g["file"]}`. Agree: ____')
    (OUT/'MARKS-roads-v2.md').write_text('\n'.join(lines)+'\n')


# === road corridor v3 (design name 'v3', 2026-10-07 evening) ===============================================================
# v1 and v2 above are untouched: DESIGN, DESIGN_V2 and every v1 / v2 function are unchanged, so `--design v1` and
# `--design v2` reproduce prototype/run-v1 and prototype/run-v2. v3 is a third layer function (corridor_layer dispatches on
# design['generation']) that reuses the v2 seed analysis and helpers. Reviewer ask (7 Oct ~11:25): "roads need to converge on
# being straight lines of a fixed width etc. layer it underneath other objects like cars/trees etc." What v3 changes, from
# the v2 by-eye tally (eye-tally-roads-v2.md) and the v2 code skeptic (v2/skeptic-notes.md), numbered as in the ask:
#   1 one street   windows overlap. Bands are georeferenced through scene_recapture.frame_transform (scene centroid +
#                  pixel_to_meters, the recapture / priors convention). Proposals of the same layer kind in two overlapping
#                  windows whose axes are collinear (within xwin_deg, each within xwin_lateral_m of the other's line) and
#                  overlap >= xwin_common_m along the street are the same reserve segment (xwin_match); they pool their
#                  clean stations: one width (the pooled median) and one axis offset (the pooled median of the stations' own
#                  centre points) for all of them; 'shared_with' names the other windows' bands. A third pass (xwin_retrim)
#                  narrows the whole group by the deepest footpath edge trim any member window needed, so a window's own
#                  guard does not split the street's width again. `run --design v3` runs the passes (run_v3).
#   2 outer kerb   per station each kerb-class mask edge (free / reserve_edge / verge) is extended outward to the kerb read
#                  in the frame PNG: the luminance profile across the edge (mean of the kerb_along_m lines) shows a light
#                  strip (>= kerb_light above the asphalt just inside the edge) ending in a fall of >= kerb_drop within
#                  kerb_max_m beyond the mask edge and inside the reserve; the strongest such fall is the outer kerb (the back
#                  of the kerb and channel). A side whose fall is found on >= kerb_min_share of the clean stations (and on
#                  >= kerb_min_found) is extended at every clean station (by the side's median extension where the fall is
#                  not seen, never past that station's stop); a side below that keeps the mask edge ('kerb_not_found:<side>');
#                  neither side -> the v2 median ('kerb_not_found'). The offset keeps the v2 rule (the reserve medial line
#                  when snapped and the frame does not cut the reserve, else the median midpoint of the extended edges). kerb_drop / kerb_light were set on the IN-SAMPLE by-eye stations only (v3/proto/kerb_proto.py).
#                  The extension never enters another object: it stops before the first footpath / vegetation / driveway /
#                  building / pool / water object or decided object of any class but road / car / tree on the ray (the seed's
#                  lineage and twins excepted), so the outer kerb cannot push a band onto a footpath (the v2 guards stand).
#   3 canopy       (a) a collinear road piece of ANY reserve segment joins across a gap when the gap is canopy (tree /
#                  vegetation >= cont_canopy_min of it), road and cars, with unlabelled pavement <= cont_unlabelled_max_m along,
#                  the bridge >= join_reserve_frac inside the reserve and no building / decided object / other-class candidate
#                  > cont_other_m2 in it; (b) after the band is built, an end whose next cont_head_m are canopy continues along
#                  the axis to the next road piece on the axis, the next band, or the frame edge (<= cont_max_m), on the same
#                  path conditions. The continued part is drawn dashed.
#   4 narrow       a band under narrow_min_m, or under narrow_reserve_ratio x (reserve width - 2 x footpath_allow_m), needs
#                  two-kerb evidence (the kerb fall found on BOTH sides) on >= narrow_two_kerb_min clean stations or a
#                  mirrored half-street width; else it exits 'lane_slice'.
#   5 cars         a car counts as under the band only when >= car_under_cover of it is ('cars_straddling' otherwise). A
#                  parked car (long axis within indent_car_deg of the axis) sticking out of one side by <= indent_max_m,
#                  along the band (not past an end), whose bay strip (band edge to the car's outer edge over the car +-
#                  indent_pad_m) is road or cars on >= indent_cover_min, inside the reserve and free of footpath / driveway /
#                  vegetation / building over indent_other_m2, widens that side ('indent_bay').
#   6 guards       tests pin the guards the v2 code skeptic found unpinned (test_road_corridor.py, v3 section).
#   + trim         (addition) the footpath edge trim takes the least depth that leaves <= footpath_eps_m2 of the blocker
#                  under the band (the guard as written), not v2's full clearance (trim_footpaths_v3): with outer-kerb widths
#                  one SAM footpath bulge over the kerb narrowed whole bands by 0.6-0.8 m on the in-sample roads.
#   + slice      the v2 narrow-slice ratio compares the band's MASK width (not the outer-kerb width) with the street's mask
#                  width (slice_check_v3).
#   + linked       in one window, a band whose canopy continuation reached the next road piece / band on its axis shares
#                  width and centre line with that piece's band (continuation_links; 'linked_with').

VERSION_V3 = 'road-corridor-v3'
DESIGN_V3 = {
    **DESIGN_V2, 'version': VERSION_V3, 'design_name': 'v3', 'generation': 3,
    # 1 one street across windows
    'xwin_match_m': 1.5, 'xwin_common_m': 10., 'xwin_deg': 10., 'xwin_lateral_m': 2.,
    # 2 outer kerb
    'kerb_max_m': .8, 'kerb_drop': 20., 'kerb_light': 5., 'kerb_step_m': .04, 'kerb_strip_m': (.04, .24),
    'kerb_along_m': (-.4, -.2, 0., .2, .4), 'kerb_ref_m': (-.9, -.3), 'kerb_min_found': 3, 'kerb_min_share': .3,
    'kerb_end_kinds': ('free', 'reserve_edge', 'verge'),
    'kerb_stop_kinds': ('footpath', 'vegetation', 'driveway', 'building', 'pool', 'water'),
    # 3 canopy continuation
    'canopy_kinds': ('tree', 'vegetation'), 'cont_max_m': 40., 'cont_other_m2': 2., 'cont_canopy_min': .3,
    'cont_unlabelled_max_m': 5., 'cont_head_m': 3., 'cont_head_canopy': .5, 'cont_piece_m2': 2., 'cont_min_m': 1.,
    # 4 narrow band against the reserve
    'narrow_min_m': 5., 'narrow_reserve_ratio': .5, 'footpath_allow_m': 2., 'narrow_two_kerb_min': 5,
    # 5 cars under the band, parking bays in kerb indents
    'car_under_cover': .9, 'car_straddle_min': .05, 'indent_max_m': 1., 'indent_car_deg': 20., 'indent_pad_m': 3.,
    'indent_cover_min': .6, 'indent_other_m2': .5, 'indent_other_kinds': ('footpath', 'driveway', 'vegetation', 'building'),
}
DESIGNS['v3'] = DESIGN_V3
V3_EXITS = ('lane_slice',)
HELDOUT = SESSION/'v3/heldout-widths.json'
# the agent by-eye marks on the v2 bands (eye-tally-roads-v2.md), keyed by the v2 band id prefix: (v2 #, mark, note)
V2_EYE_MARKS = {
    '073024': (1, 'right', 'e31f kerb to kerb, 11.56 vs 11.5 by eye'), '031363': (2, 'right', 'kerb to kerb'),
    '140584': (3, 'right', 'reaches the frame bottom under the canopy'), 'e9a741': (4, 'right', 'arterial frame to frame'),
    '905dd2': (5, 'marginal', 'd43d main: W edge 0.3-0.6 m inside the kerb'), 'd04d4c': (6, 'right', 'd43d arm under the canopy'),
    'b6568b': (7, 'right', 'one 108 m band, on the inner edge of the kerb and channel'),
    'dd2841': (8, 'wrong', '5.46 m on a ~6.1-6.9 m street that win-d43d bands at 6.14 m'),
    '96ac55': (9, 'marginal', 'a car parked in an indent ~35 % outside; E kerb ~0.85 m outside'),
    '92b2fc': (10, 'right', 'frame to frame, kerbs on the edges'),
    '160f40': (11, 'wrong', 'width step on one street (5.84 vs 6.34 m), not joined across a two-tree gap'),
    '85cd44': (12, 'partial', 'stops at a tree gap where the street goes on'), '378d29': (13, 'right', 'edges 0.3-0.5 m inside the outer kerbs'),
    '57d342': (14, 'right', 'parked cars on the N kerb under it'), '98559b': (15, 'right', 'W kerb on the band edge'),
    '9a479e': (16, 'partial', 'stops at the NW canopy'), 'ca4407': (17, 'partial', '3.43 m lane between canopies in a 13 m reserve'),
}


def luminance(image):
    """Luminance (0-255, float32) of an RGB(A) frame array or an already single-channel array."""
    a = np.asarray(image)
    if a.ndim == 3:
        a = a[..., :3].astype(np.float32)
        return a[..., 0]*.299+a[..., 1]*.587+a[..., 2]*.114
    return a.astype(np.float32)


# --- 2: the outer kerb in the image ----------------------------------------------------------------------------------------

def kerb_step(lum, p, d, n, edge_px, sign, mpp, design=DESIGN_V3, limit_m=None):
    """The outer kerb beyond one mask edge (sign = outward along n). Profile: the mean of the kerb_along_m lines along the
    station normal; asphalt level = median over kerb_ref_m (inside the edge). A candidate x in [-0.1 m, limit] is a fall from a
    light strip (mean over kerb_strip_m before x, >= kerb_light above the asphalt) to darker ground (mean over kerb_strip_m
    after x, >= kerb_drop below the strip); the strongest fall wins. Returns {'ext_m', 'drop', 'light'} (ext 0 when the fall
    lies at or inside the edge) or None (no fall, or the profile leaves the frame)."""
    lim = design['kerb_max_m'] if limit_m is None else min(design['kerb_max_m'], float(limit_m))
    if lum is None or lim < 0:
        return None
    step = design['kerb_step_m']
    k0, k1 = int(round(design['kerb_strip_m'][0]/step)), int(round(design['kerb_strip_m'][1]/step))
    r0, r1 = design['kerb_ref_m']
    i_lo = int(math.floor(min(r0, -.1)/step))-k1-1
    i_hi = int(math.ceil(lim/step))+k1+1
    s = np.arange(i_lo, i_hi+1)*step
    H, W = lum.shape
    d, n = np.asarray(d, float), np.asarray(n, float)
    rows = []
    for a in design['kerb_along_m']:
        q = np.asarray(p, float)[None, :]+n[None, :]*(edge_px+sign*s[:, None]/mpp)+d[None, :]*(a/mpp)
        if q[:, 0].min() < 0 or q[:, 1].min() < 0 or q[:, 0].max() > W-1 or q[:, 1].max() > H-1:
            return None
        rows.append(ndimage.map_coordinates(lum, [q[:, 1], q[:, 0]], order=1))
    P = np.mean(rows, axis=0)
    A = float(np.median(P[(s >= r0-1e-9) & (s <= r1+1e-9)]))
    best = None
    for i in range(len(s)):
        x = s[i]
        if x < -.1-1e-9 or x > lim+1e-9 or i-k1 < 0 or i+k1 >= len(s):
            continue
        strip = float(P[i-k1:i-k0+1].mean())
        beyond = float(P[i+k0:i+k1+1].mean())
        drop = strip-beyond
        if drop >= design['kerb_drop'] and strip-A >= design['kerb_light'] and (best is None or drop > best['drop']):
            best = {'ext_m': max(0., float(x)), 'drop': r(drop, 1), 'light': r(strip-A, 1)}
    return best


def kerb_extend(res, ctx, design=DESIGN_V3):
    """2: per inside station and kerb-class end, the kerb fall beyond the mask edge (s['kext'] = [lo, hi] metres or None),
    searched no further than the reserve piece's end on that side. Cached on res."""
    if design.get('cand_over_kerb_m') is not None:     # v3.1 (2 over kerb); DESIGN_V3 has no such key
        return kerb_extend_v31(res, ctx, design)
    if res.get('_kerb_done') or not res.get('sections'):
        return
    res['_kerb_done'] = True
    lum, mpp = ctx.get('lum'), ctx['mpp']
    from shapely import contains_xy
    own = {res['seed'], *res.get('siblings', []), *res.get('joined', []), *[m['id'] for m in res.get('twins', [])]}
    stops = [ctx['polys'][o['id']] for o in ctx['live'] if o['id'] in ctx['polys'] and o['id'] not in own
             and (o['kind'] in design['kerb_stop_kinds'] or (is_decided(o, design) and o['kind'] not in ('road', 'car', 'tree')))]
    stop = unary_union(stops) if stops else None
    probe = np.arange(0., design['kerb_max_m']+1e-9, design['kerb_step_m'])
    for s in res['sections']:
        if s['outside']:
            continue
        s['kext'] = [None, None]
        s['kroom'] = [0., 0.]
        if lum is None:
            continue
        d = res['segments'][s['seg']]['d']
        for side, sign in ((0, -1.), (1, 1.)):
            if s['ends'][side] not in design['kerb_end_kinds']:
                continue
            edge = s['lo'] if side == 0 else s['hi']
            r_end = s.get('r_lo') if side == 0 else s.get('r_hi')
            if r_end is None:
                continue
            room = ((edge-r_end) if side == 0 else (r_end-edge))*mpp
            if stop is not None:
                q = s['p'][None, :]+s['n'][None, :]*(edge+sign*probe[:, None]/mpp)
                hit = np.nonzero(contains_xy(stop, q[:, 0], q[:, 1]))[0]
                if len(hit):
                    room = min(room, float(probe[hit[0]])-design['kerb_step_m'])
            s['kroom'][side] = max(0., min(room, design['kerb_max_m']))
            got = kerb_step(lum, s['p'], d, s['n'], edge, sign, mpp, design, limit_m=room)
            if got is not None:
                s['kext'][side] = got['ext_m']


def build_band_v3(res, ctx, design=DESIGN_V3, width_override=None, offset_pts=None):
    """v2 build_band_v2 with the outer-kerb widths (2) and the cross-window offset (1). Width = the median of the extended
    clean stations (two_kerb), or of the extended mirrored stations; offset = the median extended midpoint ('kerb_mid') when
    a side found its kerb, else the v2 rule; a shared cross-window offset (offset_pts: pooled midpoints in this frame's px)
    overrides; width_override (cross-window, else the in-window segment) overrides the width."""
    mpp = ctx['mpp']
    secs = [s for s in res['sections'] if not s['outside']]
    clean = [s for s in secs if s['clean']]
    kerb = {'image': ctx.get('lum') is not None, 'clean': len(clean)}
    side_ext = {}
    for side, name in ((0, 'lo'), (1, 'hi')):
        found = [s['kext'][side] for s in clean if (s.get('kext') or [None, None])[side] is not None]
        kerb[f'found_{name}'] = len(found)
        ok = len(found) >= design['kerb_min_found'] and len(found) >= design['kerb_min_share']*max(1, len(clean))
        side_ext[side] = float(np.median(found)) if ok else None
        kerb[f'ext_{name}_m'] = r(side_ext[side], 3)
    kerb['two_kerb_n'] = sum(1 for s in clean if (s.get('kext') or [None, None])[0] is not None and s['kext'][1] is not None)
    for s in secs:
        ext = []
        for side in (0, 1):
            got = (s.get('kext') or [None, None])[side]
            if side_ext[side] is None or s['ends'][side] not in design['kerb_end_kinds']:
                ext.append(0.)
            else:                                           # the side's median where the fall is not seen, never past a stop
                ext.append(got if got is not None else min(side_ext[side], (s.get('kroom') or [0., 0.])[side]))
        s['lo_k'], s['hi_k'] = s['lo']-ext[0]/mpp, s['hi']+ext[1]/mpp
        s['w_k'] = (s['hi_k']-s['lo_k'])*mpp
        if s.get('w_mirror') is not None:
            side = 0 if s['ends'][1] == 'tree' else 1
            s['w_mirror_k'] = 2*abs((s['lo_k'] if side == 0 else s['hi_k'])-s['medial_off'])*mpp
    mirrored = [s for s in secs if s.get('w_mirror') is not None]
    out = {'sections_n': len(res['sections']), 'sections_inside': len(secs), 'sections_clean': len(clean),
           'sections_mirrored': len(mirrored), 'section_ends': dict(Counter(e for s in secs for e in s['ends']))}
    if len(clean) >= design['min_clean_sections'] and len(clean) >= design['mirror_max_two_kerb_share']*(len(clean)+len(mirrored)):
        source, used = 'two_kerb', clean
        widths = np.asarray([s['w_k'] for s in clean])
        widths_v2 = np.asarray([s['w'] for s in clean])
        any_kerb = side_ext[0] is not None or side_ext[1] is not None
    elif len(mirrored) >= design['min_clean_sections']:
        source, used = 'mirrored', mirrored
        widths = np.asarray([s['w_mirror_k'] for s in mirrored])
        widths_v2 = np.asarray([s['w_mirror'] for s in mirrored])
        any_kerb = any(side_ext[0 if s['ends'][1] == 'tree' else 1] is not None for s in mirrored)
    else:
        out['band'] = None
        out['fail'] = 'few_sections'
        out['kerb'] = kerb
        return out
    kerb['flag'] = ('kerb_not_found' if not any_kerb else
                    'kerb_not_found:lo' if side_ext[0] is None and source == 'two_kerb' else
                    'kerb_not_found:hi' if side_ext[1] is None and source == 'two_kerb' else None)
    out['kerb'] = kerb
    out['width_source'] = source
    out['width_samples'] = [r(w, 3) for w in widths]
    out['width_v2_median_m'] = r(np.median(widths_v2), 2)
    out['width_clean_median_m'] = r(np.median(widths), 2)
    out['width_median_m'] = r(np.median(widths), 2)
    out['width_p75_m'] = r(np.percentile(widths, 75), 2)
    out['width_clean_cv'] = r(cv_of(widths))
    width = float(np.percentile(widths, design['width_pct']))
    out['width_own_m'] = r(width, 2)
    if width_override is not None:
        width = float(width_override)
        out['width_shared'] = True
    out['width_raw_m'] = r(width, 2)
    rw = [s['w_reserve'] for s in secs if s.get('w_reserve')]
    cap = design['reserve_cap_ratio']*float(np.median(rw)) if (rw and res.get('clip') is not None) else None
    out['reserve_width_m'] = r(np.median(rw), 2) if rw else None
    out['width_cap_m'] = r(cap, 2)
    if cap is not None and width > cap:
        width = cap
        out['width_capped'] = True
    out['width_m'] = r(width, 2)
    segs = res['segments']
    offsets, sources = [], []
    r_cut = sum(1 for s in secs if s.get('r_frame'))/len(secs) if secs else 1.
    out['reserve_frame_cut_share'] = r(r_cut, 3)
    for k, seg in enumerate(segs):
        n = normal(seg['d'])
        if offset_pts is not None and len(offset_pts):
            offsets.append(float(np.median((np.asarray(offset_pts, float)-seg['p']) @ n)))
            sources.append('shared')
        elif source == 'mirrored':
            mids = [s['medial_off'] for s in used if s['seg'] == k]
            offsets.append(float(np.median(mids)) if mids else 0.)
            sources.append('reserve_medial')
        else:                                               # the v2 rule; the mask midpoints of the extended edges
            mids = [(s['lo_k']+s['hi_k'])/2 for s in used if s['seg'] == k]
            mask_off = float(np.median(mids)) if mids else 0.
            if design['offset_source'] == 'reserve' and seg['snapped'] and seg['reserve_pt'] is not None and r_cut < .5:
                offsets.append(float((seg['reserve_pt']-seg['p']) @ n))
                sources.append('reserve')
            else:
                offsets.append(mask_off)
                sources.append('mask')
    out['offset_sources'] = sources
    out['offsets_px'] = offsets
    # 1: the band's own centre line at each used station (pooled across windows into one offset)
    out['mids_px'] = [[r(v, 2) for v in s['p']+normal(segs[s['seg']]['d'])*offsets[s['seg']]] for s in used]
    pts = axis_polyline(segs, offsets, max_joint_px=15./mpp)
    return finish_band(out, LineString([tuple(p) for p in pts]), width, res, ctx, design)


# --- 3: canopy joins and continuation ----------------------------------------------------------------------------------------

def path_check(path, ctx, design=DESIGN_V3, exclude=(), width_px=None):
    """What lies in a continuation / canopy-join path: canopy share (tree / vegetation), road + car share, unlabelled
    pavement (no object; as metres along for the given width) and the blockers: buildings / pools / water and decided objects
    of any class but car / tree over blocker_eps_m2, other-class candidates (not road / car / tree / vegetation) over
    cont_other_m2."""
    mpp = ctx['mpp']
    if path is None or path.is_empty or path.area <= 0:
        return {'area_m2': 0., 'canopy_share': 0., 'roadlike_share': 0., 'unlabelled_m': 0., 'blockers': []}
    area = path.area
    canopy = ctx.get('canopy')
    can = path.intersection(canopy) if canopy is not None else None
    can_a = can.area if can is not None else 0.
    rest = path.difference(canopy) if canopy is not None else path
    rl = ctx.get('roadlike')
    rl_a = rest.intersection(rl).area if rl is not None else 0.
    lab = ctx.get('labelled')
    unl = rest.difference(lab) if lab is not None else rest
    blockers = []
    for o in ctx['live']:
        oid = o['id']
        if oid in exclude or oid not in ctx['polys'] or o['kind'] in design['under_kinds']:
            continue
        p = ctx['polys'][oid]
        if not p.intersects(path):
            continue
        ov = p.intersection(path).area*mpp*mpp
        why = None
        if o['kind'] in design['hard_blockers'] and ov > design['blocker_eps_m2']:
            why = 'building' if o['kind'] == 'building' else o['kind']
        elif is_decided(o, design) and ov > design['blocker_eps_m2']:
            why = 'decided'
        elif o['kind'] not in ('road', *design['canopy_kinds']) and ov > design['cont_other_m2']:
            why = 'other_class'
        if why:
            blockers.append({'id': oid, 'kind': o['kind'], 'status': o.get('status'), 'overlap_m2': r(ov, 2), 'why': why})
    wpx = width_px if width_px else math.sqrt(area)
    return {'area_m2': r(area*mpp*mpp, 2), 'canopy_share': r(can_a/area, 3), 'roadlike_share': r(rl_a/area, 3),
            'unlabelled_m': r(unl.area/wpx*mpp, 2), 'blockers': blockers}


def _lateral_join_ok(base, poly, c0, d, cb, db, mpp, design):
    """join_pieces' lateral rule (v2, unchanged): centre lines within join_offset_m, or the piece's centre inside the base's
    lateral extent with its edges within 1.5 x join_corridor_pad_m of it (a street half seen beside a canopy)."""
    n = normal(d)
    off = max(abs((cb-c0) @ n), abs((c0-cb) @ normal(db)))*mpp
    if off <= design['join_offset_m']:
        return True, off
    la = (np.asarray([c for g in iter_polygons(base) for c in g.exterior.coords], float)-c0) @ n
    lb_ = (np.asarray([c for g in iter_polygons(poly) for c in g.exterior.coords], float)-c0) @ n
    pad, mid = design['join_corridor_pad_m']/mpp, float((cb-c0) @ n)
    return bool(la.min() <= mid <= la.max() and lb_.min() >= la.min()-1.5*pad and lb_.max() <= la.max()+1.5*pad), off


def canopy_join(base, direction, candidates, ctx, design=DESIGN_V3, clip=None, exclude=()):
    """3a: candidates [(id, polygon)] of ANY reserve segment join the base across a canopy gap: >= join_min_len_m long,
    within join_deg, the lateral rule of join_pieces, a gap of 0 < gap <= cont_max_m along the axis, and the bridge (the base
    width) passes path_check (canopy >= cont_canopy_min of the gap, unlabelled <= cont_unlabelled_max_m along, no blocker) and
    lies >= join_reserve_frac inside the clip (the reserve continues). Greedy, repeated. Returns (union, ids, bridges, log)."""
    mpp = ctx['mpp']
    cur = base
    c0 = np.asarray(base.centroid.coords[0], float)
    d = unit(np.asarray(direction, float))
    n = normal(d)
    _, _, _, w0 = mrr_axis(base)
    joined, bridges, log = [], [], []
    changed = True
    while changed:
        changed = False
        for oid, poly in candidates:
            if oid in joined or poly is None or poly.is_empty:
                continue
            cb, db, lb, wb = mrr_axis(poly)
            if lb*mpp < design['join_min_len_m'] or angle_between(d, db) > design['join_deg']:
                continue
            ok, off = _lateral_join_ok(base, poly, c0, d, cb, db, mpp, design)
            if not ok:
                continue
            ca = np.asarray([c for p in iter_polygons(cur) for c in p.exterior.coords], float)
            cpb = np.asarray([c for g in iter_polygons(poly) for c in g.exterior.coords], float)
            pa, pb = (ca-c0) @ d, (cpb-c0) @ d
            if pb.min() > pa.max():
                s0, s1 = float(pa.max()), float(pb.min())
            elif pb.max() < pa.min():
                s0, s1 = float(pb.max()), float(pa.min())
            else:
                continue                                   # overlapping along the axis: not a canopy gap
            gap_m = (s1-s0)*mpp
            if gap_m <= 0 or gap_m > design['cont_max_m']:
                continue
            lc = ((cb-c0) @ n)/2
            hw = min(w0, wb)/2
            bridge = strip_rect(c0, d, s0-.5/mpp, s1+.5/mpp, lc-hw, lc+hw)
            g = polygonal(bridge.difference(cur.union(poly)))
            chk = path_check(g, ctx, design, exclude={*exclude, oid}, width_px=2*hw)
            why = None
            if chk['blockers']:
                why = 'blocker in the gap'
            elif chk['canopy_share'] < design['cont_canopy_min']:
                why = 'gap is not canopy'
            elif chk['unlabelled_m'] > design['cont_unlabelled_max_m']:
                why = 'long unlabelled gap'
            elif clip is not None and bridge.intersection(clip).area/bridge.area < design['join_reserve_frac']:
                why = 'bridge outside the reserve'
            entry = {'id': oid, 'joined': why is None, 'gap_m': r(gap_m, 2), 'angle_deg': r(angle_between(d, db), 2),
                     'offset_m': r(off, 2), 'canopy_share': chk['canopy_share'], 'unlabelled_m': chk['unlabelled_m']}
            if why:
                log.append({**entry, 'why': why, 'blockers': chk['blockers'][:3]})
                continue
            joined.append(oid)
            bridges.append(bridge)
            cur = polygonal(unary_union([cur, poly, bridge]))
            log.append(entry)
            changed = True
    return cur, joined, bridges, log


def _ray_to_frame(p, d, W, H):
    ts = []
    for k, lim in ((0, W), (1, H)):
        if d[k] > 1e-9:
            ts.append((lim-p[k])/d[k])
        elif d[k] < -1e-9:
            ts.append(-p[k]/d[k])
    return max(0., min(ts)) if ts else 0.


def continue_through_canopy(res, band_out, ctx, design=DESIGN_V3, lineage=(), others=()):
    """3b: each band end whose next cont_head_m (band width) are >= cont_head_canopy canopy continues along its axis to the
    first of: the next road piece on the axis (a live road outside the lineage covering >= cont_piece_m2 of the corridor), an
    accepted band (others), the frame edge, cont_max_m. The corridor to that point must pass path_check (canopy >=
    cont_canopy_min, unlabelled <= cont_unlabelled_max_m along, no blocker) and lie >= join_reserve_frac inside the clip.
    Returns band_out (rebuilt when an end moved) with 'continuation' (moved ends) and 'continuation_log'."""
    mpp, W, H = ctx['mpp'], ctx['W'], ctx['H']
    coords = np.asarray(band_out['axis_px'], float)
    hw = band_out['width_m']/2/mpp
    canopy = ctx.get('canopy')
    new = [coords[0].copy(), coords[-1].copy()]
    conts, log = [], []
    for end in (0, 1):
        a, b = (coords[1], coords[0]) if end == 0 else (coords[-2], coords[-1])
        d = unit(b-a)
        start = b
        L_frame = _ray_to_frame(start, d, W, H)
        L = min(L_frame, design['cont_max_m']/mpp)
        name = 'start' if end == 0 else 'end'
        if L*mpp < design['cont_min_m'] or canopy is None:
            continue
        head = strip_rect(start, d, 0., min(L, design['cont_head_m']/mpp), -hw, hw)
        head_share = head.intersection(canopy).area/head.area if head.area > 0 else 0.
        if head_share < design['cont_head_canopy']:
            log.append({'end': name, 'why': 'no canopy at the end', 'head_canopy_share': r(head_share, 3)})
            continue
        full = strip_rect(start, d, 0., L, -hw, hw)
        stop, to = L, ('frame' if L >= L_frame-1e-6 else 'cont_max')
        for o in sorted(ctx['by_kind'].get('road', []), key=lambda o: o['id']):
            if o['id'] in lineage or not ctx['polys'][o['id']].intersects(full):
                continue
            ov = polygonal(ctx['polys'][o['id']].intersection(full))
            if ov is None or area_m2(ov, mpp) < design['cont_piece_m2']:
                continue
            s_o = min(float((np.asarray(c)-start) @ d) for g in iter_polygons(ov) for c in g.exterior.coords)
            if s_o < stop:
                stop, to = max(0., s_o), o['id']
        for oid, ob in others:
            if ob is None or not ob.intersects(full):
                continue
            ov = polygonal(ob.intersection(full))
            if ov is None:
                continue
            s_o = min(float((np.asarray(c)-start) @ d) for g in iter_polygons(ov) for c in g.exterior.coords)
            if s_o < stop:
                stop, to = max(0., s_o), f'band:{oid}'
        if stop*mpp < design['cont_min_m']:
            log.append({'end': name, 'why': 'next piece adjoins', 'to': to})
            continue
        path = strip_rect(start, d, 0., stop, -hw, hw)
        chk = path_check(path, ctx, design, exclude=set(lineage), width_px=2*hw)
        inside = path.intersection(res['clip']).area/path.area if res.get('clip') is not None else 1.
        why = None
        if chk['blockers']:
            why = 'blocker in the path'
        elif chk['canopy_share'] < design['cont_canopy_min']:
            why = 'path is not canopy'
        elif chk['unlabelled_m'] > design['cont_unlabelled_max_m']:
            why = 'long unlabelled path'
        elif inside < design['join_reserve_frac']:
            why = 'reserve does not continue'
        entry = {'end': name, 'to': to, 'length_m': r(stop*mpp, 2), 'canopy_share': chk['canopy_share'],
                 'roadlike_share': chk['roadlike_share'], 'unlabelled_m': chk['unlabelled_m'], 'reserve_share': r(inside, 3)}
        if why:
            log.append({**entry, 'why': why, 'blockers': chk['blockers'][:3]})
            continue
        new[end] = start+d*stop
        conts.append({**entry, 'ring': [[r(x, 2), r(y, 2)] for x, y in path.exterior.coords[:-1]]})
    if not conts:
        band_out['continuation'] = []
        band_out['continuation_log'] = log
        return band_out
    c2 = coords.copy()
    c2[0], c2[-1] = new[0], new[1]
    out = {k: v for k, v in band_out.items() if k not in ('band', 'ring', 'fail', 'vertices', 'app_issues', 'ring_repairs', 'made_valid')}
    out = finish_band(out, LineString([tuple(p) for p in c2]), band_out['width_m'], res, ctx, design)
    if out.get('band') is None:
        band_out['continuation'] = []
        band_out['continuation_log'] = log+[{'why': f'rebuilt band failed: {out.get("fail")}'}]
        return band_out
    out['axis_mask_px'] = band_out['axis_px']
    out['continuation'] = conts
    out['continuation_log'] = log
    return out


# --- 5: cars under the band; parking bays in kerb indents -------------------------------------------------------------------

def indent_bays(res, band_out, ctx, design=DESIGN_V3):
    """5: a car partly under the band (cover < car_under_cover) widens the band's side by its protrusion (+ 0.05 m) when it is
    a parked car in a parking bay along the kerb: elongated, long axis within indent_car_deg of the axis, protruding
    0 < e <= indent_max_m on one side, its along-extent inside the band's, the bay strip (band edge to the car's outer edge,
    the car +- indent_pad_m along) >= indent_cover_min road or cars, >= 0.95 inside the clip, and <= indent_other_m2 of
    footpath / driveway / vegetation / building in it. Returns band_out (rebuilt when widened) with 'indent_bays'."""
    mpp, band = ctx['mpp'], band_out['band']
    segs = res['segments']
    d = unit(np.asarray(segs[0]['d'], float))
    n = normal(d)
    line = LineString(band_out['axis_px'])
    c0 = np.asarray(line.coords[0], float)
    along_band = [float((np.asarray(c)-c0) @ d) for c in line.coords]
    b0, b1 = min(along_band), max(along_band)
    base_lat = float((np.asarray(line.coords[0], float)-c0) @ n)
    hw = band_out['width_m']/2/mpp
    road_or_car = unary_union([g for g in (res.get('modal'), *[ctx['polys'][o['id']] for o in ctx['by_kind'].get('car', [])]) if g is not None])
    others = [ctx['polys'][o['id']] for k in design['indent_other_kinds'] for o in ctx['by_kind'].get(k, [])]
    other_u = unary_union(others) if others else None
    widen = {1: 0., -1: 0.}
    bays, seen = [], []
    for o in sorted(ctx['by_kind'].get('car', []), key=lambda o: o['id']):
        cp = ctx['polys'][o['id']]
        if not cp.intersects(band):
            continue
        cover = cp.intersection(band).area/cp.area
        if cover >= design['car_under_cover'] or cover <= design['car_straddle_min']:
            continue
        _, cd, cl, cw = mrr_axis(cp)
        rec = {'car': o['id'], 'cover': r(cover, 3)}
        if cl <= cw*1.15 or angle_between(d, cd) > design['indent_car_deg']:
            seen.append({**rec, 'why': 'not a parked car along the axis'})
            continue
        pts = np.asarray(cp.exterior.coords, float)
        lat = (pts-c0) @ n-base_lat
        alo = (pts-c0) @ d
        side = 1 if float(np.mean(lat)) > 0 else -1
        e = (float(np.max(side*lat))-hw)*mpp
        if e <= 0 or e > design['indent_max_m']:
            seen.append({**rec, 'why': f'protrudes {r(e, 2)} m', 'protrusion_m': r(e, 2)})
            continue
        if alo.min() < b0 or alo.max() > b1:
            seen.append({**rec, 'why': 'at a band end'})
            continue
        a0, a1 = max(b0, float(alo.min())-design['indent_pad_m']/mpp), min(b1, float(alo.max())+design['indent_pad_m']/mpp)
        l0, l1 = (base_lat+hw, base_lat+hw+e/mpp) if side > 0 else (base_lat-hw-e/mpp, base_lat-hw)
        strip = strip_rect(c0, d, a0, a1, l0, l1)
        road_cover = strip.intersection(road_or_car).area/strip.area if strip.area > 0 else 0.
        other_m2 = area_m2(strip.intersection(other_u), mpp) if other_u is not None else 0.
        inside = strip.intersection(res['clip']).area/strip.area if res.get('clip') is not None and strip.area > 0 else 1.
        rec.update(side='hi' if side > 0 else 'lo', protrusion_m=r(e, 2), bay_road_cover=r(road_cover, 3), bay_other_m2=r(other_m2, 2),
                   bay_reserve_share=r(inside, 3))
        if road_cover < design['indent_cover_min'] or other_m2 > design['indent_other_m2'] or inside < .95:
            seen.append({**rec, 'why': 'no parking bay along the kerb'})
            continue
        widen[side] = max(widen[side], e+.05)
        bays.append(rec)
    if not bays:
        band_out['indent_bays'] = []
        band_out['indent_seen'] = seen
        return band_out
    shift = (widen[1]-widen[-1])/2/mpp
    width = band_out['width_m']+widen[1]+widen[-1]
    moved = LineString([tuple(np.asarray(c, float)+n*shift) for c in line.coords])
    out = {k: v for k, v in band_out.items() if k not in ('band', 'ring', 'fail', 'vertices', 'app_issues', 'ring_repairs', 'made_valid')}
    out['offsets_px'] = [o+shift for o in band_out['offsets_px']]
    out['width_before_indent_m'] = band_out['width_m']
    out['width_m'] = r(width, 2)
    out = finish_band(out, moved, width, res, ctx, design)
    if out.get('band') is None:
        band_out['indent_bays'] = []
        band_out['indent_seen'] = seen+[{**b, 'why': f'rebuilt band failed: {out.get("fail")}'} for b in bays]
        return band_out
    out['indent_bays'] = bays
    out['indent_seen'] = seen
    out['indent_widen_m'] = {'lo': r(widen[-1], 2), 'hi': r(widen[1], 2)}
    return out


def band_metrics_v3(res, band_out, ctx, design=DESIGN_V3):
    """band_metrics_v2 with 5: a car counts as under the band only when >= car_under_cover of it is; cars with more than
    car_straddle_min under it but less are 'cars_straddling'. 3: candidate vegetation inside a canopy path of the band (a
    canopy-join bridge or a continuation) is canopy the band goes under, not a footpath blocker: only its overlap outside
    those paths counts against footpath_eps_m2."""
    m = band_metrics_v2(res, band_out, ctx, design)
    mpp, band = ctx['mpp'], band_out['band']
    conts = [Polygon(c['ring']) for c in band_out.get('continuation') or []]
    if conts:
        # 3: the continuation is band by design: the IoU gate compares the band with the amodal mask plus its paths
        m['iou_amodal_mask_only'] = m['iou_amodal']
        m['iou_amodal'] = r(iou(band, unary_union([res['amodal'], *conts])))
    zone = [*(res.get('_canopy_bridges') or []), *conts]
    if zone and m['blockers_footpath']:
        zu = unary_union(zone)
        keep = []
        for b in m['blockers_footpath']:
            if b['kind'] in design['canopy_kinds']:
                rest = area_m2(ctx['polys'][b['id']].intersection(band).difference(zu), mpp)
                if rest <= design['footpath_eps_m2']:
                    continue
                b = {**b, 'overlap_outside_canopy_paths_m2': r(rest, 2)}
            keep.append(b)
        m['blockers_footpath'] = keep
    ax = LineString(band_out['axis_px'])
    cars, straddle = [], []
    for o in ctx['by_kind'].get('car', []):
        cp = ctx['polys'][o['id']]
        if not cp.intersects(band):
            continue
        cover = cp.intersection(band).area/cp.area
        lateral = ax.distance(Point(np.asarray(cp.centroid.coords[0])))*mpp
        rec = {'id': o['id'], 'status': o.get('status'), 'cover': r(cover),
               'where': 'parked' if lateral > band_out['width_m']/2-3. else 'in_lane', 'lateral_m': r(lateral, 2)}
        if cover >= design['car_under_cover']:
            cars.append(rec)
        elif cover > design['car_straddle_min']:
            straddle.append(rec)
    m['cars_under'] = sorted(cars, key=lambda c: c['id'])
    m['cars_under_n'] = len(cars)
    m['cars_under_parked_n'] = sum(1 for c in cars if c['where'] == 'parked')
    m['cars_straddling'] = sorted(straddle, key=lambda c: c['id'])
    if design.get('cand_over_kerb_m') is not None:     # v3.1 (2 over kerb); DESIGN_V3 has no such key
        m = over_kerb_blockers(res, band_out, ctx, m, design)
    return m


def trim_footpaths_v3(res, band_out, metrics, ctx, design=DESIGN_V3):
    """trim_footpaths (v2) with the guard read as written: a candidate footpath / vegetation may lie <= footpath_eps_m2 under
    a band, so a blocker on one side pulls that edge back by the LEAST depth (bisection, 0.01 m) that leaves <= footpath_eps_m2
    of it under the band, not past its deepest point (v2 cleared the whole object: one bulge of a SAM footpath over the kerb
    narrowed the whole band). The edge_trim_max_m cap applies to that depth; end trims as v2."""
    from shapely.ops import substring
    mpp = ctx['mpp']
    w = band_out['width_m']/mpp
    segs = res['segments']
    line = LineString(band_out['axis_px'])
    band = band_out['band']
    proj = [line.project(Point(c)) for c in band.exterior.coords]
    s0, s1 = min(proj), max(proj)
    lim, pad = design['end_trim_max_m']/mpp, .2/mpp
    pen = {1: 0., -1: 0.}
    new0, new1, trims = s0, s1, []
    d = segs[0]['d']
    n = normal(d)
    off = band_out['offsets_px'][0]
    base = segs[0]['p']+n*off
    big = 4000.
    for b in metrics['blockers_footpath']:
        poly = (res.get('_cok_eff') or {}).get(b['id'], ctx['polys'][b['id']])     # v3.1: an over-kerb candidate's part beyond the kerb
        ov = poly.intersection(band)
        pts = np.asarray([c for q in iter_polygons(ov) for c in q.exterior.coords], float)
        if not len(pts):
            continue
        lat = (pts-base) @ n
        along = [line.project(Point(c)) for c in pts]
        b0, b1 = min(along), max(along)
        if lat.min() >= 0 or lat.max() <= 0:
            side = 1 if lat.min() >= 0 else -1
            full = w/2-float(np.min(np.abs(lat)))

            def left(depth, side=side, poly=poly):
                keep = strip_rect(base, d, -big, big, -w/2+(depth if side < 0 else 0.), w/2-(depth if side > 0 else 0.))
                return area_m2(poly.intersection(band).intersection(keep), mpp)
            lo_d, hi_d = 0., full
            while (hi_d-lo_d)*mpp > .01:
                mid = (lo_d+hi_d)/2
                lo_d, hi_d = (lo_d, mid) if left(mid) <= design['footpath_eps_m2'] else (mid, hi_d)
            depth = hi_d
            if depth*mpp <= design['edge_trim_max_m']:
                pen[side] = max(pen[side], depth)
                trims.append({'edge': 'hi' if side == 1 else 'lo', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r(depth*mpp, 2),
                              'full_clear_m': r(full*mpp+.05, 2)})
                continue
        if b1-s0 <= lim:
            new0 = max(new0, b1+pad)
            trims.append({'end': 'start', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r((b1+pad-s0)*mpp, 2)})
        elif s1-b0 <= lim:
            new1 = min(new1, b0-pad)
            trims.append({'end': 'end', 'blocker': b['id'], 'kind': b['kind'], 'trim_m': r((s1-b0+pad)*mpp, 2)})
        else:
            return None
    if new1-new0 <= 0 or (pen[1]+pen[-1]) >= w:
        return None
    shift = (pen[-1]-pen[1])/2
    width = (w-pen[1]-pen[-1])*mpp
    out = {k: v for k, v in band_out.items() if k not in ('band', 'ring', 'fail', 'vertices', 'app_issues', 'ring_repairs', 'made_valid')}
    out['offsets_px'] = [o+shift for o in band_out['offsets_px']]
    out['width_before_edge_trim_m'] = band_out['width_m']
    out['width_m'] = r(width, 2)
    shifted = LineString([tuple(np.asarray(c)+n*shift) for c in substring(line, new0, new1).coords])
    out = finish_band(out, shifted, width, res, ctx, design)
    if out.get('band') is None:
        return None
    out['footpath_trims'] = trims
    return out, band_metrics_v3(res, out, ctx, design), trims


def slice_check_v3(res, band_out, ctx, design=DESIGN_V3):
    """v2 slice_check with the narrow-slice ratio on the MASK width: the street width is measured on the masks (road and
    car pieces), so the band's own mask median (before the kerb extension, width_v2_median_m) is compared with it, not the
    outer-kerb width (which would let a 4 m lane of a 5.4 m street pass once both of its edges move out to a kerb line)."""
    out = slice_check(res, band_out, ctx, design)
    mask_w = band_out.get('width_v2_median_m')
    if mask_w is not None and out.get('street_width_m'):
        out['street_ratio_mask'] = r(mask_w/out['street_width_m'], 3)
        out['narrow_slice'] = bool(mask_w < design['slice_street_ratio']*out['street_width_m'])
    return out


# --- 4: narrow band against the reserve -------------------------------------------------------------------------------------

def narrow_check(band_out, design=DESIGN_V3):
    w, rw = band_out['width_m'], band_out.get('reserve_width_m')
    lim = design['narrow_reserve_ratio']*(rw-2*design['footpath_allow_m']) if rw else None
    narrow = bool(w < design['narrow_min_m'] or (lim is not None and w < lim))
    two = (band_out.get('kerb') or {}).get('two_kerb_n', 0)
    mirrored = band_out.get('width_source') == 'mirrored'
    return {'width_m': w, 'reserve_width_m': rw, 'reserve_limit_m': r(lim, 2), 'narrow': narrow, 'two_kerb_stations': two,
            'mirrored': mirrored, 'lane_slice': bool(narrow and not (two >= design['narrow_two_kerb_min'] or mirrored))}


def gate_v3(res, band_out, metrics, design=DESIGN_V3, slc=None, curv=None, nar=None):
    """gate_v2's reasons, then 'lane_slice' (4) LAST: a band v2 would stop keeps v2's first reason, so the 'lane_slice'
    count is the bands item 4 newly stops."""
    reasons, _ = gate_v2(res, band_out, metrics, design, slc, curv)
    if nar and nar['lane_slice']:
        reasons.append('lane_slice')
    return reasons, (reasons[0] if reasons else None)


# --- v3 layer ---------------------------------------------------------------------------------------------------------------

def layer_context_v3(objects, mpp, width, height, reserve, lots, design=DESIGN_V3, image=None):
    ctx = layer_context_v2(objects, mpp, width, height, reserve, lots, design)
    ctx['lum'] = luminance(image) if image is not None else None
    can = [ctx['polys'][o['id']] for k in design['canopy_kinds'] for o in ctx['by_kind'].get(k, [])]
    ctx['canopy'] = unary_union(can) if can else None
    lab = [ctx['polys'][o['id']] for o in ctx['live'] if o['id'] in ctx['polys']]
    ctx['labelled'] = unary_union(lab) if lab else None
    return ctx


def corridor_layer_v3(objects, *, width, height, mpp, reserve=None, lots=None, design=DESIGN_V3, affine=None, cache=None,
                      layer_ctx=None, image=None, xwin=None, retired=None):
    """v3 steps on one layer (pure). image = the frame (H x W x 3 or luminance) for the kerb profiles (2; without it every
    band is 'kerb_not_found'); xwin = {seed id: {'width_m', 'mid_pts_px', 'shared_with', ...}} from the cross-window pooling
    (1). Two passes as v2 (one width per reserve segment inside the frame); the cross-window width wins over the in-frame one.
    retired = {object id: decision} (--retire): decided twins the reviewer retired (see arbitrate); a band whose twin group
    holds one lists it in 'retires_on_accept' whatever the same-street test says. The report then carries 'reviewer_retired'."""
    ctx = layer_ctx if (layer_ctx is not None and 'canopy' in layer_ctx) else \
        layer_context_v3(objects, mpp, width, height, reserve, lots or {}, design, image)
    ctx['affine'] = affine
    ctx['retired'] = dict(retired or {})
    groups = arbitrate(ctx, design, ctx['retired'])
    groups_by_member = {m['id']: g for g in groups for m in g['members']}
    seeds = seed_order(ctx, groups, design)
    cache = {} if cache is None else cache
    first = _v3_pass(ctx, groups, groups_by_member, seeds, design, cache, shared=None, xwin=xwin)
    pooled = defaultdict(list)
    for p in first['proposals']:
        if p.get('reserve_segment') is not None and p.get('width_samples'):
            pooled[p['reserve_segment']].append(p)
    shared = {}
    for seg, recs in sorted(pooled.items()):
        if len(recs) >= 2:
            shared[seg] = float(np.median([w for p in recs for w in p['width_samples']]))
    linked = continuation_links(first['proposals'])
    eff = {**linked, **(xwin or {})}                     # a cross-window pooling wins over the in-window link
    rep = _v3_pass(ctx, groups, groups_by_member, seeds, design, cache, shared=shared, xwin=eff) if (shared or linked) else first
    rep['linked_groups'] = sorted({v['group'] for v in linked.values()})
    rep['shared_widths'] = {str(k): r(v, 2) for k, v in shared.items()}
    rep['twin_groups'] = groups
    rep['design'] = {k: (list(v) if isinstance(v, tuple) else v) for k, v in design.items()}
    rep['assumption'] = ASSUMPTION
    rep['image'] = ctx.get('lum') is not None
    if ctx['retired']:
        rep['reviewer_retired'] = retired_report(ctx, groups, rep)
    return rep, ctx, cache


def retired_report(ctx, groups, rep):
    """--retire: per reviewer-retired object, whether the layer holds it, its twin group, and which band lists it as
    retires_on_accept (or which exit record would have)."""
    out = []
    for oid, decision in sorted(ctx['retired'].items()):
        o = ctx['by_id'].get(oid)
        g = next((g['group'] for g in groups if any(m['id'] == oid for m in g['members'])), None)
        out.append({'id': oid, 'found': o is not None, 'kind': (o or {}).get('kind'), 'status': (o or {}).get('status'),
                    'twin_group': g, 'decision': decision,
                    'retired_by_proposal': [p['proposal']['id'] for p in rep['proposals'] if oid in p['proposal']['retires_on_accept']],
                    'listed_by_exit': [e['seed'] for e in rep['exits'] if any(t['id'] == oid for t in e.get('retires_on_accept') or [])]})
    return out


def continuation_links(proposals):
    """3 + 1 inside one window: a band whose canopy continuation reached the next road piece (or band) on its axis is the same
    street as the band built from that piece. Linked bands (union-find) pool their station widths and centre points: one
    width and one centre line, as across windows ('linked_with'). Returns {seed: override} for the second pass."""
    recs = [p for p in proposals if (p.get('_xwin') or {}).get('samples')]
    by_piece = {}
    for i, p in enumerate(recs):
        for oid in p['proposal']['derived_from']:
            by_piece.setdefault(oid, i)
        by_piece[f"band:{p['proposal']['id']}"] = i
    parent = list(range(len(recs)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for i, p in enumerate(recs):
        for c in p.get('continuation') or []:
            j = by_piece.get(c['to'])
            if j is not None and j != i:
                parent[root(i)] = root(j)
    comps = defaultdict(list)
    for i in range(len(recs)):
        comps[root(i)].append(i)
    out = {}
    for members in comps.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda i: recs[i]['seed'])
        samples = [w for i in members for w in recs[i]['_xwin']['samples']]
        pts = [q for i in members for q in recs[i]['_xwin']['mids_px'] or []]
        gid = 'in-' + hashlib.sha256(','.join(recs[i]['seed'] for i in members).encode()).hexdigest()[:9]
        for t in members:
            out[recs[t]['seed']] = {'width_m': float(np.median(samples)), 'mid_pts_px': pts, 'group': gid, 'pooled_stations': len(samples),
                                    'kind': 'in_window', 'shared_with': [{'scene': 'this window', 'seed': recs[i]['seed'],
                                                                          'proposal': recs[i]['proposal']['id']} for i in members if i != t]}
    return out


def _v3_pass(ctx, groups, groups_by_member, seeds, design, cache, shared, xwin=None):
    """_v2_pass with the v3 changes (3a canopy join, 2 kerb widths, 1 cross-window overrides, 3b continuation, 5 indent bays
    and cars >= 90 % under, 4 lane_slice)."""
    mpp = ctx['mpp']
    xwin = xwin or {}
    consumed, proposals, exits, accepted = {}, [], [], []
    for seed in seeds:
        if seed['id'] in consumed:
            continue
        seed_poly = ctx['polys'][seed['id']]
        seed_filled = fill_holes(seed_poly)
        _, sdir, _, _ = mrr_axis(seed_filled)
        base = {'seed': seed['id'], 'seed_kind': seed['kind'], 'seed_status': seed.get('status'),
                'seed_area_m2': r(area_m2(seed_poly, mpp), 2), 'group': (groups_by_member.get(seed['id']) or {}).get('group'),
                'vertices_before': len(seed.get('ring') or [])+sum(len(h) for h in seed.get('holes') or [])}
        evidence = car_park_evidence(seed_filled, sdir, ctx, design)
        if ctx['reserve'] is None:
            exit_ = 'car_park' if evidence['evidence'] else 'no_reserve'
            exits.append({**base, 'car_park': evidence, 'exit': exit_, 'exit_reasons': [exit_]})
            continue
        shares, dominant = lot_shares(seed_filled, ctx['reserve'], ctx['lots'])
        site = site_area_m2(seed_filled, ctx['lots'], 'single_lot_or_crown', mpp) if dominant == 'single_lot_or_crown' else 0.
        evidence['site'] = {'dominant': dominant, 'single_lot_site_m2': r(site, 1)}
        on_site = dominant == 'single_lot_or_crown' and site >= design['large_lot_m2']
        in_reserve = evidence['evidence'] and evidence['perpendicular'] > evidence['parallel']
        if evidence['evidence'] and (on_site or in_reserve):
            evidence['why'] = 'large single-lot site' if on_site else 'perpendicular cars outnumber parallel ones (in the reserve)'
            exits.append({**base, 'car_park': evidence, 'reserve_frac': r(shares.get('reserve', 0.)), 'dominant': dominant,
                          'exit': 'car_park', 'exit_reasons': ['car_park']})
            continue
        group = groups_by_member.get(seed['id'])
        pre = None
        if group is not None and group['resolved_class'] is None and group['how'].startswith('conflict'):
            pre = 'p0_conflict'
        elif group is not None and group['resolved_class'] not in (None, 'road') and group['how'].startswith('confirmed'):
            pre = 'p0_decided_other_class'
        elif group is not None and group['resolved_class'] == 'driveway':
            pre = 'p0_driveway'
        seg_id = segment_of(seed_filled, ctx, design)
        joined, bridges, join_log, cjoin_log, cjoined = [], [], [], [], []
        if pre is None:
            key_score = round(float(seed.get('score') or 0.), design['score_round'])
            cand_seg, cand_all = [], []
            for o in sorted(ctx['by_kind'].get('road', []), key=lambda o: o['id']):
                if o['id'] == seed['id'] or o['id'] in consumed:
                    continue
                same_mask = round(float(o.get('score') or 0.), design['score_round']) == key_score
                if is_decided(o, design) and not same_mask:
                    continue
                g = groups_by_member.get(o['id'])
                if g is not None and g['resolved_class'] not in (None, 'road'):
                    continue
                p = fill_holes(ctx['polys'][o['id']])
                if p.intersection(ctx['reserve']).area/p.area < design['join_reserve_frac']:
                    continue
                cand_all.append((o['id'], p))
                if seg_id is not None and segment_of(p, ctx, design) in (seg_id, None):
                    cand_seg.append((o['id'], p))
            clip = ctx['reserve'].buffer(design['reserve_clip_m']/mpp)
            cur = seed_filled
            if cand_seg:
                cur, joined, bridges, join_log = join_pieces(seed_filled, sdir, cand_seg, ctx, ctx['gap_union'], design, clip=clip)
            rest = [(i, p) for i, p in cand_all if i not in joined]
            if rest:
                excl = {seed['id'], *joined}
                _, cjoined, cb, cjoin_log = canopy_join(cur, sdir, rest, ctx, design, clip=clip, exclude=excl)
                joined, bridges = [*joined, *cjoined], [*bridges, *cb]
        key = ('v3', seed['id'], tuple(joined), design['adjacency_m'], design['segment_tol_m'], design['snap_deg'])
        if key not in cache:
            cache[key] = analyse_seed_v2(seed, ctx, groups_by_member, joined, bridges, design)
            cache[key]['_key'] = key
        res = cache[key]
        res['_canopy_bridges'] = bridges[len(bridges)-len(cjoined):] if cjoined else []    # canopy_join adds one bridge per piece
        kerb_extend(res, ctx, design)
        retires = []
        by_reviewer = ctx.get('retired') or {}
        for m in (group['members'] if group else []):
            if m['id'] == seed['id'] or not m['decided']:
                continue
            tw_iou = iou(ctx['polys'][m['id']], seed_poly)
            same_street = (group.get('reserve_frac') or 0.) >= design['retire_reserve_frac'] and tw_iou >= design['retire_iou']
            if same_street or m['id'] in by_reviewer:
                retires.append({'id': m['id'], 'kind': m['kind'], 'status': m['status'], 'iou_with_seed': r(tw_iou),
                                'reserve_frac': group.get('reserve_frac')})
                if m['id'] in by_reviewer:      # --retire only: the reviewer's decision, and whether the geometry agreed
                    retires[-1].update(retired_by='reviewer', same_street_test=bool(same_street), decision=by_reviewer[m['id']])
        res['retires'] = retires
        record = {**base, **_v2_seed_record(seed, ctx, res, mpp), 'reserve_segment': seg_id, 'join_log': join_log,
                  'canopy_join_log': cjoin_log, 'car_park': evidence}
        for k in ('candidate_over_kerb', 'candidate_over_kerb_below'):    # v3.1 only (kerb_extend_v31)
            if res.get(k):
                record[k] = res[k]
        xo = xwin.get(seed['id'])
        band_out = metrics = slc = curv = nar = None
        if res.get('exit'):
            reasons, first = [res['exit']], res['exit']
        elif pre is not None and not res.get('sections'):
            reasons, first = [pre], pre
        else:
            if res.get('sections'):
                width_override = xo['width_m'] if xo else (shared.get(seg_id) if shared else None)
                band_out = build_band_v3(res, ctx, design, width_override=width_override,
                                         offset_pts=xo['mid_pts_px'] if xo else None)
                if band_out.get('band') is not None:
                    lineage = {seed['id'], *res.get('siblings', []), *joined, *[m['id'] for m in res.get('twins', [])]}
                    band_out = continue_through_canopy(res, band_out, ctx, design, lineage=lineage,
                                                       others=[(q['proposal']['id'], qb) for q, qb in accepted])
                    band_out = indent_bays(res, band_out, ctx, design)
                    slc = slice_check_v3(res, band_out, ctx, design)
                    curv = curvature_check(res, band_out, ctx, design)
                    nar = narrow_check(band_out, design)
                    metrics = band_metrics_v3(res, band_out, ctx, design)
                    if metrics['blockers_hard'] or metrics['blockers_decided']:
                        trimmed = trim_for_blockers(res, band_out, metrics, ctx, design)
                        if trimmed is not None:
                            band_out, _, _ = trimmed
                            metrics = band_metrics_v3(res, band_out, ctx, design)
                    if metrics['blockers_footpath'] and not metrics['blockers_hard'] and not metrics['blockers_decided']:
                        trimmed = trim_footpaths_v3(res, band_out, metrics, ctx, design)
                        if trimmed is not None:
                            band_out, metrics, _ = trimmed
                            nar = narrow_check(band_out, design)
            if band_out is not None and band_out.get('fail') == 'few_sections' and res.get('sections'):
                inside = [x for x in res['sections'] if not x['outside']]
                fshares = [sum(1 for x in inside if x['ends'][q] == 'frame')/len(inside) for q in (0, 1)] if inside else [0., 0.]
                if max(fshares) >= design['slice_frame_end_share']:
                    slc = {'frame_slice': True, 'narrow_slice': False, 'frame_end_share': r(max(fshares), 3),
                           'why': 'the street runs into the frame along one side; too few clean sections for a width'}
            if res.get('axis') is None and not res.get('junction_exit') and (res.get('mrr_length_m') or 0) >= design['min_len_m']:
                reasons, first = ['no_axis'], 'no_axis'
            else:
                reasons, first = gate_v3(res, band_out, metrics, design, slc, curv, nar)
            if pre is not None:
                reasons, first = [pre, *reasons], pre
        xinfo = None
        if band_out:
            xinfo = {'samples': band_out.get('width_samples'), 'mids_px': band_out.get('mids_px'), 'axis_px': band_out.get('axis_px')}
            record.update({('band_ring' if k == 'ring' else k): v for k, v in band_out.items() if k not in ('band', 'mids_px')})
            modal_w = [s.get('w_modal') for s in res['sections'] if not s['outside'] and 'frame' not in s['ends'] and s.get('w_modal')]
            record['width_cv_before'] = r(cv_of(modal_w))
        if xo and xo.get('kind') == 'in_window':
            record['linked_with'] = xo['shared_with']
            record['linked_group'] = xo['group']
            record['linked_pooled_stations'] = xo['pooled_stations']
        elif xo:
            record['shared_with'] = xo['shared_with']
            record['xwin_group'] = xo['group']
            record['xwin_pooled_stations'] = xo['pooled_stations']
            if xo.get('xwin_trim'):
                record['xwin_trim'] = xo['xwin_trim']
            if design.get('xwin_own_trim'):                 # v3.1 (1 own trim); DESIGN_V3 has no such key
                record.update(xwin_mismatch(xo, band_out))
        if slc:
            record['slice'] = slc
        if curv:
            record['curvature'] = curv
        if nar:
            record['narrow_check'] = nar
        if metrics:
            record.update(metrics)
        if retires:
            record['retires_on_accept'] = retires
        if metrics and band_out and band_out.get('band') is not None:
            overlaps = [{'proposal': q['proposal']['id'], 'seed': q['seed'], 'share': r(band_out['band'].intersection(qb).area/band_out['band'].area)}
                        for q, qb in accepted if band_out['band'].intersects(qb)]
            overlaps = [o for o in overlaps if o['share'] >= design['overlap_share']]
            if overlaps:
                record['overlaps'] = overlaps
                reasons = [*reasons, 'overlaps_proposal']
                first = first or 'overlaps_proposal'
        record['exit_reasons'] = reasons
        if first is not None:
            record['exit'] = first
            exits.append(record)
            continue
        band = band_out['band']
        absorbs, replaces, on_accept = [], [], []
        (on_accept if is_decided(seed, design) else replaces).append(seed['id'])
        for sid in res['siblings']:
            o = ctx['by_id'][sid]
            absorbs.append({'id': sid, 'why': 'same-mask sibling', 'status': o.get('status'), 'area_m2': r(area_m2(ctx['polys'][sid], mpp), 3)})
            (on_accept if is_decided(o, design) else replaces).append(sid)
        for jid in joined:
            o = ctx['by_id'][jid]
            why = 'joined across a canopy' if any(e['id'] == jid and e['joined'] for e in cjoin_log) else 'joined collinear piece'
            absorbs.append({'id': jid, 'why': why, 'status': o.get('status'), 'area_m2': r(area_m2(ctx['polys'][jid], mpp), 3)})
            (on_accept if is_decided(o, design) else replaces).append(jid)
        for o in ctx['by_kind'].get('road', []):
            if o['id'] in replaces or o['id'] in on_accept or o['id'] in consumed:
                continue
            p = ctx['polys'][o['id']]
            cover = p.intersection(band).area/p.area if p.intersects(band) else 0.
            a = area_m2(p, mpp)
            same_mask = round(float(o.get('score') or 0.), design['score_round']) == round(float(seed.get('score') or 0.), design['score_round'])
            if cover >= design['absorb_cover'] or (a < design['speck_m2'] and p.distance(band) <= .5/mpp):
                if is_decided(o, design) and not same_mask:
                    continue
                why = 'speck' if a < design['speck_m2'] else 'road candidate inside the band'
                absorbs.append({'id': o['id'], 'why': why + (' (same mask)' if same_mask else ''), 'status': o.get('status'),
                                'area_m2': r(a, 3), 'cover': r(cover)})
                (on_accept if is_decided(o, design) else replaces).append(o['id'])
        twins = []
        for m in (group['members'] if group else []):
            if m['id'] == seed['id']:
                continue
            retiring = next((t for t in retires if t['id'] == m['id']), None)
            action = ('retires on accept: retired by the reviewer (--retire)' if retiring and retiring.get('retired_by') else
                      'retires on accept: a decided twin of the same street' if retiring else
                      'kept: a decided twin keeps its class and geometry' if m['decided'] else
                      f'absorbed: proposed {group["resolved_class"] or seed["kind"]} ({group["how"]})')
            twins.append({'id': m['id'], 'kind': m['kind'], 'status': m['status'], 'decided': m['decided'], 'action': action,
                          'iou_with_seed': r(iou(ctx['polys'][m['id']], seed_poly)),
                          'band_cover': r(ctx['polys'][m['id']].intersection(band).area/ctx['polys'][m['id']].area)})
            if not m['decided']:
                replaces.append(m['id'])
        remainder = []
        if (res.get('junction') or {}).get('arm_a') is not None:
            rest = polygonal(fill_holes(seed_poly).difference(band.buffer(.1/mpp)))
            for part in sorted(iter_polygons(rest) if rest is not None else [], key=lambda p: -p.area):
                if area_m2(part, mpp) >= design['speck_m2']:
                    remainder.append({'from': seed['id'], 'area_m2': r(area_m2(part, mpp), 2),
                                      'ring': [[r(x, 2), r(y, 2)] for x, y in part.exterior.coords[:-1]]})
        retire_ids = [t['id'] for t in retires]
        lineage = sorted(set(replaces) | set(on_accept) | set(retire_ids))
        pid = hashlib.sha256((design.get('proposal_version', design['version'])+':'+','.join(lineage)).encode()).hexdigest()[:16]
        model = _ring_details([tuple(p) for p in band_out['ring']], [], np.asarray(affine_of(ctx), float), 'road') if affine_of(ctx) is not None else {}
        seed_model = (seed.get('model') or {})
        if seed_model.get('color'):
            model['color'] = seed_model['color']
        proposal = {'id': pid, 'kind': 'road', 'label': 'Road', 'ring': band_out['ring'], 'status': 'proposal',
                    'source': design.get('proposal_version', design['version']), 'geometry_reviewed': False, 'score': seed.get('score'), 'model': model,
                    'replaces': sorted(set(replaces)), 'replaces_on_accept': sorted(set(on_accept)),
                    'retires_on_accept': sorted(retire_ids), 'remainder_on_accept': remainder,
                    'absorbs': absorbs, 'xclass_twin': twins, 'derived_from': lineage,
                    'cars_under': [c['id'] for c in record.get('cars_under', [])], 'assumption': 'parking lane counts as road'}
        record['proposal'] = proposal
        record['vertices_after'] = len(band_out['ring'])
        band_secs = []
        for s in res['sections']:
            if s['outside'] or 'frame' in s['ends']:
                continue
            piece = containing(LineString([tuple(s['p']-s['n']*60./mpp), tuple(s['p']+s['n']*60./mpp)]).intersection(band), Point(s['p']), tol=60./mpp)
            if piece is not None:
                band_secs.append(piece.length*mpp)
        record['width_cv_after'] = r(cv_of(band_secs))
        if xinfo is not None and seg_id is not None:
            lab = ctx['segment_labels']
            pts = ctx['medial'][lab == seg_id][:, :2] if len(lab) else np.zeros((0, 2))
            step = max(1, int(round(.5/design['skel_m'])))
            xinfo['seg_pts'] = [[r(x, 1), r(y, 1)] for x, y in sorted(map(tuple, pts.tolist()))[::step]]
        record['_xwin'] = xinfo
        for i in lineage:
            consumed[i] = pid
        proposals.append(record)
        accepted.append((record, band))
    counts = {'seeds': len(seeds), 'proposals': len(proposals), 'exits': dict(Counter(e['exit'] for e in exits)),
              'twin_groups': len(groups), 'twin_pairs': sum(len(g['pairs']) for g in groups),
              'twin_groups_by_class': dict(Counter(str(g['resolved_class']) for g in groups)),
              'specks_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'].startswith('speck')),
              'siblings_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'] == 'same-mask sibling'),
              'joined_absorbed': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'].startswith('joined')),
              'canopy_joins': sum(1 for p in proposals for a in p['proposal']['absorbs'] if a['why'] == 'joined across a canopy'),
              'continued_ends': sum(len(p.get('continuation') or []) for p in proposals),
              'indent_bays': sum(len(p.get('indent_bays') or []) for p in proposals),
              'kerb_not_found': sum(1 for p in proposals if (p.get('kerb') or {}).get('flag') == 'kerb_not_found'),
              'retires_on_accept': sum(len(p['proposal']['retires_on_accept']) for p in proposals),
              'cars_under': sum(p.get('cars_under_n', 0) for p in proposals),
              'cars_straddling': sum(len(p.get('cars_straddling') or []) for p in proposals)}
    return {'counts': counts, 'proposals': proposals, 'exits': exits}


# --- 1: one street, one width across windows ---------------------------------------------------------------------------------

def _apply_affine(pts, aff):
    pts = np.asarray(pts, float).reshape(-1, 2)
    return pts @ aff[:2]+aff[2]


def xwin_match(a, b, t_ab, t_ba, sa, sb, design=DESIGN_V3):
    """Same street in two overlapping windows: the band axes, mapped through the georeferenced frame transform, run within
    xwin_deg of each other, each axis line passes within xwin_lateral_m of the other's centre, and their extents along the
    street overlap by >= xwin_common_m. Collinear carriageway bands that close and that long in common are one reserve
    segment (each band lies >= join_reserve_frac in its frame's reserve by the gates). The reserve medial points are NOT
    the test: where the two frames cut the reserve at different places their medial lines differ (lot-25641 / win-d43d: the
    frames' east edges are 2.7 m apart); their match share is recorded ('medial_share') for information."""
    from scipy.spatial import cKDTree
    out = {'same': False}
    if not a.get('axis_px') or not b.get('axis_px'):
        return out
    ma, mb = scene_mpp(sa), scene_mpp(sb)
    axa = _apply_affine(a['axis_px'], t_ab)
    pb = np.asarray(b['axis_px'], float)
    da, db = unit(axa[-1]-axa[0]), unit(pb[-1]-pb[0])
    ang = angle_between(da, db)
    lat_ab = abs(float((axa.mean(axis=0)-pb[0]) @ normal(db)))*mb
    axb = _apply_affine(b['axis_px'], t_ba)
    pa = np.asarray(a['axis_px'], float)
    lat_ba = abs(float((axb.mean(axis=0)-pa[0]) @ normal(unit(pa[-1]-pa[0]))))*ma
    ia, ib = sorted(float(v) for v in axa @ db), sorted(float(v) for v in pb @ db)
    common = (min(ia[-1], ib[-1])-max(ia[0], ib[0]))*mb
    share = None
    if a.get('seg_pts') and b.get('seg_pts'):
        dist, _ = cKDTree(np.asarray(b['seg_pts'], float)).query(_apply_affine(a['seg_pts'], t_ab))
        share = float(np.mean(dist*mb <= design['xwin_match_m']))
    out.update(angle_deg=r(ang, 2), lateral_m=[r(lat_ab, 2), r(lat_ba, 2)], common_m=r(common, 1), medial_share=r(share, 3))
    out['same'] = bool(ang <= design['xwin_deg'] and max(lat_ab, lat_ba) <= design['xwin_lateral_m'] and common >= design['xwin_common_m'])
    return out


def xwin_pool(bands, scenes, design=DESIGN_V3):
    """1: group the first-pass proposals (bands: {scene, layer, seed, proposal, samples, mids_px, axis_px, seg_pts}) across
    overlapping windows of the same layer kind (union-find over xwin_match pairs) and pool each group: one width (the median
    of all station samples) and one offset (all members' station midpoints, mapped into each member's frame). Returns
    ({scene: {layer: {seed: override}}}, groups, pairs)."""
    from ..scene_recapture import frame_transform
    parent = list(range(len(bands)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    tf = {}

    def T(a, b):
        if a == b:
            return np.asarray([[1., 0.], [0., 1.], [0., 0.]])
        if (a, b) not in tf:
            tf[(a, b)] = np.asarray(frame_transform(scenes[a], scenes[b])['pixel_affine'], float)
        return tf[(a, b)]

    def overlaps(a, b):
        sa, sb = scenes[a], scenes[b]
        c = _apply_affine([[0, 0], [sa['image_width'], 0], [sa['image_width'], sa['image_height']], [0, sa['image_height']]], T(a, b))
        return Polygon(c).intersects(box(0, 0, sb['image_width'], sb['image_height']))
    pairs = []
    ov_cache = {}
    for i, j in itertools.combinations(range(len(bands)), 2):
        a, b = bands[i], bands[j]
        if a['layer'] != b['layer'] or a['scene'] == b['scene']:
            continue
        k = tuple(sorted((a['scene'], b['scene'])))
        if k not in ov_cache:
            ov_cache[k] = overlaps(*k)
        if not ov_cache[k]:
            continue
        m = xwin_match(a, b, T(a['scene'], b['scene']), T(b['scene'], a['scene']), scenes[a['scene']], scenes[b['scene']], design)
        if m.get('angle_deg') is not None:
            pairs.append({'a': f"{a['scene']}:{a['layer']}:{a['seed']}", 'b': f"{b['scene']}:{b['layer']}:{b['seed']}", **m})
        if m['same']:
            parent[root(i)] = root(j)
    comps = defaultdict(list)
    for i in range(len(bands)):
        comps[root(i)].append(i)
    overrides, groups = defaultdict(lambda: defaultdict(dict)), []
    for members in comps.values():
        if len({bands[i]['scene'] for i in members}) < 2:
            continue
        members = sorted(members, key=lambda i: (bands[i]['scene'], bands[i]['layer'], bands[i]['seed']))
        samples = [w for i in members for w in (bands[i]['samples'] or [])]
        width = float(np.median(samples))
        gid = hashlib.sha256(','.join(f"{bands[i]['scene']}:{bands[i]['seed']}" for i in members).encode()).hexdigest()[:12]
        groups.append({'group': gid, 'layer': bands[members[0]]['layer'], 'width_m': r(width, 2), 'pooled_stations': len(samples),
                       'members': [{'scene': bands[i]['scene'], 'seed': bands[i]['seed'], 'proposal_pass1': bands[i]['proposal'],
                                    'own_width_m': bands[i]['width_m'], 'stations': len(bands[i]['samples'] or [])} for i in members]})
        for t in members:
            bt = bands[t]
            pts = np.concatenate([_apply_affine(bands[i]['mids_px'], T(bands[i]['scene'], bt['scene'])) for i in members if bands[i]['mids_px']])
            overrides[bt['scene']][bt['layer']][bt['seed']] = {
                'width_m': width, 'mid_pts_px': pts.tolist(), 'group': gid, 'pooled_stations': len(samples),
                'shared_with': [{'scene': bands[i]['scene'], 'seed': bands[i]['seed'], 'proposal': bands[i]['proposal']}
                                for i in members if i != t]}
    return overrides, groups, pairs


def xwin_retrim(overrides, groups, records, scenes):
    """1 (third pass): a window's guard may still narrow its copy of a shared band (the footpath edge trim against that
    window's own candidates). To keep one street at one width, the group takes, per side, the deepest edge trim any member
    needed (sides compared through each band's normal; every frame here is north-up, so a pixel direction is the same
    direction in each), narrows the pooled width by both and shifts the pooled centre line by half their difference.
    records: {(scene, layer, seed): final record}. Returns (new overrides for the windows to re-run, per-group notes)."""
    out, notes = defaultdict(lambda: defaultdict(dict)), []
    for g in groups:
        recs = [(m, records.get((m['scene'], g['layer'], m['seed']))) for m in g['members']]
        recs = [(m, rec) for m, rec in recs if rec is not None and rec.get('axis_px') and len(rec['axis_px']) >= 2]
        if len(recs) < 2:
            continue
        signs = {np.sign(np.asarray(scenes[m['scene']]['pixel_to_meters'], float)[[0, 1], [0, 1]]).tolist() == [1., -1.] for m, _ in recs}
        if signs != {True}:
            notes.append({'group': g['group'], 'skipped': 'a frame is not north-up'})
            continue
        ref = None
        neg = pos = 0.
        for _m, rec in recs:
            ax = np.asarray(rec['axis_px'], float)
            n = normal(unit(ax[-1]-ax[0]))
            ref = n if ref is None else ref
            sgn = 1. if float(n @ ref) > 0 else -1.
            t = {'lo': 0., 'hi': 0.}
            for tr in rec.get('footpath_trims') or []:
                if tr.get('edge') in t:
                    t[tr['edge']] = max(t[tr['edge']], float(tr['trim_m']))
            neg, pos = max(neg, t['lo'] if sgn > 0 else t['hi']), max(pos, t['hi'] if sgn > 0 else t['lo'])
        if neg+pos <= 0:
            continue
        width = g['width_m']-neg-pos
        notes.append({'group': g['group'], 'layer': g['layer'], 'trim_neg_m': r(neg, 2), 'trim_pos_m': r(pos, 2), 'width_m': r(width, 2)})
        for m, _rec in recs:
            ov = overrides[m['scene']][g['layer']][m['seed']]
            mpp = scene_mpp(scenes[m['scene']])
            shift = ref*((neg-pos)/2/mpp)
            out[m['scene']][g['layer']][m['seed']] = {**ov, 'width_m': width, 'mid_pts_px': (np.asarray(ov['mid_pts_px'], float)+shift).tolist(),
                                                      'xwin_trim': {'neg_m': r(neg, 2), 'pos_m': r(pos, 2)}}
    return out, notes


# --- v3 run ---------------------------------------------------------------------------------------------------------------------

def load_heldout():
    return json.load(open(HELDOUT)) if HELDOUT.exists() else None


def _scene_meta(scene):
    return {k: v for k, v in scene.items() if k not in ('objects', 'groups', 'scans', 'property_roofs')}


def _v3_layer_run(scene_id, scene, review, reserve, lots, meta, image, xwin_by_layer, byeye_sets, design=DESIGN_V3, retired=None):
    """Both layers of one window: reports (with the by-eye rows of the review layer) and the cross-window band records.
    retired = {object id: decision} (--retire) is read on the REVIEW layer only (the reviewer's decided objects)."""
    mpp = scene_mpp(scene)
    reps, bands = {}, []
    for layer, objects in layers(scene, review):
        rep, _, _ = corridor_layer(objects, width=scene['image_width'], height=scene['image_height'], mpp=mpp, reserve=reserve,
                                   lots=lots, design=design, affine=scene['pixel_to_meters'], image=image,
                                   xwin=(xwin_by_layer or {}).get(layer), retired=retired if layer == 'review' else None)
        rep.update(scene=scene_id, layer=layer, mpp=r(mpp, 5), parcels=meta,
                   reserve_area_m2=r(area_m2(reserve, mpp), 1) if reserve is not None else None)
        for name, data in byeye_sets.items():
            rep[name] = byeye_errors(rep, scene_id, data) if (layer == 'review' and data) else []
        for p in rep['proposals']:
            x = p.pop('_xwin', None)
            if x and x.get('seg_pts') and x.get('samples'):
                bands.append({'scene': scene_id, 'layer': layer, 'seed': p['seed'], 'proposal': p['proposal']['id'],
                              'width_m': p['width_m'], **x})
        for e in rep['exits']:
            e.pop('_xwin', None)
        reps[layer] = rep
    return reps, bands


def run_v3(args):
    """run --design v3: pass 1 over every window (both layers), the cross-window pooling (1), pass 2 on the windows holding a
    pooled band, then metrics.json (totals, exits by reason, the v2 -> v3 table with the v2 eye marks, the by-eye width errors
    in-sample and HELD-OUT) and xwin.json (groups and every matched pair)."""
    design = DESIGN_V3
    out = Path(args.out) if getattr(args, 'out', None) else OUT/'run-v3'
    out.mkdir(parents=True, exist_ok=True)
    names = args.scenes or windows()
    byeye_sets = {'byeye': load_byeye(), 'byeye_new': load_byeye_new(), 'byeye_heldout': load_heldout()}
    beat(f'run-v3 start: {len(names)} windows (pass 1)')
    scenes, all_bands = {}, []

    def one(scene_id, xw=None):
        scene, review = load_scene(scene_id)
        reserve, lots, meta = scene_inputs(scene)
        image = np.asarray(Image.open(ROOT/'data/scenes'/scene_id/f"{scene['image_sha256']}.png").convert('RGB'))
        reps, bands = _v3_layer_run(scene_id, scene, review, reserve, lots, meta, image, xw, byeye_sets, design)
        for layer, rep in reps.items():
            json.dump(jsonable(rep), open(out/f'{scene_id}.{layer}.json', 'w'), indent=1)
        return scene, reps, bands
    for k, scene_id in enumerate(names):
        scene, _, bands = one(scene_id)
        scenes[scene_id] = _scene_meta(scene)
        all_bands += bands
        beat(f'run-v3 pass 1 {k+1}/{len(names)} {scene_id}: {len(bands)} band(s)')
    overrides, groups, pairs = xwin_pool(all_bands, scenes, design)
    beat(f'run-v3 cross-window: {len(groups)} group(s) over {sum(len(g["members"]) for g in groups)} band(s); pass 2 on {len(overrides)} window(s)')
    for k, scene_id in enumerate(sorted(overrides)):
        one(scene_id, overrides[scene_id])
        beat(f'run-v3 pass 2 {k+1}/{len(overrides)} {scene_id}')
    final = {}
    for scene_id in names:
        for layer in ('model', 'review'):
            f = out/f'{scene_id}.{layer}.json'
            if f.exists():
                final[(scene_id, layer)] = json.load(open(f))
    records = {(s, rep['layer'], x['seed']): x for (s, _), rep in final.items() for x in rep['proposals']+rep['exits']}
    retrim, notes = xwin_retrim(overrides, groups, records, scenes)
    beat(f'run-v3 cross-window retrim: {len(notes)} group(s); pass 3 on {len(retrim)} window(s)')
    for k, scene_id in enumerate(sorted(retrim)):
        merged = {layer: {**overrides[scene_id].get(layer, {}), **retrim[scene_id].get(layer, {})} for layer in ('model', 'review')}
        one(scene_id, merged)
        for layer in ('model', 'review'):
            f = out/f'{scene_id}.{layer}.json'
            if f.exists():
                final[(scene_id, layer)] = json.load(open(f))
        beat(f'run-v3 pass 3 {k+1}/{len(retrim)} {scene_id}')
    for g in groups:
        g['retrim'] = next((n for n in notes if n.get('group') == g['group'] and n.get('layer') == g['layer']), None)
    ids = {(s, rep['layer'], p['seed']): p['proposal']['id'] for (s, _), rep in final.items() for p in rep['proposals']}
    for (scene_id, layer), rep in final.items():
        touched = False
        for rec in rep['proposals']+rep['exits']:
            for sw in rec.get('shared_with') or []:
                sw['proposal'] = ids.get((sw['scene'], layer, sw['seed']))
                sw['exited'] = sw['proposal'] is None
                touched = True
        if touched:
            json.dump(rep, open(out/f'{scene_id}.{layer}.json', 'w'), indent=1)
    for g in groups:
        for m in g['members']:
            m['proposal_final'] = ids.get((m['scene'], g['layer'], m['seed']))
    json.dump(jsonable({'groups': groups, 'pairs': pairs, 'bands_pass1': [{k: v for k, v in b.items() if k not in ('mids_px', 'seg_pts')}
                                                                          for b in all_bands]}), open(out/'xwin.json', 'w'), indent=1)
    totals = defaultdict(Counter)
    lists = defaultdict(lambda: defaultdict(list))
    per_scene = {}
    for (scene_id, layer), rep in sorted(final.items()):
        c = rep['counts']
        t = totals[layer]
        t['windows'] += 1
        for key in ('seeds', 'proposals', 'twin_groups', 'twin_pairs', 'specks_absorbed', 'siblings_absorbed', 'cars_under',
                    'canopy_joins', 'continued_ends', 'indent_bays', 'kerb_not_found', 'cars_straddling', 'joined_absorbed'):
            t[key] += c.get(key, 0)
        t['windows_with_proposal'] += bool(c['proposals'])
        for k, v in c['exits'].items():
            t[f'exit:{k}'] += v
        L = lists[layer]
        for p in rep['proposals']:
            for key in ('vertices_before', 'vertices_after', 'width_cv_before', 'width_cv_after', 'width_m', 'iou_modal',
                        'iou_amodal', 'building_overlap_m2', 'outside_reserve_m2'):
                L[key].append(p.get(key))
            L['width_minus_v2_median_m'].append((p['width_m']-p['width_v2_median_m']) if p.get('width_v2_median_m') is not None else None)
            t['shared_xwin'] += bool(p.get('shared_with'))
            t['confirmed_seed_proposals'] += p['seed_status'] == 'confirmed'
            t['xclass_twins_kept'] += sum(1 for x in p['proposal']['xclass_twin'] if x['decided'])
        per_scene[f'{scene_id}.{layer}'] = {'proposals': c['proposals'], 'exits': c['exits']}

    def dist(values):
        v = [x for x in values if x is not None]
        return {'n': len(v), 'median': r(np.median(v)), 'p10': r(np.percentile(v, 10)), 'p90': r(np.percentile(v, 90)),
                'sum': r(sum(v), 2)} if v else {'n': 0}
    metrics = {'version': design['version'], 'written_at': datetime.now(UTC).isoformat(), 'assumption': ASSUMPTION,
               'design': {k: (list(v) if isinstance(v, tuple) else v) for k, v in design.items()},
               'totals': {layer: dict(t) for layer, t in totals.items()},
               'distributions': {layer: {k: dist(v) for k, v in L.items()} for layer, L in lists.items()},
               'per_scene': per_scene, 'xwin_groups': groups}
    metrics.update(v3_summary(out, names, totals, final))
    json.dump(jsonable(metrics), open(out/'metrics.json', 'w'), indent=1)
    beat('run-v3 wrote metrics.json')
    print(json.dumps(jsonable(metrics['totals']), indent=1)[:1500])


def compare_v2_v3(run_v3_dir, names):
    """For every v2 review-layer proposal: identical / changed / gone in v3 (as compare_v1_v2), with the v2 eye mark (HINT);
    v3 proposals matching no v2 proposal are 'new'."""
    rows, new = [], []
    for scene_id in names:
        f2, f3 = OUT/'run-v2'/f'{scene_id}.review.json', run_v3_dir/f'{scene_id}.review.json'
        if not (f2.exists() and f3.exists()):
            continue
        r2, r3 = json.load(open(f2)), json.load(open(f3))
        used = set()
        for p2 in r2['proposals']:
            b2 = _band_poly(p2)
            pid2 = p2['proposal']['id']
            mark = V2_EYE_MARKS.get(pid2[:6], (None, None, None))
            best = None
            for p3 in r3['proposals']:
                b3 = _band_poly(p3)
                v = iou(b2, b3) if (b2 is not None and b3 is not None) else 0.
                lineage = p2['seed'] in p3['proposal']['derived_from']
                if (lineage or v >= .3) and (best is None or v > best[0]):
                    best = (v, p3)
            row = {'scene': scene_id, 'v2_number': mark[0], 'v2_band': pid2, 'v2_seed': p2['seed'], 'v2_width_m': p2['width_m'],
                   'eye_mark': mark[1], 'eye_note': mark[2]}
            if best is None:
                ex = next((e for e in r3['exits'] if e['seed'] == p2['seed']), None)
                row.update(v3='gone', v3_exit=ex['exit'] if ex else 'not_seeded', v3_exit_reasons=ex.get('exit_reasons') if ex else None,
                           v3_width_m=ex.get('width_m') if ex else None)
            else:
                v, p3 = best
                used.add(p3['proposal']['id'])
                same = v >= .99 and abs((p3['width_m'] or 0)-(p2['width_m'] or 0)) <= .01
                row.update(v3='identical' if same else 'changed', v3_band=p3['proposal']['id'], v3_seed=p3['seed'], v3_width_m=p3['width_m'],
                           band_iou_v2_v3=r(v), v3_width_source=p3.get('width_source'), v3_kerb=(p3.get('kerb') or {}).get('flag') or 'kerb',
                           v3_offset=p3.get('offset_sources'), v3_joined=p3.get('joined'), v3_continued=[c['end']+'->'+str(c['to']) for c in p3.get('continuation') or []],
                           v3_shared_with=[f"{s['scene']}:{s.get('proposal')}" for s in p3.get('shared_with') or []],
                           v3_indent_bays=[b['car'] for b in p3.get('indent_bays') or []])
            rows.append(row)
        for p3 in r3['proposals']:
            if p3['proposal']['id'] not in used:
                new.append({'scene': scene_id, 'v3_band': p3['proposal']['id'], 'v3_seed': p3['seed'], 'v3_width_m': p3['width_m'],
                            'joined': p3.get('joined'), 'continued': len(p3.get('continuation') or []),
                            'shared_with': [s['scene'] for s in p3.get('shared_with') or []]})
    by_mark = defaultdict(Counter)
    for row in rows:
        by_mark[str(row['eye_mark'])][row['v3']] += 1
    return {'rows': rows, 'new_v3_proposals': new, 'counts_by_v2_eye_mark': {k: dict(v) for k, v in sorted(by_mark.items())},
            'counts': dict(Counter(row['v3'] for row in rows)), 'new_count': len(new),
            'gone_exits': dict(Counter(row['v3_exit'] for row in rows if row['v3'] == 'gone'))}


def _byeye_agg(rows, note):
    have = [b for b in rows if b.get('proposal')]
    per_road = {}
    for b in have:
        per_road.setdefault(b.get('road') or b['object'], []).append(b)
    return {'note': note, 'objects': len(rows), 'with_band': len(have),
            'width_abs_error_m': r(np.mean([b['width_abs_error_m'] for b in have])) if have else None,
            'width_signed_error_m': r(np.mean([b['width_error_m'] for b in have])) if have else None,
            'kerb_edge_abs_error_m': r(np.mean([b['kerb_edge_abs_error_m'] for b in have if b.get('kerb_edge_abs_error_m') is not None]))
            if any(b.get('kerb_edge_abs_error_m') is not None for b in have) else None, 'rows': rows}


def _byeye_rows(run_dir, names, key, data):
    rows = []
    for scene_id in names:
        f = run_dir/f'{scene_id}.review.json'
        if not f.exists():
            continue
        rep = json.load(open(f))
        got = rep.get(key) if key in rep else byeye_errors(rep, scene_id, data)
        rows += [{'scene': scene_id, **b} for b in got or []]
    return rows


def v3_summary(out, names, totals, final):
    """metrics.json additions for v3: exits by reason, the v2 -> v3 table, the by-eye width errors on the IN-SAMPLE sets
    (kerb-widths-by-eye.json, 5 roads; v2/byeye/new-readings.json, 2 roads: both informed v2 / v3) and on the HELD-OUT set
    (v3/heldout-widths.json, 4 roads read before any v3 code; nothing tuned on it), with v2's errors on the same sets."""
    exits = {layer: {k.split(':', 1)[1]: v for k, v in sorted(t.items()) if k.startswith('exit:')} for layer, t in totals.items()}
    held = load_heldout()
    sets = {'byeye_in_sample': ('byeye', load_byeye(), 'IN-SAMPLE: the 5 roads of kerb-widths-by-eye.json (informed v1 / v2 and the v3 kerb thresholds)'),
            'byeye_new_in_sample': ('byeye_new', load_byeye_new(), 'IN-SAMPLE for v3: the 2 roads of v2/byeye/new-readings.json (seen with v2; used to set the v3 kerb thresholds)'),
            'byeye_heldout': ('byeye_heldout', held, 'HELD-OUT: 4 roads read before any v3 code (v3/heldout-widths.json), outer kerb to outer kerb; nothing tuned on them')}
    res = {'exits_by_reason': exits, 'v2_vs_v3': compare_v2_v3(out, names)}
    for name, (key, data, note) in sets.items():
        rows3 = _byeye_rows(out, names, key, data)
        rows2 = _byeye_rows(OUT/'run-v2', names, '__recompute__', data)
        road = {(s['window'], s['object']): s.get('road') for s in (data or {}).get('stations', [])}
        for rr in (rows3, rows2):
            for b in rr:
                b['road'] = road.get((b['scene'], b['object']))
        res[name] = {'v3': _byeye_agg(rows3, note), 'v2': _byeye_agg(rows2, 'v2 bands (run-v2) on the same set')}
    return res


# --- v3 renders and marks -------------------------------------------------------------------------------------------------------

def _dashed(draw, ring, x0, y0, colour, width=1, dash=6., gap=4., closed=True):
    pts = [(x-x0, y-y0) for x, y in ring]+([(ring[0][0]-x0, ring[0][1]-y0)] if closed and ring else [])
    for k in range(len(pts)-1):
        a, b = np.asarray(pts[k], float), np.asarray(pts[k+1], float)
        seg = float(np.linalg.norm(b-a))
        t = 0.
        while t < seg:
            e = min(t+dash, seg)
            draw.line([tuple(a+(b-a)*t/max(seg, 1e-6)), tuple(a+(b-a)*e/max(seg, 1e-6))], fill=colour, width=width)
            t = e+gap


def render_proposal_v3(scene_id, record, objects, scene, reserve, medial, path, ctx_res=None, ghost=None, title=None):
    """render_proposal_v2's two panels (raw LEFT: imagery + band outline cyan 1 px; annotated RIGHT) plus: the canopy
    continuation drawn DASHED (white on the raw panel, blue on the annotated one) over the solid mask-supported band, the
    extended kerb lines of the kept sections (cyan ticks at the extended ends), indent-bay cars (magenta), straddling cars
    (red), and a third header line naming the windows a shared width comes from."""
    d = ROOT/'data/scenes'/scene_id
    image = Image.open(d/f"{scene['image_sha256']}.png").convert('RGB')
    W, H = image.size
    by_id = {o['id']: o for o in objects}
    p = record.get('proposal') or {'id': 'exit-'+str(record.get('exit')), 'ring': record.get('band_ring') or [],
                                   'cars_under': [c['id'] for c in record.get('cars_under') or []], 'replaces': [], 'replaces_on_accept': [],
                                   'retires_on_accept': [t['id'] for t in record.get('retires_on_accept') or []], 'remainder_on_accept': []}
    ring = p['ring']
    seed = by_id[record['seed']]
    conts = record.get('continuation') or []
    pts = list(ring)+list(seed['ring'])+list(ghost or [])+[q for c in conts for q in c['ring']]
    for jid in record.get('joined') or []:
        pts += by_id[jid]['ring']
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    m = 30
    x0, y0 = max(0, int(min(xs))-m), max(0, int(min(ys))-m)
    x1, y1 = min(W, int(math.ceil(max(xs)))+m), min(H, int(math.ceil(max(ys)))+m)
    crop = image.crop((x0, y0, x1, y1))
    band = Polygon(ring) if len(ring) >= 3 else None
    cont_polys = [Polygon(c['ring']) for c in conts]
    solid = band
    if band is not None and cont_polys:
        solid = polygonal(band.difference(unary_union([cp.buffer(.5) for cp in cont_polys])))
    raw = crop.copy()
    dr = ImageDraw.Draw(raw)
    if ring:
        for part in (iter_polygons(solid) if solid is not None else []):
            _outline(dr, [list(c) for c in part.exterior.coords[:-1]], x0, y0, (0, 255, 255))
        for cp in cont_polys:
            q = polygonal(cp.intersection(band)) if band is not None else None
            for part in (iter_polygons(q) if q is not None else []):
                _dashed(dr, [list(c) for c in part.exterior.coords[:-1]], x0, y0, (255, 255, 255))
    ann = crop.copy()
    da = ImageDraw.Draw(ann)
    if reserve is not None:
        _geom_outline(da, reserve, x0, y0, (190, 120, 40))
    for x, y, _ in medial:
        if x0 <= x < x1 and y0 <= y < y1:
            da.point((x-x0, y-y0), fill=(255, 150, 0))
    under = set(p['cars_under'])
    bay = {b['car'] for b in record.get('indent_bays') or []}
    straddle = {c['id'] for c in record.get('cars_straddling') or []}
    for o in objects:
        if o.get('status') in DEAD:
            continue
        if o['kind'] == 'car':
            colour = (255, 0, 255) if o['id'] in bay else (0, 255, 0) if o['id'] in under else (255, 40, 40) if o['id'] in straddle else (0, 110, 0)
            _outline(da, o['ring'], x0, y0, colour, 2 if (o['id'] in under or o['id'] in bay or o['id'] in straddle) else 1)
        elif o['kind'] in ('tree', 'vegetation'):
            _outline(da, o['ring'], x0, y0, (150, 160, 40))
    for oid in [record['seed'], *record.get('siblings', [])]:
        _outline(da, by_id[oid]['ring'], x0, y0, (255, 255, 0))
    for jid in record.get('joined') or []:
        _outline(da, by_id[jid]['ring'], x0, y0, (255, 255, 160))
    for rem in p.get('remainder_on_accept') or []:
        _outline(da, rem['ring'], x0, y0, (255, 120, 200), 2)
    if ctx_res is not None:
        for j, s in enumerate(ctx_res.get('sections') or []):
            if s['outside'] or j % 2:
                continue
            a, b = s['p']+s['n']*s['lo'], s['p']+s['n']*s['hi']
            colour = (0, 220, 255) if s['clean'] else ((255, 0, 255) if 'tree' in s['ends'] else (255, 255, 255) if 'frame' in s['ends'] else (255, 140, 0))
            da.line([(a[0]-x0, a[1]-y0), (b[0]-x0, b[1]-y0)], fill=colour, width=1)
            if s['clean'] and s.get('lo_k') is not None:
                for e0, e1 in ((s['lo_k'], s['lo']), (s['hi'], s['hi_k'])):
                    if abs(e1-e0) > .5:
                        a2, b2 = s['p']+s['n']*e0, s['p']+s['n']*e1
                        da.line([(a2[0]-x0, a2[1]-y0), (b2[0]-x0, b2[1]-y0)], fill=(255, 255, 255), width=1)
    if ghost:
        _dashed(da, ghost, x0, y0, (255, 0, 255), dash=4., gap=4.)
    if ring:
        for part in (iter_polygons(solid) if solid is not None else []):
            _outline(da, [list(c) for c in part.exterior.coords[:-1]], x0, y0, (60, 120, 255), 2)
        for cp in cont_polys:
            q = polygonal(cp.intersection(band)) if band is not None else None
            for part in (iter_polygons(q) if q is not None else []):
                _dashed(da, [list(c) for c in part.exterior.coords[:-1]], x0, y0, (60, 120, 255), width=2)
    for tid in p.get('retires_on_accept') or []:
        if tid in by_id:
            _outline(da, by_id[tid]['ring'], x0, y0, (255, 140, 0), 3)
    axis = record.get('axis_px') or []
    if len(axis) >= 2 and ring:
        da.line([(x-x0, y-y0) for x, y in axis], fill=(255, 0, 0), width=2)
    w, h = crop.size
    header = 50
    sheet = Image.new('RGB', (2*w+4, h+header), (20, 20, 20))
    sheet.paste(raw, (0, header))
    sheet.paste(ann, (w+4, header))
    ds = ImageDraw.Draw(sheet)
    ds.text((4, 2), title or f'{scene_id} {p["id"]} seed {record["seed"]} [{record["seed_status"]}] crop x{x0}-{x1} y{y0}-{y1}', fill=(255, 255, 255))
    kerb = record.get('kerb') or {}
    line2 = (f'width {record.get("width_m")} m ({record.get("width_source")}; own {record.get("width_own_m")}, v2-median '
             f'{record.get("width_v2_median_m")}; {kerb.get("flag") or "kerb found"} ext lo {kerb.get("ext_lo_m")} hi {kerb.get("ext_hi_m")})  '
             f'cars under {record.get("cars_under_n")}  straddling {len(record.get("cars_straddling") or [])}  '
             f'continued {len(conts)}  joined {len(record.get("joined") or [])}  bays {len(record.get("indent_bays") or [])}')
    if record.get('exit'):
        line2 = f'EXIT {record.get("exit_reasons")}  ' + line2
    ds.text((4, 17), line2, fill=(255, 255, 255))
    sw = record.get('shared_with') or []
    lw = record.get('linked_with') or []
    parts = []
    if sw:
        parts.append('SHARED width/centre with ' + ', '.join(f'{s["scene"]} band {s.get("proposal")} (seed {s["seed"]})' for s in sw))
    if lw:
        parts.append('LINKED through the canopy with band(s) ' + ', '.join(f'{s.get("proposal")} (seed {s["seed"]})' for s in lw))
    if record.get('xwin_trim'):
        parts.append(f'group trim {record["xwin_trim"]}')
    line3 = '; '.join(parts) if parts else 'width not shared across windows'
    ds.text((4, 32), line3, fill=(255, 220, 120) if parts else (170, 170, 170))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return sheet, (w, h)


def render_v3(args):
    """Renders of every v3 review-layer proposal (renders-v3/<scene>/<band>.png, header 50 px), exit sheets of every v2 band
    gone in v3 (renders-v3/gone/, the v2 band dashed magenta), contact sheets, index.json and MARKS-roads-v3.md."""
    run_dir = Path(args.out) if getattr(args, 'out', None) else OUT/'run-v3'
    rdir = OUT/'renders-v3'
    rdir.mkdir(parents=True, exist_ok=True)
    metrics = json.load(open(run_dir/'metrics.json'))
    table = metrics['v2_vs_v3']
    hints = {row['v3_band']: row for row in table['rows'] if row.get('v3_band')}
    gone = defaultdict(list)
    for row in table['rows']:
        if row['v3'] == 'gone':
            gone[row['scene']].append(row)
    files = sorted(run_dir.glob('*.review.json'))
    entries, gone_entries = [], []
    beat(f'render-v3 start: {len(files)} review files')
    for f in files:
        rep = json.load(open(f))
        scene_id = rep['scene']
        if not rep['proposals'] and not gone.get(scene_id):
            continue
        scene, review = load_scene(scene_id)
        reserve, lots, _ = scene_inputs(scene)
        mpp = scene_mpp(scene)
        objects = review['objects']
        image = np.asarray(Image.open(ROOT/'data/scenes'/scene_id/f"{scene['image_sha256']}.png").convert('RGB'))
        _, ctx, cache = corridor_layer(objects, width=scene['image_width'], height=scene['image_height'], mpp=mpp, reserve=reserve,
                                       lots=lots, design=DESIGN_V3, affine=scene['pixel_to_meters'], image=image)
        for rec in rep['proposals']:
            res = cache.get(('v3', rec['seed'], tuple(rec.get('joined') or []), DESIGN_V3['adjacency_m'], DESIGN_V3['segment_tol_m'], DESIGN_V3['snap_deg']))
            if res is not None:
                build_band_v3(res, ctx, DESIGN_V3)        # sets lo_k / hi_k on the sections for the kerb ticks
            path = rdir/scene_id/f'{rec["proposal"]["id"]}.png'
            _, size = render_proposal_v3(scene_id, rec, objects, scene, reserve, ctx['medial'], path, res)
            entries.append({'crop_size': list(size), 'scene': scene_id, 'proposal': rec['proposal']['id'], 'seed': rec['seed'],
                            'seed_status': rec['seed_status'], 'file': str(path.relative_to(OUT)), 'width_m': rec['width_m'],
                            'width_source': rec.get('width_source'), 'width_own_m': rec.get('width_own_m'),
                            'width_v2_median_m': rec.get('width_v2_median_m'), 'kerb': (rec.get('kerb') or {}).get('flag'),
                            'vertices': f'{rec["vertices_before"]}->{rec["vertices_after"]}', 'cars_under': rec['cars_under_n'],
                            'cars_straddling': len(rec.get('cars_straddling') or []),
                            'replaces': rec['proposal']['replaces'], 'replaces_on_accept': rec['proposal']['replaces_on_accept'],
                            'retires_on_accept': rec['proposal']['retires_on_accept'], 'joined': rec.get('joined') or [],
                            'canopy_joined': [a['id'] for a in rec['proposal']['absorbs'] if a['why'] == 'joined across a canopy'],
                            'continuation': [{'end': c['end'], 'to': c['to'], 'length_m': c['length_m']} for c in rec.get('continuation') or []],
                            'indent_bays': [b['car'] for b in rec.get('indent_bays') or []],
                            'shared_with': rec.get('shared_with') or [], 'linked_with': rec.get('linked_with') or [],
                            'xwin_trim': rec.get('xwin_trim'), 'width_shared': bool(rec.get('width_shared')),
                            'remainder': len(rec['proposal']['remainder_on_accept']), 'junction_arm': bool((rec.get('junction') or {}).get('arm_a')),
                            'hint': hints.get(rec['proposal']['id'])})
        for row in gone.get(scene_id, []):
            ex = next((e for e in rep['exits'] if e['seed'] == row['v2_seed']), None)
            if ex is None:
                continue
            v2rep = json.load(open(OUT/'run-v2'/f'{scene_id}.review.json'))
            ghost = next(p['proposal']['ring'] for p in v2rep['proposals'] if p['proposal']['id'] == row['v2_band'])
            path = rdir/'gone'/f'{scene_id}-{row["v2_band"]}.png'
            title = f'{scene_id} v2 #{row["v2_number"]} band {row["v2_band"]} (eye: {row["eye_mark"]}) GONE in v3: {row["v3_exit"]}'
            _, size = render_proposal_v3(scene_id, ex, objects, scene, reserve, ctx['medial'], path, None, ghost=ghost, title=title)
            gone_entries.append({**row, 'file': str(path.relative_to(OUT))})
        beat(f'render-v3 {scene_id}: {len(rep["proposals"])}')
    order = marks_order_v2(entries)
    contact_sheets_v3(order, rdir)
    write_marks_v3(order, gone_entries, metrics)
    json.dump({'proposals': order, 'gone': gone_entries}, open(rdir/'index.json', 'w'), indent=1, default=str)
    beat(f'render-v3 done: {len(order)} proposals, {len(gone_entries)} gone sheets')
    print(len(order), 'renders', len(gone_entries), 'gone sheets')


def contact_sheets_v3(order, rdir, per_sheet=12, thumb=300):
    for k in range(0, len(order), per_sheet):
        chunk = order[k:k+per_sheet]
        cols = 4
        rows = math.ceil(len(chunk)/cols)
        sheet = Image.new('RGB', (cols*(thumb+6), rows*(thumb+22)), (15, 15, 15))
        ds = ImageDraw.Draw(sheet)
        for i, e in enumerate(chunk):
            im = Image.open(OUT/e['file'])
            cw, ch = e['crop_size']
            raw = im.crop((0, 50, cw, 50+ch))
            raw.thumbnail((thumb, thumb))
            cx, cy = (i % cols)*(thumb+6), (i//cols)*(thumb+22)
            sheet.paste(raw, (cx, cy+18))
            ds.text((cx+2, cy+2), f'{k+i+1}. {e["scene"][:16]} w{e["width_m"]}', fill=(255, 255, 255))
        sheet.save(rdir/f'contact-{k//per_sheet+1:02d}.png')


def write_marks_v3(order, gone_entries, metrics):
    t = metrics['v2_vs_v3']
    held = metrics.get('byeye_heldout') or {}
    ins = metrics.get('byeye_in_sample') or {}
    ins2 = metrics.get('byeye_new_in_sample') or {}
    lines = ['# MARKS: road corridor v3 (sidecar, 2026-10-07)', '',
             'Reviewer ask (7 Oct ~11:25): "roads need to converge on being straight lines of a fixed width etc. layer it underneath '
             'other objects like cars/trees etc." Each numbered line is one proposed straight, fixed-width road band on the review '
             'layer. Nothing is applied: a band replaces its road objects only if you accept it (a confirmed road is replaced only '
             'on accept). SAM3 cannot give this shape (probe-v1: masks stay modal; lower thresholds bleed 7-24x onto the footpath), '
             'so these are geometry proposals.', '',
             f'Assumption to confirm: {ASSUMPTION}', '',
             'What changed since v2 (MARKS-roads-v2.md): the width now runs to the OUTER kerb (the back of the kerb and channel, read '
             'in the image within 0.8 m beyond the mask edge; where it is not seen the v2 width is kept and the line says '
             '"kerb_not_found"); one street seen in two overlapping windows gets one width and one centre line ("shares width with '
             '..."); a band goes on through a tree canopy to the next road piece or the frame edge when the road reserve goes on and '
             'nothing else is in the way (that part is drawn DASHED); a band under 5 m, or under half the reserve minus footpaths, '
             'needs kerbs seen on both sides or it exits as a lane slice; a car counts as under a band only when 90 % of it is, '
             'and a car parked in a kerb indent may widen its side by up to 1 m.', '',
             'How to answer in chat: one word per line number, e.g. `1 right, 2 wrong (too wide), 3 unclear`. right = the band is '
             'the carriageway (outer kerb to outer kerb, parking lanes included) and may replace the listed objects (and retire the '
             'orange ones); wrong = misplaced, too wide or narrow, or not a street; unclear = cannot tell from the render. For the '
             '"gone since v2" list, answer `G<n> agree` or `G<n> should have a band`.', '',
             'HINT = the agent by-eye skeptic mark on the matching v2 band (eye-tally-roads-v2.md), not your mark.', '',
             'Render: LEFT panel = imagery with the band outline (cyan, 1 px, no fill; the canopy continuation dashed white); RIGHT '
             'panel = modal polygon (yellow), joined pieces (light yellow), band (blue; continuation dashed), axis (red), kept '
             'sections (cyan) with the kerb extension (white ticks), dropped sections (magenta tree / white frame / orange other), '
             'cars under the band (bright green), straddling cars (red), indent-bay cars (magenta), retires-on-accept objects '
             '(ORANGE, thick), junction remainder (pink), reserve edge (brown), medial line (orange dots). The third header line '
             'names the other window(s) a shared width comes from.', '',
             f'v2 -> v3: {t["counts"]}; new in v3: {t["new_count"]}. By v2 eye mark: {t["counts_by_v2_eye_mark"]}.',
             f'By-eye width error (mean absolute, m): IN-SAMPLE (5 roads) v2 {((ins.get("v2") or {}).get("width_abs_error_m"))} -> v3 '
             f'{((ins.get("v3") or {}).get("width_abs_error_m"))}; IN-SAMPLE (2 roads, v2/byeye) v2 {((ins2.get("v2") or {}).get("width_abs_error_m"))} '
             f'-> v3 {((ins2.get("v3") or {}).get("width_abs_error_m"))}; HELD-OUT (4 roads, 6 band objects) v2 '
             f'{((held.get("v2") or {}).get("width_abs_error_m"))} -> v3 {((held.get("v3") or {}).get("width_abs_error_m"))} (read by eye before '
             'any v3 code, outer kerb to outer kerb; nothing tuned on them).', '']
    for n, e in enumerate(order, 1):
        h = e.get('hint')
        hint = (f' HINT v2 #{h["v2_number"]} eye {h["eye_mark"]} ({h["eye_note"]}); v2 width {h["v2_width_m"]} m.' if h and h.get('v2_number') else
                ' HINT: new in v3 (no v2 band).' if not h else ' HINT: v2 band without an eye mark.')
        extra = []
        if e['replaces_on_accept']:
            extra.append(f'replaces on accept {", ".join(e["replaces_on_accept"])}')
        if e['retires_on_accept']:
            extra.append(f'RETIRES on accept {", ".join(e["retires_on_accept"])} (orange)')
        if e['joined']:
            extra.append(f'joins {len(e["joined"])} piece(s) {", ".join(e["joined"])}' + (f' ({len(e["canopy_joined"])} across a canopy)' if e['canopy_joined'] else ''))
        if e['continuation']:
            extra.append('continues through the canopy (dashed): ' + ', '.join(f'{c["end"]} {c["length_m"]} m to {c["to"]}' for c in e['continuation']))
        if e['indent_bays']:
            extra.append(f'widened for a car in a kerb indent ({", ".join(e["indent_bays"])})')
        if e['cars_straddling']:
            extra.append(f'{e["cars_straddling"]} car(s) only partly under it')
        if e['junction_arm']:
            extra.append(f'junction: longest straight arm, {e["remainder"]} other arm piece(s) stay as they are')
        if e['shared_with']:
            extra.append('shares width and centre line with ' + ', '.join(f'`{s["scene"]}` band `{s.get("proposal")}`' for s in e['shared_with'])
                         + (f' (the group narrowed by {e["xwin_trim"]["neg_m"]} / {e["xwin_trim"]["pos_m"]} m for a footpath in one window)' if e.get('xwin_trim') else ''))
        if e['linked_with']:
            extra.append('one street with band(s) ' + ', '.join(f'`{s.get("proposal")}`' for s in e['linked_with']) + ' in this window (linked through the canopy: one width and centre line)')
        if e['kerb']:
            extra.append(e['kerb'])
        lines.append(f'{n}. `{e["scene"]}` band `{e["proposal"]}` from road `{e["seed"]}` ({e["seed_status"]}): width {e["width_m"]} m '
                     f'({e["width_source"]}; own {e["width_own_m"]}, v2-style {e["width_v2_median_m"]}), vertices {e["vertices"]}, cars under '
                     f'{e["cars_under"]}; replaces {", ".join(e["replaces"]) or "-"}' + (f'; {"; ".join(extra)}' if extra else '') + f'.{hint} '
                     f'Render `prototype/{e["file"]}`. Mark: ____')
    lines += ['', '## Gone since v2 (no v3 band; the v2 band is drawn dashed magenta on the exit sheet)', '']
    for n, g in enumerate(gone_entries, 1):
        lines.append(f'G{n}. `{g["scene"]}` v2 #{g["v2_number"]} band `{g["v2_band"]}` (HINT eye {g["eye_mark"]}: {g["eye_note"]}): '
                     f'v3 exit `{g["v3_exit"]}` ({", ".join(g.get("v3_exit_reasons") or [])}). Sheet `prototype/{g["file"]}`. Agree: ____')
    (OUT/'MARKS-roads-v3.md').write_text('\n'.join(lines)+'\n')


# === road corridor v3.1 (design name 'v3.1', 2026-10-07 night) ==============================================================
# v1, v2 and v3 above behave as before: DESIGN, DESIGN_V2 and DESIGN_V3 lack the v3.1 keys and the four v3 hooks (kerb_extend,
# band_metrics_v3, trim_footpaths_v3, _v3_pass) branch on those keys only, so `--design v1 / v2 / v3` reproduce prototype/run-v1,
# run-v2 and run-v3. v3.1 = v3 with two changes, from the v3 code skeptic (v3/skeptic-notes.md F1) and the v3 by-eye tally
# (eye-tally-roads-v3.md: #6 and #8 wrong, the shared d43d / lot-25641 street at 5.98 m, 1.0-1.4 m too narrow):
#   1 own trim   cross-window groups keep v3's pooling (one centre line: the pooled station midpoints; one width: the pooled
#                median of the members' clean-station widths) but there is no group retrim. v3's xwin_retrim took, per side,
#                the deepest footpath trim of any member and narrowed every member by it (lot-25641's 0.80 m west trim pulled
#                win-d43d from its own 7.27 m to 5.98 m). In v3.1 a member's own guard (footpath edge trim, reserve cap) narrows
#                that member only; a member narrower than its group records 'width_mismatch_m' (group width - its width) and
#                'trimmed_by' (the trims / the cap). `run --design v3.1` runs pass 1, the pooling and pass 2 (xwin_passes).
#   2 over kerb  per side, a CANDIDATE (undecided) footpath / vegetation object whose edge lies inside the kerb read in the
#                image by more than cand_over_kerb_m on >= cand_over_kerb_share of the clean stations no longer stops that
#                side's kerb extension: a SAM footpath lying over the kerb and channel is a mask error, not the kerb. The kerb
#                is read with every other stop in place (the 'free' read); the edge is where the station ray from the mask edge
#                first enters the object (exact, not the 0.04 m probe). Recorded as 'candidate_over_kerb' (lifted) and
#                'candidate_over_kerb_below' (seen over the kerb on fewer stations). The part of a lifted object inside the
#                side's read kerb line (the straight line through the median extended edge of the clean stations) is not a
#                footpath blocker for this window's band, so the guard does not trim the band straight back off the kerb; its
#                part beyond that line still is (so a pooled width wider than this window's kerb is still trimmed here, item 1).
#                A decided object, and every other stop kind (driveway, building, pool, water), still stops the extension.

VERSION_V31 = 'road-corridor-v3.1'
DESIGN_V31 = {
    **DESIGN_V3, 'version': VERSION_V31, 'design_name': 'v3.1', 'generation': 3,
    # 1 a member's own trim narrows that member only (no group retrim)
    'xwin_own_trim': True,
    # 2 a candidate lying over the kerb read in the image does not stop the kerb extension
    'cand_over_kerb_m': .2, 'cand_over_kerb_share': .3, 'cand_over_kerb_kinds': ('footpath', 'vegetation'),
}
DESIGNS['v3.1'] = DESIGN_V31
# the agent by-eye marks on the v3 bands (eye-tally-roads-v3.md), keyed by the v3 band id prefix: (v3 #, mark, note)
V3_EYE_MARKS = {
    'c54634': (1, 'right', 'e31f frame to frame, W edge up to 0.65 m out in building shadow'),
    'c33e4f': (2, 'right', 'both edges on the light kerb strips'), 'c027f8': (3, 'right', 'own reading 9.8 (+0.23)'),
    '540a96': (4, 'right', 'kerb strips on both edges'), '115185': (5, 'right', 'd43d arm, edges on the outer kerb'),
    'e1aa00': (6, 'wrong', 'd43d main too narrow: 5.98 vs kerb to kerb ~7.1-7.35; the group retrim took lot-25641\'s 0.8 m trim'),
    'a7af22': (7, 'right', 'one 108 m band, borderline narrow'),
    'a46d25': (8, 'wrong', 'lot-25641 too narrow: W edge 1.0-1.2 m short of the outer kerb'),
    '3644ca': (9, 'marginal', 'mirrored, kerb_not_found; E kerb ~0.85 m outside'),
    '14a8e6': (11, 'right', '3fa3 through the crossroads, kerbs on the edges'),
    'd15c29': (10, 'right', '2fbb kerb strips on both edges, frame to frame'),
    'a1741b': (12, 'partial', 'one width with #11 but stops 14-18 m short of the N junction'),
    '940f7a': (13, 'right', 'edges at the kerb strips'), '65cfa4': (14, 'marginal', 'E edge ~1.05 m over a footpath build-out at 36-44 m'),
    'f815a0': (15, 'right', 'flush brick street (low confidence)'), 'f88865': (16, 'right', 'a740 kerb strips on both edges'),
    '116056': (17, 'partial', 'width right; W end stops at the canopy'),
}


def _ray_entry_m(a, b, poly, mpp):
    """Distance (m) from a along the segment a -> b to where it first enters poly (0 when a lies in it), or None."""
    g = LineString([tuple(a), tuple(b)]).intersection(poly)
    if g.is_empty:
        return None
    pa = Point(tuple(a))
    ds = [pa.distance(Point(c)) for part in getattr(g, 'geoms', [g]) for c in getattr(part, 'coords', [])]
    return float(min(ds))*mpp if ds else None


def kerb_extend_v31(res, ctx, design=DESIGN_V31):
    """kerb_extend (v3) with 2 (over kerb). The v3 read is made first (every stop in place, so a seed with no lifted candidate
    gets v3's kext / kroom). On each ray that enters a candidate (undecided cand_over_kerb_kinds object): the 'free' kerb read
    with the other stops only, and each candidate's exact entry distance. A candidate whose entry lies more than
    cand_over_kerb_m inside the free kerb on >= cand_over_kerb_share of the clean stations (the kerb_min_share base) is lifted
    on that side; that side's rays through it are read again without it (any other candidate on the ray still stops)."""
    if res.get('_kerb_done') or not res.get('sections'):
        return
    res['_kerb_done'] = True
    lum, mpp = ctx.get('lum'), ctx['mpp']
    from shapely import contains_xy
    own = {res['seed'], *res.get('siblings', []), *res.get('joined', []), *[m['id'] for m in res.get('twins', [])]}
    stops, hard, cand = [], [], {}
    for o in ctx['live']:
        if o['id'] not in ctx['polys'] or o['id'] in own:
            continue
        if not (o['kind'] in design['kerb_stop_kinds'] or (is_decided(o, design) and o['kind'] not in ('road', 'car', 'tree'))):
            continue
        stops.append(ctx['polys'][o['id']])                 # the v3 stop set, in the v3 order
        if o['kind'] in design['cand_over_kerb_kinds'] and not is_decided(o, design):
            cand[o['id']] = o
        else:
            hard.append(ctx['polys'][o['id']])
    stop_all = unary_union(stops) if stops else None
    stop_hard = unary_union(hard) if hard else None
    step = design['kerb_step_m']
    probe = np.arange(0., design['kerb_max_m']+1e-9, step)
    secs = [s for s in res['sections'] if not s['outside']]
    near = []
    if cand and secs:
        reach = (design['kerb_max_m']+.5)/mpp
        ends = np.asarray([s['p']+s['n']*e for s in secs for e in (s['lo'], s['hi'])], float)
        bb = box(ends[:, 0].min()-reach, ends[:, 1].min()-reach, ends[:, 0].max()+reach, ends[:, 1].max()+reach)
        near = [(oid, ctx['polys'][oid]) for oid in sorted(cand) if ctx['polys'][oid].intersects(bb)]
    rows = []
    for s in secs:
        s['kext'] = [None, None]
        s['kroom'] = [0., 0.]
        if lum is None:
            continue
        d = res['segments'][s['seg']]['d']
        for side, sign in ((0, -1.), (1, 1.)):
            if s['ends'][side] not in design['kerb_end_kinds']:
                continue
            edge = s['lo'] if side == 0 else s['hi']
            r_end = s.get('r_lo') if side == 0 else s.get('r_hi')
            if r_end is None:
                continue
            room0 = ((edge-r_end) if side == 0 else (r_end-edge))*mpp
            q = s['p'][None, :]+s['n'][None, :]*(edge+sign*probe[:, None]/mpp)
            room = room0
            if stop_all is not None:
                hit = np.nonzero(contains_xy(stop_all, q[:, 0], q[:, 1]))[0]
                if len(hit):
                    room = min(room, float(probe[hit[0]])-step)
            s['kroom'][side] = max(0., min(room, design['kerb_max_m']))
            got = kerb_step(lum, s['p'], d, s['n'], edge, sign, mpp, design, limit_m=room)
            if got is not None:
                s['kext'][side] = got['ext_m']
            hits = {}
            a = s['p']+s['n']*edge
            b = s['p']+s['n']*(edge+sign*design['kerb_max_m']/mpp)
            for oid, poly in near:
                h = np.nonzero(contains_xy(poly, q[:, 0], q[:, 1]))[0]
                if len(h):
                    e = _ray_entry_m(a, b, poly, mpp)
                    hits[oid] = (float(probe[h[0]]), float(probe[h[0]]) if e is None else e)
            if not hits:
                continue
            room_h = room0
            if stop_hard is not None:
                hit = np.nonzero(contains_xy(stop_hard, q[:, 0], q[:, 1]))[0]
                if len(hit):
                    room_h = min(room_h, float(probe[hit[0]])-step)
            free = kerb_step(lum, s['p'], d, s['n'], edge, sign, mpp, design, limit_m=room_h)
            rows.append({'s': s, 'side': side, 'sign': sign, 'edge': edge, 'd': d, 'room_h': room_h, 'hits': hits,
                         'free': None if free is None else free['ext_m']})
    clean_n = sum(1 for s in secs if s['clean'])
    need = design['cand_over_kerb_share']*max(1, clean_n)
    over = defaultdict(list)
    for row in rows:
        if not row['s']['clean'] or row['free'] is None:
            continue
        for oid, (_, entry) in row['hits'].items():
            if row['free']-entry > design['cand_over_kerb_m']:
                over[(row['side'], oid)].append(row['free']-entry)
    lifted, rec_l, rec_b = {0: set(), 1: set()}, [], []
    for (side, oid), v in sorted(over.items()):
        e = {'side': 'lo' if side == 0 else 'hi', 'id': oid, 'kind': cand[oid]['kind'], 'status': cand[oid].get('status'),
             'stations_over': len(v), 'stations': clean_n, 'share': r(len(v)/max(1, clean_n), 3), 'over_m_median': r(float(np.median(v)), 2)}
        if len(v) >= need:
            lifted[side].add(oid)
            rec_l.append(e)
        else:
            rec_b.append(e)
    for row in rows:
        lift = lifted[row['side']] & set(row['hits'])
        if not lift:
            continue
        s, side = row['s'], row['side']
        rest = [p for oid, (p, _) in row['hits'].items() if oid not in lifted[side]]
        room = min(row['room_h'], min(rest)-step) if rest else row['room_h']
        s['kroom'][side] = max(0., min(room, design['kerb_max_m']))
        got = kerb_step(lum, s['p'], row['d'], s['n'], row['edge'], row['sign'], mpp, design, limit_m=room)
        s['kext'][side] = None if got is None else got['ext_m']
    if rec_l:
        res['candidate_over_kerb'] = rec_l
    if rec_b:
        res['candidate_over_kerb_below'] = rec_b
    res['_cok_lifted'] = {side: sorted(v) for side, v in lifted.items() if v}


def over_kerb_polys(res, ctx, design=DESIGN_V31):
    """2: per lifted object, its part beyond the side's read kerb line: the straight line (per segment) through the median
    extended edge (lo_k / hi_k, set by build_band_v3) of the clean stations on the side it was lifted on."""
    lifted = res.get('_cok_lifted') or {}
    big = 4000.
    zones = {0: [], 1: []}
    for k, seg in enumerate(res['segments']):
        secs = [s for s in res['sections'] if not s['outside'] and s['seg'] == k and s.get('lo_k') is not None]
        use = [s for s in secs if s['clean']] or secs
        if not use:
            continue
        lo = float(np.median([s['lo_k'] for s in use]))
        hi = float(np.median([s['hi_k'] for s in use]))
        zones[0].append(strip_rect(seg['p'], seg['d'], -big, big, lo, big))
        zones[1].append(strip_rect(seg['p'], seg['d'], -big, big, -big, hi))
    out = {}
    for side, ids in sorted(lifted.items()):
        if not zones[side]:
            continue
        zone = unary_union(zones[side])
        for oid in ids:
            base = out.get(oid, ctx['polys'][oid])
            out[oid] = base.difference(zone)
    return out


def over_kerb_blockers(res, band_out, ctx, m, design=DESIGN_V31):
    """2: a lifted over-kerb candidate counts against footpath_eps_m2 (and is trimmed) by its part beyond the read kerb line
    only; one at or under eps there leaves the footpath blockers ('candidate_over_kerb_under_band')."""
    if not res.get('_cok_lifted') or not m.get('blockers_footpath'):
        return m
    if '_cok_eff' not in res:
        res['_cok_eff'] = over_kerb_polys(res, ctx, design)
    eff, mpp, band = res['_cok_eff'], ctx['mpp'], band_out['band']
    keep, under = [], []
    for b in m['blockers_footpath']:
        if b['id'] in eff:
            rest = area_m2(eff[b['id']].intersection(band), mpp) if not eff[b['id']].is_empty else 0.
            b = {**b, 'overlap_beyond_kerb_m2': r(rest, 2)}
            if rest <= design['footpath_eps_m2']:
                under.append(b)
                continue
        keep.append(b)
    m['blockers_footpath'] = keep
    if under:
        m['candidate_over_kerb_under_band'] = under
    return m


def xwin_mismatch(xo, band_out):
    """1 (v3.1): a member narrower than its cross-window group: the gap and what narrowed it (its own footpath edge trims,
    the reserve cap). Nothing when it holds the group width."""
    if not band_out or band_out.get('width_m') is None or xo.get('width_m') is None:
        return {}
    gap = float(xo['width_m'])-float(band_out['width_m'])
    if gap <= .005:
        return {}
    by = [{'edge': t['edge'], 'blocker': t['blocker'], 'kind': t['kind'], 'trim_m': t['trim_m']}
          for t in band_out.get('footpath_trims') or [] if t.get('edge')]
    if band_out.get('width_capped'):
        by.append({'by': 'reserve_cap', 'cap_m': band_out.get('width_cap_m')})
    return {'width_mismatch_m': r(gap, 2), 'trimmed_by': by}


def xwin_passes(run_window, names, scenes, design=DESIGN_V31):
    """The cross-window passes as one function (pure given run_window(scene_id, overrides or None) -> ({layer: report},
    [band records]); scenes = {scene_id: scene meta}, filled by pass 1 when run_window does so): pass 1 over names, the pooling
    (xwin_pool), pass 2 on the windows holding a pooled band; designs without xwin_own_trim (v3) add v3's group retrim and a
    pass 3. Returns (final {(scene, layer): report}, pass-1 bands, overrides, groups, pairs, retrim notes)."""
    final, bands = {}, []
    for scene_id in names:
        reps, b = run_window(scene_id, None)
        final.update({(scene_id, layer): rep for layer, rep in reps.items()})
        bands += b
    overrides, groups, pairs = xwin_pool(bands, scenes, design)
    for scene_id in sorted(overrides):
        reps, _ = run_window(scene_id, overrides[scene_id])
        final.update({(scene_id, layer): rep for layer, rep in reps.items()})
    notes = []
    if not design.get('xwin_own_trim'):
        records = {(s, layer, x['seed']): x for (s, layer), rep in final.items() for x in rep['proposals']+rep['exits']}
        retrim, notes = xwin_retrim(overrides, groups, records, scenes)
        for scene_id in sorted(retrim):
            merged = {layer: {**overrides[scene_id].get(layer, {}), **retrim[scene_id].get(layer, {})} for layer in ('model', 'review')}
            reps, _ = run_window(scene_id, merged)
            final.update({(scene_id, layer): rep for layer, rep in reps.items()})
    return final, bands, overrides, groups, pairs, notes


def xwin_members_final(groups, final):
    """1 (v3.1): each group member's final band: proposal id (None = exited), width, and the mismatch with the group width."""
    for g in groups:
        for m in g['members']:
            rep = final.get((m['scene'], g['layer']))
            rec = next((x for x in (rep or {}).get('proposals', [])+(rep or {}).get('exits', []) if x['seed'] == m['seed']), None)
            m['proposal_final'] = (rec.get('proposal') or {}).get('id') if rec else None
            m['exit_final'] = rec.get('exit') if rec else 'missing'
            m['width_final_m'] = rec.get('width_m') if rec else None
            m['width_mismatch_m'] = rec.get('width_mismatch_m') if rec else None
            m['trimmed_by'] = rec.get('trimmed_by') if rec else None
            m['candidate_over_kerb'] = rec.get('candidate_over_kerb') if rec else None
    return groups


# --- v3.1 run --------------------------------------------------------------------------------------------------------------------

def _layer_totals(final):
    totals = defaultdict(Counter)
    lists = defaultdict(lambda: defaultdict(list))
    per_scene = {}
    for (scene_id, layer), rep in sorted(final.items()):
        c = rep['counts']
        t = totals[layer]
        t['windows'] += 1
        for key in ('seeds', 'proposals', 'twin_groups', 'twin_pairs', 'specks_absorbed', 'siblings_absorbed', 'cars_under',
                    'canopy_joins', 'continued_ends', 'indent_bays', 'kerb_not_found', 'cars_straddling', 'joined_absorbed'):
            t[key] += c.get(key, 0)
        t['windows_with_proposal'] += bool(c['proposals'])
        for k, v in c['exits'].items():
            t[f'exit:{k}'] += v
        L = lists[layer]
        for p in rep['proposals']:
            for key in ('vertices_before', 'vertices_after', 'width_cv_before', 'width_cv_after', 'width_m', 'iou_modal',
                        'iou_amodal', 'building_overlap_m2', 'outside_reserve_m2'):
                L[key].append(p.get(key))
            t['shared_xwin'] += bool(p.get('shared_with'))
            t['width_mismatch'] += bool(p.get('width_mismatch_m'))
            t['candidate_over_kerb_bands'] += bool(p.get('candidate_over_kerb'))
            t['confirmed_seed_proposals'] += p['seed_status'] == 'confirmed'
        for x in rep['proposals']+rep['exits']:
            t['candidate_over_kerb_seeds'] += bool(x.get('candidate_over_kerb'))
        per_scene[f'{scene_id}.{layer}'] = {'proposals': c['proposals'], 'exits': c['exits']}

    def dist(values):
        v = [x for x in values if x is not None]
        return {'n': len(v), 'median': r(np.median(v)), 'p10': r(np.percentile(v, 10)), 'p90': r(np.percentile(v, 90)),
                'sum': r(sum(v), 2)} if v else {'n': 0}
    return totals, {layer: {k: dist(v) for k, v in L.items()} for layer, L in lists.items()}, per_scene


def run_v31(args):
    """run --design v3.1: xwin_passes (pass 1 over every window, both layers; pooling; pass 2, no retrim), then xwin.json (groups
    with each member's final width and mismatch) and metrics.json (totals, the v3 -> v3.1 table with the v3 eye marks, the
    by-eye width errors in-sample and HELD-OUT for v3 and v3.1, per held-out road). `run --design v3.2` runs the same with
    DESIGN_V32 (default out prototype/run-v32; the v3 -> v3.2 table keeps the v3.1 names)."""
    name = 'v3.2' if getattr(args, 'design', None) == 'v3.2' else 'v3.1'
    design = DESIGNS[name]
    out = Path(args.out) if getattr(args, 'out', None) else OUT/f'run-{name.replace(".", "")}'
    out.mkdir(parents=True, exist_ok=True)
    names = args.scenes or windows()
    byeye_sets = {'byeye': load_byeye(), 'byeye_new': load_byeye_new(), 'byeye_heldout': load_heldout()}
    retire = getattr(args, 'retire_map', None) or {}         # --retire: {scene: {object id: decision}}
    beat(f'run-{name} start: {len(names)} windows (pass 1)' + (f'; reviewer-retired twins {json.dumps(sorted(f"{s}:{i}" for s, m in retire.items() for i in m))}' if retire else ''))
    scenes, done = {}, Counter()

    def run_window(scene_id, xw):
        scene, review = load_scene(scene_id)
        reserve, lots, meta = scene_inputs(scene)
        image = np.asarray(Image.open(ROOT/'data/scenes'/scene_id/f"{scene['image_sha256']}.png").convert('RGB'))
        reps, bands = _v3_layer_run(scene_id, scene, review, reserve, lots, meta, image, xw, byeye_sets, design,
                                    retired=retire.get(scene_id))
        if retire.get(scene_id):
            reps['review']['review_revision'] = (review or {}).get('revision')
            reps['review']['review_updated_at'] = (review or {}).get('updated_at')
        scenes[scene_id] = _scene_meta(scene)
        got = {}
        for layer, rep in reps.items():
            data = jsonable(rep)
            json.dump(data, open(out/f'{scene_id}.{layer}.json', 'w'), indent=1)
            got[layer] = json.loads(json.dumps(data))
        stage = 'pass 1' if xw is None else 'pass 2'
        done[stage] += 1
        beat(f'run-{name} {stage} {done[stage]} {scene_id}: {len(bands)} band(s)')
        return got, bands
    final, all_bands, overrides, groups, pairs, _ = xwin_passes(run_window, names, scenes, design)
    beat(f'run-{name} cross-window: {len(groups)} group(s) over {sum(len(g["members"]) for g in groups)} band(s); pass 2 on {len(overrides)} window(s)')
    ids = {(s, rep['layer'], p['seed']): p['proposal']['id'] for (s, _), rep in final.items() for p in rep['proposals']}
    for (scene_id, layer), rep in final.items():
        touched = False
        for rec in rep['proposals']+rep['exits']:
            for sw in rec.get('shared_with') or []:
                sw['proposal'] = ids.get((sw['scene'], layer, sw['seed']))
                sw['exited'] = sw['proposal'] is None
                touched = True
        if touched:
            json.dump(rep, open(out/f'{scene_id}.{layer}.json', 'w'), indent=1)
    xwin_members_final(groups, final)
    json.dump(jsonable({'groups': groups, 'pairs': pairs, 'bands_pass1': [{k: v for k, v in b.items() if k not in ('mids_px', 'seg_pts')}
                                                                          for b in all_bands]}), open(out/'xwin.json', 'w'), indent=1)
    totals, dists, per_scene = _layer_totals(final)
    metrics = {'version': design['version'], 'written_at': datetime.now(UTC).isoformat(), 'assumption': ASSUMPTION,
               'design': {k: (list(v) if isinstance(v, tuple) else v) for k, v in design.items()},
               'totals': {layer: dict(t) for layer, t in totals.items()}, 'distributions': dists, 'per_scene': per_scene,
               'xwin_groups': groups}
    metrics.update(v31_summary(out, names, final))
    if retire:
        metrics['reviewer_retired'] = {f'{s}:{i}': d for s, m in sorted(retire.items()) for i, d in sorted(m.items())}
    json.dump(jsonable(metrics), open(out/'metrics.json', 'w'), indent=1)
    beat(f'run-{name} wrote metrics.json')
    print(json.dumps(jsonable(metrics['totals']), indent=1)[:1500])


def compare_v3_v31(run_dir, names, layer='review'):
    """For every v3 proposal of the layer: identical / changed / gone in v3.1 (same seed, else lineage, else IoU >= 0.3;
    identical = the same ring and width), with the v3 eye mark (HINT, review layer); v3.1 proposals matching none are 'new'."""
    rows, new = [], []
    for scene_id in names:
        f3, f31 = OUT/'run-v3'/f'{scene_id}.{layer}.json', run_dir/f'{scene_id}.{layer}.json'
        if not (f3.exists() and f31.exists()):
            continue
        r3, r31 = json.load(open(f3)), json.load(open(f31))
        used = set()
        for p3 in r3['proposals']:
            pid3 = p3['proposal']['id']
            mark = V3_EYE_MARKS.get(pid3[:6], (None, None, None)) if layer == 'review' else (None, None, None)
            best = next(((1., q) for q in r31['proposals'] if q['seed'] == p3['seed']), None)
            if best is None:
                b3 = _band_poly(p3)
                for q in r31['proposals']:
                    bq = _band_poly(q)
                    v = iou(b3, bq) if (b3 is not None and bq is not None) else 0.
                    if (p3['seed'] in q['proposal']['derived_from'] or v >= .3) and (best is None or v > best[0]):
                        best = (v, q)
            row = {'scene': scene_id, 'layer': layer, 'v3_number': mark[0], 'v3_band': pid3, 'v3_seed': p3['seed'],
                   'v3_width_m': p3['width_m'], 'eye_mark': mark[1], 'eye_note': mark[2]}
            if best is None:
                ex = next((e for e in r31['exits'] if e['seed'] == p3['seed']), None)
                row.update(v31='gone', v31_exit=ex['exit'] if ex else 'not_seeded', v31_exit_reasons=ex.get('exit_reasons') if ex else None,
                           v31_width_m=ex.get('width_m') if ex else None)
            else:
                q = best[1]
                used.add(q['proposal']['id'])
                same = q['proposal']['ring'] == p3['proposal']['ring'] and q['width_m'] == p3['width_m']
                b3, bq = _band_poly(p3), _band_poly(q)
                row.update(v31='identical' if same else 'changed', v31_band=q['proposal']['id'], v31_seed=q['seed'], v31_width_m=q['width_m'],
                           width_delta_m=r((q['width_m'] or 0)-(p3['width_m'] or 0), 2),
                           band_iou_v3_v31=r(iou(b3, bq)) if (b3 is not None and bq is not None) else None,
                           v31_width_own_m=q.get('width_own_m'), v31_kerb=(q.get('kerb') or {}).get('flag') or 'kerb',
                           v31_kerb_ext=[(q.get('kerb') or {}).get('ext_lo_m'), (q.get('kerb') or {}).get('ext_hi_m')],
                           v3_kerb_ext=[(p3.get('kerb') or {}).get('ext_lo_m'), (p3.get('kerb') or {}).get('ext_hi_m')],
                           v31_shared_with=[f"{s['scene']}:{s.get('proposal')}" for s in q.get('shared_with') or []],
                           v31_width_mismatch_m=q.get('width_mismatch_m'), v31_trimmed_by=q.get('trimmed_by'),
                           v31_candidate_over_kerb=q.get('candidate_over_kerb'), v3_xwin_trim=p3.get('xwin_trim'),
                           v31_footpath_trims=q.get('footpath_trims'), v3_footpath_trims=p3.get('footpath_trims'))
            rows.append(row)
        for q in r31['proposals']:
            if q['proposal']['id'] not in used:
                new.append({'scene': scene_id, 'layer': layer, 'v31_band': q['proposal']['id'], 'v31_seed': q['seed'], 'v31_width_m': q['width_m'],
                            'candidate_over_kerb': q.get('candidate_over_kerb'), 'shared_with': [s['scene'] for s in q.get('shared_with') or []]})
    by_mark = defaultdict(Counter)
    for row in rows:
        by_mark[str(row['eye_mark'])][row['v31']] += 1
    return {'rows': rows, 'new_v31_proposals': new, 'counts_by_v3_eye_mark': {k: dict(v) for k, v in sorted(by_mark.items())},
            'counts': dict(Counter(row['v31'] for row in rows)), 'new_count': len(new),
            'gone_exits': dict(Counter(row['v31_exit'] for row in rows if row['v31'] == 'gone'))}


def _per_road(agg):
    out = {}
    for b in agg['rows']:
        if not b.get('proposal'):
            continue
        out.setdefault(b.get('road') or b['object'], []).append(b)
    return {k: {'objects': [f"{b['scene']}:{b['object']}" for b in v], 'width_m': [b['width_m'] for b in v],
                'byeye_median_m': [b['byeye_median_m'] for b in v],
                'width_error_m': r(np.mean([b['width_error_m'] for b in v]), 2),
                'width_abs_error_m': r(np.mean([b['width_abs_error_m'] for b in v]), 2)} for k, v in sorted(out.items())}


def v31_summary(out, names, final):
    """metrics.json additions for v3.1: exits by reason, the v3 -> v3.1 tables (review: with the v3 eye marks; model), and the
    by-eye width errors of v3 (run-v3) and v3.1 on the IN-SAMPLE sets and the HELD-OUT set (v3/heldout-widths.json, read
    before any v3 code; nothing in v3 or v3.1 tuned on it), per road."""
    totals = defaultdict(Counter)
    for (_, layer), rep in final.items():
        for k, v in rep['counts']['exits'].items():
            totals[layer][k] += v
    held = load_heldout()
    sets = {'byeye_in_sample': ('byeye', load_byeye(), 'IN-SAMPLE: the 5 roads of kerb-widths-by-eye.json (informed v1 / v2 and the v3 kerb thresholds)'),
            'byeye_new_in_sample': ('byeye_new', load_byeye_new(), 'IN-SAMPLE for v3: the 2 roads of v2/byeye/new-readings.json (used to set the v3 kerb thresholds)'),
            'byeye_heldout': ('byeye_heldout', held, 'HELD-OUT: 4 roads read before any v3 code (v3/heldout-widths.json), outer kerb to outer kerb; nothing in v3 / v3.1 tuned on them')}
    res = {'exits_by_reason': {layer: dict(sorted(t.items())) for layer, t in totals.items()},
           'v3_vs_v31': compare_v3_v31(out, names, 'review'), 'v3_vs_v31_model': compare_v3_v31(out, names, 'model')}
    for name, (key, data, note) in sets.items():
        rows31 = _byeye_rows(out, names, key, data)
        rows3 = _byeye_rows(OUT/'run-v3', names, key, data)
        road = {(s['window'], s['object']): s.get('road') for s in (data or {}).get('stations', [])}
        for rr in (rows31, rows3):
            for b in rr:
                b['road'] = road.get((b['scene'], b['object']))
        a31, a3 = _byeye_agg(rows31, note), _byeye_agg(rows3, 'v3 bands (run-v3) on the same set')
        res[name] = {'v31': a31, 'v3': a3, 'per_road': {'v31': _per_road(a31), 'v3': _per_road(a3)}}
    return res


# --- v3.1 renders and marks -------------------------------------------------------------------------------------------------------

def render_v31(args):
    """Renders (render_proposal_v3: raw LEFT, annotated RIGHT) of every v3.1 review-layer band whose geometry differs from v3
    (renders-v31/<scene>/<band>.png; the title line names the v3 -> v3.1 change), exit sheets of every v3 band gone in v3.1
    (renders-v31/gone/, the v3 band dashed magenta), contact sheets, index.json and MARKS-roads-v31.md for ALL bands (an
    unchanged band points at its v3 render)."""
    run_dir = Path(args.out) if getattr(args, 'out', None) else OUT/'run-v31'
    rdir = OUT/'renders-v31'
    rdir.mkdir(parents=True, exist_ok=True)
    metrics = json.load(open(run_dir/'metrics.json'))
    table = metrics['v3_vs_v31']
    by31 = {row['v31_band']: row for row in table['rows'] if row.get('v31_band')}
    v3_files = {}
    idx3 = OUT/'renders-v3/index.json'
    if idx3.exists():
        v3_files = {e['proposal']: e['file'] for e in json.load(open(idx3))['proposals']}
    gone = defaultdict(list)
    for row in table['rows']:
        if row['v31'] == 'gone':
            gone[row['scene']].append(row)
    files = sorted(run_dir.glob('*.review.json'))
    entries, gone_entries = [], []
    beat(f'render-v3.1 start: {len(files)} review files')
    for f in files:
        rep = json.load(open(f))
        scene_id = rep['scene']

        def reuse(p):
            row = by31.get(p['proposal']['id']) or {}
            return row.get('v31') == 'identical' and row.get('v3_band') in v3_files
        changed = [p for p in rep['proposals'] if not reuse(p)]
        ctx = cache = scene = reserve = objects = None
        if changed or gone.get(scene_id):
            scene, review = load_scene(scene_id)
            reserve, lots, _ = scene_inputs(scene)
            mpp = scene_mpp(scene)
            objects = review['objects']
            image = np.asarray(Image.open(ROOT/'data/scenes'/scene_id/f"{scene['image_sha256']}.png").convert('RGB'))
            _, ctx, cache = corridor_layer(objects, width=scene['image_width'], height=scene['image_height'], mpp=mpp, reserve=reserve,
                                           lots=lots, design=DESIGN_V31, affine=scene['pixel_to_meters'], image=image)
        for rec in rep['proposals']:
            pid = rec['proposal']['id']
            row = by31.get(pid)
            same = reuse(rec)
            if same:
                file, size = v3_files[row['v3_band']], None
            else:
                res = cache.get(('v3', rec['seed'], tuple(rec.get('joined') or []), DESIGN_V31['adjacency_m'], DESIGN_V31['segment_tol_m'], DESIGN_V31['snap_deg']))
                if res is not None:
                    build_band_v3(res, ctx, DESIGN_V31)       # lo_k / hi_k on the sections for the kerb ticks
                note = []
                if row:
                    note.append(f'v3 {row["v3_width_m"]} -> v3.1 {rec["width_m"]} m')
                if rec.get('candidate_over_kerb'):
                    note.append('over-kerb candidate lifted: ' + ', '.join(f'{c["side"]} {c["id"][:8]} {c["stations_over"]}/{c["stations"]}' for c in rec['candidate_over_kerb']))
                if rec.get('width_mismatch_m'):
                    note.append(f'narrower than its group by {rec["width_mismatch_m"]} m (own trim)')
                title = f'{scene_id} {pid} seed {rec["seed"]} [{rec["seed_status"]}] v3.1' + (f' | {"; ".join(note)}' if note else '')
                path = rdir/scene_id/f'{pid}.png'
                _, size = render_proposal_v3(scene_id, rec, objects, scene, reserve, ctx['medial'], path, res, title=title)
                file = str(path.relative_to(OUT))
            entries.append({'crop_size': list(size) if size else None, 'scene': scene_id, 'proposal': pid, 'seed': rec['seed'],
                            'seed_status': rec['seed_status'], 'file': file, 'rendered_v31': not same, 'width_m': rec['width_m'],
                            'width_source': rec.get('width_source'), 'width_own_m': rec.get('width_own_m'),
                            'width_v2_median_m': rec.get('width_v2_median_m'), 'kerb': (rec.get('kerb') or {}).get('flag'),
                            'kerb_ext': [(rec.get('kerb') or {}).get('ext_lo_m'), (rec.get('kerb') or {}).get('ext_hi_m')],
                            'vertices': f'{rec["vertices_before"]}->{rec["vertices_after"]}', 'cars_under': rec['cars_under_n'],
                            'cars_straddling': len(rec.get('cars_straddling') or []),
                            'replaces': rec['proposal']['replaces'], 'replaces_on_accept': rec['proposal']['replaces_on_accept'],
                            'retires_on_accept': rec['proposal']['retires_on_accept'], 'joined': rec.get('joined') or [],
                            'canopy_joined': [a['id'] for a in rec['proposal']['absorbs'] if a['why'] == 'joined across a canopy'],
                            'continuation': [{'end': c['end'], 'to': c['to'], 'length_m': c['length_m']} for c in rec.get('continuation') or []],
                            'indent_bays': [b['car'] for b in rec.get('indent_bays') or []],
                            'shared_with': rec.get('shared_with') or [], 'linked_with': rec.get('linked_with') or [],
                            'width_mismatch_m': rec.get('width_mismatch_m'), 'trimmed_by': rec.get('trimmed_by'),
                            'candidate_over_kerb': rec.get('candidate_over_kerb'), 'footpath_trims': rec.get('footpath_trims'),
                            'remainder': len(rec['proposal']['remainder_on_accept']), 'junction_arm': bool((rec.get('junction') or {}).get('arm_a')),
                            'hint': row})
        for row in gone.get(scene_id, []):
            ex = next((e for e in rep['exits'] if e['seed'] == row['v3_seed']), None)
            if ex is None:
                continue
            v3rep = json.load(open(OUT/'run-v3'/f'{scene_id}.review.json'))
            ghost = next(p['proposal']['ring'] for p in v3rep['proposals'] if p['proposal']['id'] == row['v3_band'])
            path = rdir/'gone'/f'{scene_id}-{row["v3_band"]}.png'
            title = f'{scene_id} v3 #{row["v3_number"]} band {row["v3_band"]} (eye: {row["eye_mark"]}) GONE in v3.1: {row["v31_exit"]}'
            render_proposal_v3(scene_id, ex, objects, scene, reserve, ctx['medial'], path, None, ghost=ghost, title=title)
            gone_entries.append({**row, 'file': str(path.relative_to(OUT))})
        beat(f'render-v3.1 {scene_id}: {len(rep["proposals"])} band(s), {len(changed)} rendered')
    order = marks_order_v2(entries)
    rendered = [e for e in order if e['rendered_v31']]
    if rendered:
        contact_sheets_v3(rendered, rdir)
    write_marks_v31(order, gone_entries, metrics)
    json.dump({'proposals': order, 'gone': gone_entries}, open(rdir/'index.json', 'w'), indent=1, default=str)
    beat(f'render-v3.1 done: {len(order)} bands, {len(rendered)} rendered, {len(gone_entries)} gone sheets')
    print(len(order), 'bands', len(rendered), 'rendered', len(gone_entries), 'gone sheets')


def write_marks_v31(order, gone_entries, metrics):
    t = metrics['v3_vs_v31']
    held = metrics.get('byeye_heldout') or {}
    ins = metrics.get('byeye_in_sample') or {}
    ins2 = metrics.get('byeye_new_in_sample') or {}
    hr = (held.get('per_road') or {})

    def err(a, k='width_abs_error_m'):
        return (a or {}).get(k)
    roads = ', '.join(f'{k} v3 {hr.get("v3", {}).get(k, {}).get("width_error_m")} -> v3.1 {v.get("width_error_m")}'
                      for k, v in sorted((hr.get('v31') or {}).items()))
    lines = ['# MARKS: road corridor v3.1 (sidecar, 2026-10-07)', '',
             'Reviewer ask (7 Oct ~11:25): "roads need to converge on being straight lines of a fixed width etc. layer it underneath '
             'other objects like cars/trees etc." Each numbered line is one proposed straight, fixed-width road band on the review '
             'layer. Nothing is applied: a band replaces its road objects only if you accept it (a confirmed road is replaced only '
             'on accept).', '',
             f'Assumption to confirm: {ASSUMPTION}', '',
             'What changed since v3 (MARKS-roads-v3.md): (1) one street seen in two overlapping windows still gets one centre line '
             'and the pooled width, but a footpath trim in one window now narrows only that window\'s band (v3 narrowed the whole '
             'street by it); a band narrower than its street says so ("narrower than ... by"). (2) a SAM footpath / vegetation '
             'candidate lying over the kerb read in the image (its edge more than 0.2 m inside the kerb on at least 30 % of the '
             'stations of that side) no longer stops the band at its edge ("over-kerb candidate"); a confirmed object still does. '
             'Bands whose geometry did not change point at their v3 render.', '',
             'How to answer in chat: one word per line number, e.g. `1 right, 2 wrong (too wide), 3 unclear`. right = the band is '
             'the carriageway (outer kerb to outer kerb, parking lanes included) and may replace the listed objects; wrong = '
             'misplaced, too wide or narrow, or not a street; unclear = cannot tell from the render. For the "gone since v3" list, '
             'answer `G<n> agree` or `G<n> should have a band`.', '',
             'HINT = the agent by-eye skeptic mark on the matching v3 band (eye-tally-roads-v3.md), not your mark.', '',
             'Render: LEFT panel = imagery with the band outline (cyan, 1 px, no fill; the canopy continuation dashed white); RIGHT '
             'panel = modal polygon (yellow), joined pieces (light yellow), band (blue; continuation dashed), axis (red), kept '
             'sections (cyan) with the kerb extension (white ticks), dropped sections (magenta tree / white frame / orange other), '
             'cars under the band (bright green), straddling cars (red), indent-bay cars (magenta), reserve edge (brown), medial '
             'line (orange dots). The title line of a v3.1 render names the v3 -> v3.1 change.', '',
             f'v3 -> v3.1 (review layer): {t["counts"]}; new in v3.1: {t["new_count"]}. By v3 eye mark: {t["counts_by_v3_eye_mark"]}.',
             f'By-eye width error (mean absolute, m): IN-SAMPLE (5 roads) v3 {err(ins.get("v3"))} -> v3.1 {err(ins.get("v31"))}; '
             f'IN-SAMPLE (2 roads, v2/byeye) v3 {err(ins2.get("v3"))} -> v3.1 {err(ins2.get("v31"))}; HELD-OUT (4 roads) v3 '
             f'{err(held.get("v3"))} -> v3.1 {err(held.get("v31"))} (read by eye before any v3 code, outer kerb to outer kerb; '
             f'nothing tuned on them). Held-out signed error per road (band - eye, m): {roads}.', '']
    for n, e in enumerate(order, 1):
        h = e.get('hint') or {}
        if h.get('v3_number'):
            hint = f' HINT v3 #{h["v3_number"]} eye {h["eye_mark"]} ({h["eye_note"]}); v3 width {h["v3_width_m"]} m.'
        elif h:
            hint = f' HINT: v3 band {h.get("v3_band")} without an eye mark; v3 width {h.get("v3_width_m")} m.'
        else:
            hint = ' HINT: new in v3.1 (no v3 band).'
        extra = ['geometry CHANGED since v3' if e['rendered_v31'] else 'geometry as v3']
        if e['replaces_on_accept']:
            extra.append(f'replaces on accept {", ".join(e["replaces_on_accept"])}')
        if e['retires_on_accept']:
            extra.append(f'RETIRES on accept {", ".join(e["retires_on_accept"])} (orange)')
        if e['joined']:
            extra.append(f'joins {len(e["joined"])} piece(s) {", ".join(e["joined"])}' + (f' ({len(e["canopy_joined"])} across a canopy)' if e['canopy_joined'] else ''))
        if e['continuation']:
            extra.append('continues through the canopy (dashed): ' + ', '.join(f'{c["end"]} {c["length_m"]} m to {c["to"]}' for c in e['continuation']))
        if e['indent_bays']:
            extra.append(f'widened for a car in a kerb indent ({", ".join(e["indent_bays"])})')
        if e['cars_straddling']:
            extra.append(f'{e["cars_straddling"]} car(s) only partly under it')
        if e['junction_arm']:
            extra.append(f'junction: longest straight arm, {e["remainder"]} other arm piece(s) stay as they are')
        if e['shared_with']:
            extra.append('shares the centre line and pooled width with ' + ', '.join(f'`{s["scene"]}` band `{s.get("proposal")}`' for s in e['shared_with']))
        if e.get('width_mismatch_m'):
            by = ', '.join(f'{t.get("kind")} {t.get("blocker")} on the {t.get("edge")} edge ({t.get("trim_m")} m)' if t.get('edge') else 'the reserve cap'
                           for t in e.get('trimmed_by') or [])
            extra.append(f'narrower than its street by {e["width_mismatch_m"]} m (trimmed here only: {by or "-"})')
        if e.get('candidate_over_kerb'):
            extra.append('over-kerb candidate lifted: ' + ', '.join(f'{c["kind"]} `{c["id"]}` on the {c["side"]} side ({c["stations_over"]} of {c["stations"]} stations, '
                                                                     f'median {c["over_m_median"]} m inside the kerb)' for c in e['candidate_over_kerb']))
        if e['linked_with']:
            extra.append('one street with band(s) ' + ', '.join(f'`{s.get("proposal")}`' for s in e['linked_with']) + ' in this window (linked through the canopy)')
        if e['kerb']:
            extra.append(e['kerb'])
        v3w = f'v3 {h["v3_width_m"]} -> ' if h.get('v3_width_m') is not None else ''
        lines.append(f'{n}. `{e["scene"]}` band `{e["proposal"]}` from road `{e["seed"]}` ({e["seed_status"]}): width {v3w}{e["width_m"]} m '
                     f'({e["width_source"]}; own {e["width_own_m"]}, v2-style {e["width_v2_median_m"]}; kerb ext lo/hi {e["kerb_ext"][0]} / {e["kerb_ext"][1]} m), '
                     f'vertices {e["vertices"]}, cars under {e["cars_under"]}; replaces {", ".join(e["replaces"]) or "-"}; {"; ".join(extra)}.{hint} '
                     f'Render `prototype/{e["file"]}`. Mark: ____')
    lines += ['', '## Gone since v3 (no v3.1 band; the v3 band is drawn dashed magenta on the exit sheet)', '']
    if not gone_entries:
        lines.append('None: every v3 band has a v3.1 band.')
    for n, g in enumerate(gone_entries, 1):
        lines.append(f'G{n}. `{g["scene"]}` v3 #{g["v3_number"]} band `{g["v3_band"]}` (HINT eye {g["eye_mark"]}: {g["eye_note"]}): '
                     f'v3.1 exit `{g["v31_exit"]}` ({", ".join(g.get("v31_exit_reasons") or [])}). Sheet `prototype/{g["file"]}`. Agree: ____')
    (OUT/'MARKS-roads-v31.md').write_text('\n'.join(lines)+'\n')


# === road corridor v3.2 (design name 'v3.2', 2026-10-07 late night) =========================================================
# v1, v2, v3 and v3.1 behave as before: DESIGN .. DESIGN_V31 lack 'car_spur_rule', so analyse_seed_v2 passes no spur_ctx to
# mask_axis. v3.2 = v3.1 with ONE change, from band 1 at review rev 47 (.run/session-2026-10-07-roads/apply/band1/README.md):
# the reviewer's redraw of car 5577ce2151449c6d (+0.6 m at its north end, beside tree 066d9777) made the car-plus-mask outline
# throw a 3.24 m spur, so v3.1 read e31f2cb143c1b8dc as a junction (2 spur clusters, longest straight arm 18.36 m < arm_min_m).
#   car spur   when the junction spurs are counted on the pruned skeleton (protrusion >= junction_spur_m), a spur whose end
#              region (the last car_spur_end_m of the spur, buffered by car_spur_buffer_m) lies inside the unioned car polygons
#              (the cars of the amodal mask) and holds no pixel of the street's road-mask pieces (seed, same-mask siblings, P0
#              twins, joined pieces) is a car spur, not a side street: it is not clustered and does not count toward
#              'is_junction'. Recorded per seed as junction.car_spurs (end, protrusion and full spur length, the cars) with
#              junction.spur_tests (the pixel counts of every spur >= junction_spur_m). A spur ending half in a car and half on
#              road pixels, or on a real side street, counts as before.
# Band ids and the proposal 'source' stay v3.1's ('proposal_version'): a band built from the same pieces keeps the id the reviewer
# marked (MARKS-roads-v31.md numbers) and scene_road_promote (source road-corridor-v3.1) reads it; the reports and metrics carry
# 'road-corridor-v3.2' (design.version, design.design_name). `run --design v3.2` writes prototype/run-v32 by default.
VERSION_V32 = 'road-corridor-v3.2'
DESIGN_V32 = {
    **DESIGN_V31, 'version': VERSION_V32, 'design_name': 'v3.2', 'generation': 3, 'proposal_version': VERSION_V31,
    # the car spur rule
    'car_spur_rule': True, 'car_spur_end_m': 1., 'car_spur_buffer_m': .5,
}
DESIGNS['v3.2'] = DESIGN_V32


def retire_decisions(specs, decisions_path, scenes=None):
    """--retire SCENE:OBJECT_ID x n checked against the decisions file: it must be a human decision, one of its sentences must name
    the object id together with 'retire', a band that sentence names ('retired with band N') must be marked right, the scene
    must be in --scenes (when given) and its review layer must hold the object as a decided object. Returns
    {scene: {object id: decision}}; raises ValueError with the reason otherwise."""
    if not decisions_path:
        raise ValueError('--retire needs --decisions (the file that records the reviewer retiring the object)')
    path = Path(decisions_path)
    doc = json.load(open(path))
    if not str(doc.get('source') or '').startswith('human'):
        raise ValueError(f'{path}: source {doc.get("source")!r} is not a human decision')
    texts = []
    for key in ('defaults_accepted', 'decisions', 'retire'):
        val = doc.get(key)
        if isinstance(val, dict):
            texts += [(f'{key}.{k}', v) for k, v in sorted(val.items()) if isinstance(v, str)]
        elif isinstance(val, list):
            texts += [(f'{key}[{n}]', v) for n, v in enumerate(val) if isinstance(v, str)]
    marks = doc.get('marks_by_band_number') or {}
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    out = {}
    for spec in specs:
        scene_id, sep, oid = spec.partition(':')
        if not (sep and scene_id and oid):
            raise ValueError(f'--retire {spec!r}: expected SCENE:OBJECT_ID')
        if scenes and scene_id not in scenes:
            raise ValueError(f'--retire {spec}: {scene_id} is not in --scenes')
        hits = [(k, t) for k, t in texts if re.search(rf'\b{re.escape(oid)}\b', t) and 'retire' in t.lower()]
        if not hits:
            raise ValueError(f'--retire {spec}: {path} records no retire of {oid}')
        key, text = hits[0]
        band = re.search(r'band (\d+)', text)
        if band and marks and marks.get(band.group(1)) != 'right':
            raise ValueError(f'--retire {spec}: {key} retires it with band {band.group(1)}, marked {marks.get(band.group(1))!r} not right')
        rpath = ROOT/'data/scenes'/scene_id/'review.json'
        review = json.load(open(rpath)) if rpath.exists() else None
        obj = next((o for o in (review or {}).get('objects') or [] if o['id'] == oid), None)
        if obj is None:
            raise ValueError(f'--retire {spec}: the review layer of {scene_id} holds no object {oid}')
        if not is_decided(obj, DESIGN_V31):
            raise ValueError(f'--retire {spec}: {oid} reads {obj.get("status")} (not decided): the twin arbitration needs no retire')
        out.setdefault(scene_id, {})[oid] = {
            'by': 'reviewer', 'decisions': rel, 'entry': key, 'text': text, 'source': doc.get('source'),
            'sentence': doc.get('sentence'), 'transcribed_by': doc.get('transcribed_by'), 'transcribed_at': doc.get('transcribed_at'),
            'band': band.group(1) if band else None, 'band_mark': marks.get(band.group(1)) if band else None,
            'status_at_run': obj.get('status'), 'review_revision_at_check': (review or {}).get('revision')}
    return out


def main(argv=None):
    global PROGRESS
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('command', choices=('sweep', 'run', 'render'))
    parser.add_argument('--scenes', nargs='*')
    parser.add_argument('--design', choices=sorted(DESIGNS), default='v1', help="'v1' (default, reproduces run-v1), 'v2', 'v3', 'v3.1' or 'v3.2'")
    parser.add_argument('--out', help='run output folder (default prototype/run-<design>)')
    parser.add_argument('--progress', help='heartbeat file (default: the session stream folder of the design)')
    parser.add_argument('--retire', action='append', metavar='SCENE:OBJECT_ID',
                        help='run --design v3.1 / v3.2 only: a decided twin the reviewer retired (it causes no p0_conflict and is '
                             'listed as retires_on_accept); must be recorded as retired in --decisions')
    parser.add_argument('--decisions', help='the reviewer decisions file that records each --retire (e.g. decisions-2026-10-07.json)')
    args = parser.parse_args(argv)
    if args.command == 'render' and args.design == 'v3.2':
        parser.error('render --design v3.2 is not built (it would fall through to the v2 renders and MARKS-roads-v2.md)')
    if args.retire:
        if args.command != 'run' or args.design not in ('v3.1', 'v3.2'):
            parser.error('--retire is read by "run --design v3.1" / "run --design v3.2" only')
        if not args.out:
            parser.error('--retire needs --out (a retire run never overwrites prototype/run-v31)')
        try:
            args.retire_map = retire_decisions(args.retire, args.decisions, args.scenes)
        except ValueError as e:
            parser.error(str(e))
    if args.progress:
        PROGRESS = Path(args.progress)
    elif args.design != 'v1':
        PROGRESS = SESSION/f'{args.design.replace(".", "")}/progress.txt'
    {'sweep': sweep, 'run': run, 'render': render}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
