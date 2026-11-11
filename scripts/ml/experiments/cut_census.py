"""Object-cut census (2026-10-08). READ-ONLY over data/scenes; no model is loaded, nothing is re-inferred.

Reviewer (chat, 8 Oct ~08:45 AEDT, verbatim): "still seeing cuts randomly which then impactd the roof boundsary or tree boundary:
  http://localhost:3008/scenes?scene=win-3dd37155248c04ef&status=all&frame=all&object=1bac44c96bf45847&layer=edited
  http://localhost:3008/scenes?scene=win-3dd37155248c04ef&status=all&frame=all&object=55ca4c5c041e3646&layer=edited"

Question: where is one object's outline cut by something other than the imagery (the window frame, another object, a
contour-tracing artefact), how often, and what did the reviewer decide on those objects. Every figure is a count or a length;
nothing is accuracy, nothing is promoted, nothing under data/scenes, the registry, RULES / WINDOW / DESIGN or the eyes index is
written. Imports numpy, scipy.ndimage, shapely and PIL only (nothing from scene_extract, which pulls the SAM3 runtime).

Population: the 54 windows (data/scenes/win-*, lot-* with latest.json). Layers: model = latest.json objects, review =
review.json objects (what the editor shows as layer=edited). Live = status not rejected / superseded. Classified objects: live
kind building, tree, vegetation. Partners: every OTHER live object of the same layer, any kind.

Boundary classes. Every ring (exterior and holes, image px) is sampled every SAMPLE_PX along its edges; each sample gets ONE
class, in this precedence:
  spike         a frame-touching vertex excursion narrower than SPIKE_MAX_WIDTH_M (1.5 m). Two shapes: 'notch' / 'tab' = an
                off-frame vertex run between two on-frame vertices of the same frame side whose chord is < 1.5 m and whose
                enclosed area is < SPIKE_AREA_SHARE of the object and whose depth is >= 3 px and >= half its width (a
                shallower run is a 'shallow dent', counted apart: mostly an edge lying 1-3 px inside the frame); 'protrusion' = an on-frame vertex run shorter than 1.5 m whose
                polygon cross-section parallel to that side is narrower than min(1.5 m, depth) at some depth in SPIKE_DEPTHS_M
                (deeper than wide: a 90-degree corner touching the frame is not a spike) and whose part above that depth is
                < SPIKE_AREA_SHARE of the object. Objects whose minimum rotated
                rectangle is narrower than 1.5 m have no protrusion spikes (they are thin objects, not excursions).
  frame         within FRAME_TOL_PX (1 px) of the window frame (x <= 1, y <= 1, x >= W-1, y >= H-1).
  source_frame  a carried copy (carried_from.scene_id) only: within 1 px of its SOURCE frame's edge projected into the window
                through the georeferenced affine (scene_recapture.frame_transform arithmetic): the cut a carried slice brings in.
  carve         within PARTNER_TOL_PX (1 px) of ANOTHER live object's boundary over a run >= CARVE_MIN_M (1.5 m; gaps up to
                BRIDGE_PX bridged; frame / spike / source_frame samples excluded) whose median RGB step is < STEP_RGB.
  shared_strong the same proximity run on a strong image step (median >= STEP_RGB): the two outlines share a real image edge.
                Under the brief's four classes this is 'imagery'; it is reported separately because a roof that stops on a
                tree canopy's outline (an occlusion: the canopy hides the roof) is on a strong step by construction. The
                building<-tree subset is reported as 'occlusion candidates'.
  imagery       everything else.
  RGB step at a sample: max over normal offsets o in [-1.5, 1.5] px (0.5 px steps) of |RGB(p+(o+.75)n) - RGB(p+(o-.75)n)| / sqrt 3
  on the saved PNG (bilinear; pixel centres at +0.5), the measure and the 18 threshold of roof_boundary_guidance.py LIMITS
  'minimum_step_rgb' / its 'contrast' line. Sensitivity rows repeat the proximity at 2 px and 3 px.
Canopy-chopped corners (buildings; the 55ca pattern): on the exterior ring simplified at 0.35 m, a short 'chamfer' edge
(0.3-4 m) between two arms >= 1.5 m whose extensions meet OUTSIDE the roof at a 60-120 degree corner no deeper than 2 m
(roof_boundary.regularize_eaves' hidden-extension limit); canopy-chopped when the completed triangle is >= 50 % under the live
trees buffered 0.35 m (regularize_eaves' canopy buffer) or the chamfer runs within 4 px of a tree outline over >= 50 % of its
length. Corners whose chamfer touches the frame are skipped. This is the occlusion cut the 1 px carve test cannot see: a
straightened roof sits up to 0.35 m / 4 px from its mask (roof_surface.straighten_roof_boundary max_shift).
Per object: lengths (m) per class, frame sides, every carve / shared_strong run with its partner (id, kind, length, median step),
every spike (shape, side, width, depth, location), decision = the review.json status of the same id (model layer: 'absent' when
the id is not in review.json), geometry_reviewed, carried role.
An object HAS a frame edge when its frame length >= HAS_FRAME_M (0.5 m; shorter = a vertex touching the frame, counted apart).

Cross-frame bodies (option evidence, georeferenced only, NO residual measured): every building of every region-registered scene
(data/scenes/*/latest.json with region.bounds, qa_* skipped) in Web-Mercator units; for each live window building with a frame
edge, the best building of ANOTHER scene that lies inside its own frame by > 1 px, covers >= .8 (whole) / .5 (partial) of the
window object's footprint and reaches beyond the window frame. Whether a body carry would verify needs scene_recapture's measured
transform; these counts are an upper bound on what body carry could reach.

Run from the repo root:
    nice -n 19 scripts/ml/.venv/bin/python -m scripts.ml.experiments.cut_census [--skip-renders] [--skip-bodies] [--windows ...]
Outputs ONLY under data/reviews/eyes/experiments/object-cuts-2026-10-08/census/ (census.json, census.md, objects.jsonl,
renders/*.png, renders/contact-sheet.png) and heartbeat lines in .run/session-2026-10-08-cuts-veg/cuts/progress.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import shapely
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import map_coordinates
from shapely import make_valid
from shapely.geometry import LineString, Polygon, box
from shapely.strtree import STRtree

VERSION = 'object-cut-census-v1'
ROOT = Path(__file__).resolve().parents[3]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCENES = ROOT/'data/scenes'
OUT = ROOT/'data/reviews/eyes/experiments/object-cuts-2026-10-08/census'
RENDERS = OUT/'renders'
RUN = ROOT/'.run/session-2026-10-08-cuts-veg/cuts'
WINDOW_PREFIXES = ('win-', 'lot-')
SCRATCH_PREFIX = 'qa_'
KINDS = ('building', 'tree', 'vegetation')
LIVE_EXCLUDED = ('rejected', 'superseded')
LINEAR = ('road', 'driveway', 'footpath')
FRAME_TOL_PX = 1.0
PARTNER_TOL_PX = 1.0
SENSITIVITY_TOL_PX = (2.0, 3.0, 4.0)   # 4 px = roof_surface.straighten_roof_boundary max_shift min(.45 m, 4 px)
CARVE_MIN_M = 1.5
HAS_FRAME_M = 0.5
SPIKE_MAX_WIDTH_M = 1.5
SPIKE_AREA_SHARE = .25
SPIKE_DEPTHS_M = (.25, .5, .75, 1., 1.5)
SPIKE_MIN_DEPTH_PX = 3.0               # a notch shallower than 3 px is an edge running just past the 1 px frame tolerance
SPIKE_MIN_ASPECT = .5                  # notch depth >= .5 x its width (narrow and deep), else a shallow dent (counted apart)
STEP_RGB = 18.                         # scripts/ml/roof_boundary_guidance.py LIMITS['minimum_step_rgb']
STEP_HALF_PX = .75
STEP_OFFSETS_PX = np.arange(-1.5, 1.51, .5)
SAMPLE_PX = .5
BRIDGE_PX = 1.0
BODY_COVER = (.8, .5)
CORNER = {'simplify_m': .35, 'chamfer_max_m': 4., 'chamfer_min_m': .3, 'arm_min_m': 1.5, 'depth_max_m': 2.,
          'angle_deg': (60., 120.), 'tree_buffer_m': .35, 'tree_share': .5, 'edge_near_px': 4., 'edge_near_share': .5}
WORLD = 40075016.68557849              # scripts/ml/scene_addresses.py WORLD
NAMED = (('win-3dd37155248c04ef', '1bac44c96bf45847'), ('win-3dd37155248c04ef', '55ca4c5c041e3646'))
CLASSES = ('spike', 'frame', 'source_frame', 'carve', 'shared_strong', 'imagery')
CLASS_CODE = {name: i for i, name in enumerate(CLASSES)}
RENDER_LONG_SIDE = 520
COLOURS = {'outline': (255, 255, 255), 'frame': (255, 30, 30), 'source_frame': (255, 140, 0), 'carve': (255, 0, 255),
           'partner': (255, 120, 255), 'shared_strong': (0, 230, 255), 'spike': (255, 235, 0), 'window': (200, 200, 200),
           'text': (255, 255, 255), 'box': (0, 0, 0)}


# --- io ----------------------------------------------------------------------------------------------------------------

def load_json(path):
    with open(path) as handle:
        return json.load(handle)


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def heartbeat(message):
    RUN.mkdir(parents=True, exist_ok=True)
    with open(RUN/'progress.txt', 'a') as handle:
        handle.write(f'{datetime.now().strftime("%H:%M:%S")} {message}\n')


def population(root=SCENES):
    return [p for p in sorted(root.iterdir()) if p.is_dir() and p.name.startswith(WINDOW_PREFIXES) and (p/'latest.json').exists()]


def spacing(pixel_to_meters):
    m = np.asarray(pixel_to_meters, dtype=float)
    return math.sqrt(abs(float(np.linalg.det(m[:2]))))


def rel(path, base):
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


def r(value, digits=3):
    return None if value is None else round(float(value), digits)


# --- geometry ----------------------------------------------------------------------------------------------------------

def clean_ring(ring):
    pts = np.asarray(ring, dtype=float).reshape(-1, 2)
    if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    keep = np.r_[True, np.any(np.abs(np.diff(pts, axis=0)) > 1e-9, axis=1)]
    return pts[keep]


def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type == 'Polygon':
        return geometry
    parts = [g for g in getattr(geometry, 'geoms', []) if g.geom_type in ('Polygon', 'MultiPolygon') and not g.is_empty]
    if not parts:
        return None
    return shapely.unary_union(parts)


def object_polygon(obj):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None
    try:
        polygon = Polygon(ring, [h for h in (obj.get('holes') or []) if len(h) >= 3])
    except (ValueError, TypeError):
        return None
    if not polygon.is_valid:
        polygon = polygonal(make_valid(polygon))
    return polygon if polygon is not None and polygon.area > 0 else None


def shoelace(pts):
    x, y = pts[:, 0], pts[:, 1]
    return 0.5*abs(float(np.dot(x, np.roll(y, -1))-np.dot(y, np.roll(x, -1))))


def ring_samples(pts, step=SAMPLE_PX):
    """Midpoints of equal sub-pieces (<= step px) of every edge of a closed ring: points, unit normals, lengths, edge index."""
    nxt = np.roll(pts, -1, axis=0)
    seg = nxt-pts
    length = np.hypot(seg[:, 0], seg[:, 1])
    count = np.maximum(1, np.ceil(length/step).astype(int))
    edge = np.repeat(np.arange(len(pts)), count)
    offset = np.concatenate([(np.arange(c)+.5)/c for c in count])
    points = pts[edge]+offset[:, None]*seg[edge]
    safe = np.maximum(length, 1e-12)
    normal = np.stack([-seg[:, 1]/safe, seg[:, 0]/safe], axis=1)[edge]
    lengths = (length/count)[edge]
    return points, normal, lengths, edge


def frame_distance(points, width, height):
    return np.minimum.reduce([points[:, 0], points[:, 1], width-points[:, 0], height-points[:, 1]])


def sides_of(point, width, height, tol=FRAME_TOL_PX):
    x, y = float(point[0]), float(point[1])
    out = set()
    if x <= tol:
        out.add('left')
    if x >= width-tol:
        out.add('right')
    if y <= tol:
        out.add('top')
    if y >= height-tol:
        out.add('bottom')
    return out


def side_depth(points, side, width, height):
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    return {'left': points[:, 0], 'right': width-points[:, 0], 'top': points[:, 1], 'bottom': height-points[:, 1]}[side]


def cross_line(side, depth_px, centre, half_px, width, height):
    """A segment parallel to `side` at depth_px inside the frame, centred on the touch, half_px each way."""
    cx, cy = centre
    if side == 'top':
        return LineString([(cx-half_px, depth_px), (cx+half_px, depth_px)])
    if side == 'bottom':
        return LineString([(cx-half_px, height-depth_px), (cx+half_px, height-depth_px)])
    if side == 'left':
        return LineString([(depth_px, cy-half_px), (depth_px, cy+half_px)])
    return LineString([(width-depth_px, cy-half_px), (width-depth_px, cy+half_px)])


def line_pieces(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == 'LineString':
        return [geometry]
    return [g for g in getattr(geometry, 'geoms', []) if g.geom_type == 'LineString' and not g.is_empty]


def find_spikes(pts, polygon, width, height, s):
    """Frame-touching vertex excursions narrower than SPIKE_MAX_WIDTH_M on one exterior ring (px). Returns (spikes, edge mask)."""
    n = len(pts)
    spikes, edge_mask = [], np.zeros(n, dtype=bool)
    if n < 3 or polygon is None:
        return spikes, edge_mask
    sides = [sides_of(p, width, height) for p in pts]
    on = np.array([bool(x) for x in sides])
    if on.sum() == 0:
        return spikes, edge_mask
    area_px = polygon.area
    limit_px = SPIKE_MAX_WIDTH_M/s
    # notch / tab: an off-frame run between two on-frame vertices of one side, chord < 1.5 m, small enclosed area.
    q = np.flatnonzero(on)
    if len(q) >= 2:
        for k in range(len(q)):
            i, j = int(q[k]), int(q[(k+1) % len(q)])
            gap = (j-i) % n
            if gap <= 1:
                continue
            common = sides[i] & sides[j]
            if not common:
                continue
            chord = float(np.hypot(*(pts[j]-pts[i])))
            if chord <= 1e-9 or chord >= limit_px:
                continue
            idx = [(i+t) % n for t in range(gap+1)]
            run = pts[idx]
            enclosed = shoelace(run)
            if enclosed >= SPIKE_AREA_SHARE*area_px:
                continue
            side = sorted(common)[0]
            depth = float(side_depth(run, side, width, height).max())
            if depth < SPIKE_MIN_DEPTH_PX or depth < SPIKE_MIN_ASPECT*chord:
                spikes.append({'shape': 'shallow_dent', 'side': side, 'width_m': r(chord*s), 'depth_m': r(depth*s),
                               'at_px': [r(run[0][0], 2), r(run[0][1], 2)], 'vertices': gap-1})
                continue
            try:
                probe = Polygon(run)
                probe = probe if probe.is_valid else polygonal(make_valid(probe))
                inside = probe is not None and polygon.contains(probe.representative_point())
            except (ValueError, shapely.errors.GEOSException):
                inside = False
            tip = run[int(np.argmax(side_depth(run, side, width, height)))]
            for t in range(gap):
                edge_mask[(i+t) % n] = True
            spikes.append({'shape': 'tab' if inside else 'notch', 'side': side, 'width_m': r(chord*s), 'depth_m': r(depth*s),
                           'at_px': [r(tip[0], 2), r(tip[1], 2)], 'vertices': gap-1})
    # protrusion: an on-frame vertex run shorter than 1.5 m whose cross-section stays narrower than min(1.5 m, depth).
    rect = polygon.minimum_rotated_rectangle
    if rect.geom_type == 'Polygon':
        c = np.asarray(rect.exterior.coords)[:4]
        short = min(np.hypot(*(c[1]-c[0])), np.hypot(*(c[2]-c[1])))
    else:
        short = 0.
    if short*s < SPIKE_MAX_WIDTH_M or on.all():
        return spikes, edge_mask
    start = int(np.flatnonzero(~on)[0])
    order = [(start+t) % n for t in range(n)]
    runs, current = [], []
    for idx in order:
        if on[idx]:
            current.append(idx)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    for run in runs:
        frame_len = sum(float(np.hypot(*(pts[run[t+1]]-pts[run[t]]))) for t in range(len(run)-1))
        if frame_len >= limit_px:
            continue
        common = set.intersection(*[sides[i] for i in run])
        if not common:
            continue
        side = sorted(common)[0]
        centre = pts[run].mean(axis=0)
        hit = None
        for depth_m in SPIKE_DEPTHS_M:
            line = cross_line(side, depth_m/s, centre, 3./s, width, height)
            pieces = line_pieces(polygon.intersection(line))
            if not pieces:
                break
            anchor = shapely.Point(*{'top': (centre[0], depth_m/s), 'bottom': (centre[0], height-depth_m/s),
                                     'left': (depth_m/s, centre[1]), 'right': (width-depth_m/s, centre[1])}[side])
            piece = min(pieces, key=lambda g: g.distance(anchor))
            if piece.distance(anchor) > limit_px:
                break
            if piece.length*s < min(SPIKE_MAX_WIDTH_M, depth_m):
                hit = (depth_m, piece.length*s)
                break
        if hit is None:
            continue
        depth_px = hit[0]/s
        # The excursion (the polygon within the strip of that depth, the part touching the run) must be a small part of the
        # object: a small object touching the frame by a corner is not a spike.
        half = 3./s
        strip = {'top': box(centre[0]-half, -1, centre[0]+half, depth_px), 'bottom': box(centre[0]-half, height-depth_px, centre[0]+half, height+1),
                 'left': box(-1, centre[1]-half, depth_px, centre[1]+half), 'right': box(width-depth_px, centre[1]-half, width+1, centre[1]+half)}[side]
        touch = shapely.Point(*centre)
        parts = [g for g in getattr(polygon.intersection(strip), 'geoms', [polygon.intersection(strip)]) if g.geom_type == 'Polygon']
        part = min(parts, key=lambda g: g.distance(touch)) if parts else None
        if part is None or part.area >= SPIKE_AREA_SHARE*polygon.area:
            continue
        before, after = (run[0]-1) % n, (run[-1]+1) % n
        marked = set(range(run[0], run[0]+len(run)-1))
        a = before
        while side_depth(pts[a], side, width, height)[0] <= depth_px and len(marked) < n:
            marked.add(a)
            a = (a-1) % n
        marked.add(a)
        b = after
        while side_depth(pts[b], side, width, height)[0] <= depth_px and len(marked) < n:
            marked.add((b-1) % n)
            b = (b+1) % n
        marked.add((b-1) % n)
        for e in marked:
            edge_mask[e % n] = True
        spikes.append({'shape': 'protrusion', 'side': side, 'width_m': r(hit[1]), 'depth_m': r(hit[0]),
                       'at_px': [r(centre[0], 2), r(centre[1], 2)], 'vertices': len(run)})
    return spikes, edge_mask


def chopped_corners(polygon, trees, s, width, height):
    """Roof corners cut diagonally (the 55ca pattern): on the exterior ring simplified at CORNER simplify_m, a short 'chamfer'
    edge between two arms >= arm_min_m whose extensions meet outside the roof at a 60-120 degree corner no deeper than
    depth_max_m (roof_boundary.regularize_eaves' hidden limit). Canopy-chopped when the completed triangle is >= tree_share
    under the live trees (buffered tree_buffer_m, regularize_eaves' canopy buffer) or the chamfer runs within edge_near_px of a
    tree outline over >= edge_near_share of its length. Corners on the frame are skipped."""
    if polygon is None or polygon.geom_type != 'Polygon':
        return []
    ring = np.asarray(polygon.exterior.simplify(CORNER['simplify_m']/s).coords)[:-1]
    n = len(ring)
    if n < 4:
        return []
    canopy = shapely.unary_union([t for t in trees]) if trees else None
    canopy_b = canopy.buffer(CORNER['tree_buffer_m']/s) if canopy is not None else None
    out = []
    for i in range(n):
        a0, a1 = ring[i-1], ring[i]                 # arm before the chamfer ends at a1
        b0, b1 = ring[(i+1) % n], ring[(i+2) % n]   # chamfer a1 -> b0, arm after starts at b0
        chamfer = np.hypot(*(b0-a1))*s
        arm_a, arm_b = np.hypot(*(a1-a0))*s, np.hypot(*(b1-b0))*s
        if not (CORNER['chamfer_min_m'] <= chamfer <= CORNER['chamfer_max_m']) or min(arm_a, arm_b) < CORNER['arm_min_m']:
            continue
        if (frame_distance(np.array([a1, b0]), width, height) <= FRAME_TOL_PX).any():
            continue
        da, db = a1-a0, b1-b0
        det = da[0]*(-db[1])-da[1]*(-db[0])
        if abs(det) < 1e-9:
            continue
        t, u = np.linalg.solve(np.array([da, -db]).T, b0-a0)
        corner = a0+t*da
        cos = float(np.dot(-da, db)/(np.linalg.norm(da)*np.linalg.norm(db)))
        angle = math.degrees(math.acos(max(-1., min(1., cos))))
        if not (CORNER['angle_deg'][0] <= angle <= CORNER['angle_deg'][1]) or t <= 1 or u >= 0:
            continue
        tri = Polygon([a1, corner, b0])
        if not tri.is_valid or tri.area <= 0 or polygon.contains(shapely.Point(*corner)):
            continue
        depth = LineString([a1, b0]).distance(shapely.Point(*corner))*s
        if depth > CORNER['depth_max_m'] or depth*1e3 < 1:
            continue
        share = tri.intersection(canopy_b).area/tri.area if canopy_b is not None else 0.
        near = 0.
        if canopy is not None:
            pts, _, lengths, _ = ring_samples(np.array([a1, b0]))
            half = len(pts)//2
            dist = shapely.distance(shapely.points(pts[:half]), canopy.boundary)
            near = float(lengths[:half][dist <= CORNER['edge_near_px']].sum()/max(lengths[:half].sum(), 1e-9))
        out.append({'at_px': [r(corner[0], 2), r(corner[1], 2)], 'chamfer_m': r(chamfer), 'depth_m': r(depth),
                    'triangle_m2': r(tri.area*s*s), 'angle_deg': r(angle, 1), 'tree_share': r(share), 'edge_near_tree_share': r(near),
                    'canopy_chopped': bool(share >= CORNER['tree_share'] or near >= CORNER['edge_near_share'])})
    return out


# --- image -------------------------------------------------------------------------------------------------------------

def rgb_step(channels, points, normals):
    """Max over normal offsets of the RGB step across 1.5 px (roof_boundary_guidance 'contrast'), per sample."""
    if not len(points):
        return np.zeros(0)
    o = STEP_OFFSETS_PX[:, None, None]
    a = points[None]+(o-STEP_HALF_PX)*normals[None]
    b = points[None]+(o+STEP_HALF_PX)*normals[None]
    def sample(p):
        flat = p.reshape(-1, 2)
        coords = np.vstack([flat[:, 1]-.5, flat[:, 0]-.5])
        return np.stack([map_coordinates(c, coords, order=1, mode='nearest') for c in channels])
    va, vb = sample(a), sample(b)
    diff = np.linalg.norm(va-vb, axis=0)/math.sqrt(3)
    return diff.reshape(len(STEP_OFFSETS_PX), -1).max(axis=0)


# --- runs --------------------------------------------------------------------------------------------------------------

def cyclic_runs(mask, lengths, bridge_px):
    """Maximal cyclic runs of True samples, bridging False gaps of total length <= bridge_px. Returns index arrays."""
    n = len(mask)
    if n == 0 or not mask.any():
        return []
    if mask.all():
        return [np.arange(n)]
    start = int(np.flatnonzero(~mask)[0])
    order = np.roll(np.arange(n), -start)
    m, L = mask[order], lengths[order]
    runs, i = [], 0
    while i < n:
        if m[i]:
            j = i
            while j < n and m[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    merged = [runs[0]]
    for a, b in runs[1:]:
        if L[merged[-1][1]:a].sum() <= bridge_px:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    out = [order[a:b] for a, b in merged]
    if len(merged) > 1 and L[merged[-1][1]:].sum()+L[:merged[0][0]].sum() <= bridge_px:
        first, last = merged[0], merged[-1]
        joined = np.r_[order[last[0]:], order[:first[1]]]
        out = [joined]+out[1:-1]
    return out


# --- frames ------------------------------------------------------------------------------------------------------------

def mercator_origin(scene):
    lon, lat = scene['centroid']['lon'], scene['centroid']['lat']
    return lon/360*WORLD, math.asinh(math.tan(math.radians(lat)))/(2*math.pi)*WORLD, math.cos(math.radians(lat))


def pixels_to_mercator(points, scene, origin=None):
    x0, y0, c = origin or mercator_origin(scene)
    m = np.asarray(scene['pixel_to_meters'], dtype=float)
    p = np.asarray(points, dtype=float).reshape(-1, 2)
    metres = p @ m[:2]+m[2]
    return np.stack([x0+metres[:, 0]/c, y0+metres[:, 1]/c], axis=1)


def mercator_to_pixels(points, scene, origin=None):
    x0, y0, c = origin or mercator_origin(scene)
    m = np.asarray(scene['pixel_to_meters'], dtype=float)
    p = np.asarray(points, dtype=float).reshape(-1, 2)
    metres = np.stack([(p[:, 0]-x0)*c, (p[:, 1]-y0)*c], axis=1)
    return (metres-m[2]) @ np.linalg.inv(m[:2])


_SOURCE_CACHE = {}


def source_frame_line(obj, window):
    """The carried copy's source frame edge in window px (LineString ring), or None."""
    carried = obj.get('carried_from')
    if not isinstance(carried, dict) or not carried.get('scene_id'):
        return None
    sid = carried['scene_id']
    if sid not in _SOURCE_CACHE:
        path = SCENES/sid/'latest.json'
        if not path.exists():
            _SOURCE_CACHE[sid] = None
        else:
            scene = load_json(path)
            _SOURCE_CACHE[sid] = {k: scene.get(k) for k in ('centroid', 'pixel_to_meters', 'image_width', 'image_height')}
    source = _SOURCE_CACHE[sid]
    if not source:
        return None
    w, h = source['image_width'], source['image_height']
    corners = pixels_to_mercator([[0, 0], [w, 0], [w, h], [0, h], [0, 0]], source)
    return LineString(mercator_to_pixels(corners, window))


# --- classification ----------------------------------------------------------------------------------------------------

def pair_group(kind):
    if kind in ('building', 'tree', 'vegetation'):
        return kind
    return 'linear' if kind in LINEAR else 'other'


def classify_object(obj, polygon, partners, tree, window, channels, s, *, detail=False):
    """One object's boundary classes. partners: [(id, kind, polygon)] of the same layer; tree: STRtree over their polygons."""
    width, height = window['image_width'], window['image_height']
    rings = [clean_ring(obj['ring'])]+[clean_ring(h) for h in (obj.get('holes') or []) if len(h) >= 3]
    rings = [p for p in rings if len(p) >= 3]
    src_line = source_frame_line(obj, window)
    candidates = []
    if polygon is not None and tree is not None:
        grown = box(*polygon.buffer(max(SENSITIVITY_TOL_PX)+1).bounds)
        for k in tree.query(grown):
            pid, pkind, ppoly = partners[int(k)]
            if pid != obj['id']:
                candidates.append((pid, pkind, ppoly.boundary))
    totals = Counter()
    sens = {tol: Counter() for tol in SENSITIVITY_TOL_PX}
    sens_runs = {tol: [] for tol in SENSITIVITY_TOL_PX}
    runs_out, spikes_out, dents_out, sides = [], [], [], set()
    rings_detail = []
    perimeter = 0.
    for ring_index, pts in enumerate(rings):
        points, normals, lengths, edges = ring_samples(pts)
        perimeter += lengths.sum()*s
        cls = np.full(len(points), CLASS_CODE['imagery'], dtype=np.int8)
        if ring_index == 0:
            spikes, edge_mask = find_spikes(pts, polygon, width, height, s)
            spikes_out.extend(x for x in spikes if x['shape'] != 'shallow_dent')
            dents_out.extend(x for x in spikes if x['shape'] == 'shallow_dent')
            spike_mask = edge_mask[edges]
        else:
            spike_mask = np.zeros(len(points), dtype=bool)
        frame_mask = (frame_distance(points, width, height) <= FRAME_TOL_PX) & ~spike_mask
        for p in points[frame_mask]:
            sides |= sides_of(p, width, height)
        src_mask = np.zeros(len(points), dtype=bool)
        if src_line is not None:
            src_mask = (shapely.distance(shapely.points(points), src_line) <= FRAME_TOL_PX) & ~spike_mask & ~frame_mask
        excluded = spike_mask | frame_mask | src_mask
        steps = rgb_step(channels, points, normals)
        distances = []
        if candidates:
            geoms = shapely.points(points)
            for pid, pkind, boundary in candidates:
                distances.append((pid, pkind, shapely.distance(geoms, boundary)))
        cls[spike_mask] = CLASS_CODE['spike']
        cls[frame_mask] = CLASS_CODE['frame']
        cls[src_mask] = CLASS_CODE['source_frame']
        def shared_runs(tol, distances=distances, excluded=excluded, lengths=lengths, steps=steps, ring_index=ring_index):
            found = []
            for pid, pkind, dist in distances:
                near = (dist <= tol) & ~excluded
                for idx in cyclic_runs(near, lengths, BRIDGE_PX):
                    run_m = float(lengths[idx].sum()*s)
                    if run_m < CARVE_MIN_M:
                        continue
                    step = float(np.median(steps[idx]))
                    found.append({'partner_id': pid, 'partner_kind': pkind, 'length_m': r(run_m),
                                  'median_step_rgb': r(step, 1), 'class': 'carve' if step < STEP_RGB else 'shared_strong',
                                  'ring': ring_index, '_idx': idx})
            return found
        primary = shared_runs(PARTNER_TOL_PX)
        # One sample may sit in runs of two partners: the class is carve if any covering run is carve.
        for run in sorted(primary, key=lambda x: x['class'] == 'carve'):
            cls[run['_idx']] = CLASS_CODE[run['class']]
        for tol in SENSITIVITY_TOL_PX:
            found = shared_runs(tol)
            mask_c = np.zeros(len(points), dtype=bool)
            mask_s = np.zeros(len(points), dtype=bool)
            for run in found:
                (mask_c if run['class'] == 'carve' else mask_s)[run['_idx']] = True
            sens[tol]['carve'] += float(lengths[mask_c].sum()*s)
            sens[tol]['shared_strong'] += float(lengths[mask_s & ~mask_c].sum()*s)
            sens_runs[tol].extend({k: v for k, v in run.items() if k != '_idx'} for run in found)
        for name, code in CLASS_CODE.items():
            totals[name] += float(lengths[cls == code].sum()*s)
        runs_out.extend({k: v for k, v in run.items() if k != '_idx'} for run in primary)
        if detail:
            rings_detail.append({'points': points, 'cls': cls, 'steps': steps})
    corners = []
    if obj['kind'] == 'building' and polygon is not None:
        trees = [pp for pid, pk, pp in partners if pk == 'tree' and pp.distance(polygon) <= CORNER['depth_max_m']/s]
        corners = chopped_corners(polygon, trees, s, width, height)
    row = {'lengths_m': {k: r(totals[k]) for k in CLASSES}, 'perimeter_m': r(perimeter), 'frame_sides': sorted(sides),
           'chamfer_corners': corners, 'shallow_dents': dents_out,
           'runs': runs_out, 'spikes': spikes_out,
           'sensitivity': {f'{tol:g}px': {'carve_m': r(sens[tol]['carve']), 'shared_strong_m': r(sens[tol]['shared_strong']),
                                          'runs': sens_runs[tol]} for tol in SENSITIVITY_TOL_PX},
           'source_frame_known': src_line is not None}
    if detail:
        row['_rings'] = rings_detail
        row['_source_line'] = src_line
    return row


def analyse_window(path, *, detail_ids=None):
    latest = load_json(path/'latest.json')
    review_path = path/'review.json'
    review = load_json(review_path) if review_path.exists() else None
    width, height = latest['image_width'], latest['image_height']
    s = spacing(latest['pixel_to_meters'])
    image = Image.open(path/f'{latest["image_sha256"]}.png').convert('RGB')
    array = np.asarray(image, dtype=np.float32)
    channels = [np.ascontiguousarray(array[..., c]) for c in range(3)]
    review_by_id = {o['id']: o for o in (review or {}).get('objects', [])}
    model_by_id = {o['id']: o for o in latest.get('objects', [])}
    rows, details = [], {}
    layers = [('model', latest.get('objects', []))]+([('review', review.get('objects', []))] if review else [])
    for layer, objects in layers:
        live = [o for o in objects if o.get('status') not in LIVE_EXCLUDED and len(o.get('ring') or []) >= 3]
        partners = []
        for o in live:
            p = object_polygon(o)
            if p is not None:
                partners.append((o['id'], o['kind'], p))
        tree = STRtree([p for _, _, p in partners]) if partners else None
        poly_by_id = {pid: p for pid, _, p in partners}
        for o in live:
            if o['kind'] not in KINDS:
                continue
            want = detail_ids is not None and (layer, o['id']) in detail_ids
            if detail_ids is not None and not want:
                continue
            polygon = poly_by_id.get(o['id'])
            result = classify_object(o, polygon, partners, tree, latest, channels, s, detail=want)
            rv = review_by_id.get(o['id'])
            md = model_by_id.get(o['id'])
            carried = o.get('carried_from') if isinstance(o.get('carried_from'), dict) else None
            row = {'window': path.name, 'layer': layer, 'id': o['id'], 'kind': o['kind'], 'status': o.get('status'),
                   'decision': (rv or {}).get('status', 'absent'),
                   'geometry_reviewed': bool((rv or {}).get('geometry_reviewed')),
                   'edited_vs_model': bool(rv and md and (rv.get('ring') != md.get('ring') or rv.get('holes') != md.get('holes'))),
                   'source': o.get('source'), 'carried_role': (carried or {}).get('role'),
                   'area_m2': r(polygon.area*s*s if polygon is not None else shoelace(clean_ring(o['ring']))*s*s, 2),
                   'image': [width, height], 'm_per_px': r(s, 4),
                   'construction_reason': ((o.get('boundary_refinement') or {}).get('construction') or {}).get('reason'),
                   **result}
            if want:
                details[(layer, o['id'])] = (row, poly_by_id, {pid: k for pid, k, _ in partners})
            rows.append(row)
    if detail_ids is not None:
        return details, image, latest
    return rows, {'window': path.name, 'image': [width, height], 'm_per_px': r(s, 4),
                  'latest_sha256': file_sha(path/'latest.json'), 'review_sha256': file_sha(review_path) if review else None,
                  'review_revision': (review or {}).get('revision'), 'model_signature': latest.get('model_signature')}


# --- flags and aggregates ----------------------------------------------------------------------------------------------

def flags(row):
    L = row['lengths_m']
    carve_pairs = sorted({pair_group(x['partner_kind']) for x in row['runs'] if x['class'] == 'carve'})
    strong_pairs = sorted({pair_group(x['partner_kind']) for x in row['runs'] if x['class'] == 'shared_strong'})
    return {'frame': L['frame'] >= HAS_FRAME_M, 'frame_touch_only': 0 < L['frame'] < HAS_FRAME_M,
            'source_frame': L['source_frame'] >= HAS_FRAME_M, 'spike': bool(row['spikes']), 'carve': bool(carve_pairs),
            'carve_pairs': carve_pairs, 'shared_strong': bool(strong_pairs), 'shared_strong_pairs': strong_pairs,
            'occlusion_candidate': row['kind'] == 'building' and 'tree' in strong_pairs,
            'canopy_chopped_corner': any(c['canopy_chopped'] for c in row.get('chamfer_corners', [])),
            **{f'carve_{t:g}px': row['sensitivity'][f'{t:g}px']['carve_m'] > 0 for t in SENSITIVITY_TOL_PX},
            **{f'occlusion_{t:g}px': row['kind'] == 'building' and any(
                x['class'] == 'shared_strong' and x['partner_kind'] == 'tree' for x in row['sensitivity'][f'{t:g}px']['runs'])
               for t in SENSITIVITY_TOL_PX},
            **{f'carve_pairs_{t:g}px': sorted({pair_group(x['partner_kind']) for x in row['sensitivity'][f'{t:g}px']['runs']
                                              if x['class'] == 'carve'}) for t in SENSITIVITY_TOL_PX}}


def aggregate(rows):
    out = {}
    for layer in ('model', 'review'):
        out[layer] = {}
        for kind in KINDS:
            sel = [x for x in rows if x['layer'] == layer and x['kind'] == kind]
            f = [flags(x) for x in sel]
            lengths = {c: r(sum(x['lengths_m'][c] for x in sel), 1) for c in CLASSES}
            perim = sum(x['perimeter_m'] for x in sel)
            pairs = Counter(p for fl in f for p in fl['carve_pairs'])
            strong = Counter(p for fl in f for p in fl['shared_strong_pairs'])
            def decisions(pred, sel=sel, f=f):
                return dict(Counter(x['decision'] for x, fl in zip(sel, f, strict=True) if pred(fl)).most_common())
            out[layer][kind] = {
                'objects': len(sel),
                'with_frame_edge': sum(fl['frame'] for fl in f), 'frame_touch_only': sum(fl['frame_touch_only'] for fl in f),
                'with_source_frame_edge': sum(fl['source_frame'] for fl in f),
                'with_spike': sum(fl['spike'] for fl in f), 'spikes': sum(len(x['spikes']) for x in sel),
                'spike_shapes': dict(Counter(sp['shape'] for x in sel for sp in x['spikes'])),
                'spike_depth_m_median': (sorted(sp['depth_m'] for x in sel for sp in x['spikes'])[sum(len(x['spikes']) for x in sel)//2]
                                         if any(x['spikes'] for x in sel) else None),
                'spikes_deeper_than_0_5m': sum(sp['depth_m'] >= .5 for x in sel for sp in x['spikes']),
                'shallow_dents': sum(len(x.get('shallow_dents', [])) for x in sel),
                'with_carve_edge': sum(fl['carve'] for fl in f), 'carve_partner_groups': dict(pairs.most_common()),
                'with_shared_strong_edge': sum(fl['shared_strong'] for fl in f), 'shared_strong_partner_groups': dict(strong.most_common()),
                'occlusion_candidates': sum(fl['occlusion_candidate'] for fl in f),
                'with_canopy_chopped_corner': sum(fl['canopy_chopped_corner'] for fl in f),
                'canopy_chopped_corners': sum(c['canopy_chopped'] for x in sel for c in x.get('chamfer_corners', [])),
                'chamfer_corners_all': sum(len(x.get('chamfer_corners', [])) for x in sel),
                'canopy_chopped_with_frame_edge': sum(fl['canopy_chopped_corner'] and fl['frame'] for fl in f),
                'sensitivity': {f'{t:g}px': {'with_carve': sum(fl[f'carve_{t:g}px'] for fl in f),
                                             'carve_partner_groups': dict(Counter(p for fl in f for p in fl[f'carve_pairs_{t:g}px']).most_common()),
                                             'occlusion_candidates': sum(fl[f'occlusion_{t:g}px'] for fl in f)}
                                for t in SENSITIVITY_TOL_PX},
                'length_m': lengths, 'perimeter_m': r(perim, 1),
                'length_share': {c: r(lengths[c]/perim, 4) if perim else None for c in CLASSES},
                'decisions': {'all': decisions(lambda fl: True), 'frame_edge': decisions(lambda fl: fl['frame']),
                              'carve_edge': decisions(lambda fl: fl['carve']), 'spike': decisions(lambda fl: fl['spike']),
                              'occlusion_candidate': decisions(lambda fl: fl['occlusion_candidate']),
                              'canopy_chopped_corner': decisions(lambda fl: fl['canopy_chopped_corner']),
                              'none_of_these': decisions(lambda fl: not (fl['frame'] or fl['carve'] or fl['spike'] or fl['source_frame']))},
            }
    return out


# --- cross-frame bodies ------------------------------------------------------------------------------------------------

def body_index(window_frames):
    """Live buildings of every region-registered scene whose frame meets a window frame (Mercator units)."""
    area = shapely.unary_union(list(window_frames.values()))
    entries, scanned = [], 0
    for path in sorted(SCENES.glob('*/latest.json')):
        if path.parent.name.startswith(SCRATCH_PREFIX):
            continue
        scene = load_json(path)
        scanned += 1
        if not isinstance(scene, dict) or not (scene.get('region') or {}).get('bounds'):
            continue
        w, h = scene['image_width'], scene['image_height']
        origin = mercator_origin(scene)
        frame = Polygon(pixels_to_mercator([[0, 0], [w, 0], [w, h], [0, h]], scene, origin))
        if not frame.intersects(area):
            continue
        for o in scene.get('objects', []):
            if o.get('kind') != 'building' or o.get('status') in LIVE_EXCLUDED or len(o.get('ring') or []) < 3:
                continue
            ring = np.asarray(o['ring'], dtype=float)
            touches = bool((frame_distance(ring, w, h) <= FRAME_TOL_PX).any())
            try:
                geo = Polygon(pixels_to_mercator(ring, scene, origin))
            except (ValueError, TypeError):
                continue
            if not geo.is_valid:
                geo = polygonal(make_valid(geo))
            if geo is None or geo.area <= 0:
                continue
            entries.append({'scene_id': scene['id'], 'object_id': o['id'], 'geo': geo, 'touches_own_frame': touches,
                            'status': o.get('status'), 'm_per_px': spacing(scene['pixel_to_meters']),
                            'vertices': len(o['ring'])})
        del scene
    return entries, scanned


def body_search(rows, windows_meta):
    frames, objects = {}, {}
    for meta in windows_meta:
        scene = load_json(SCENES/meta['window']/'latest.json')
        w, h = scene['image_width'], scene['image_height']
        frames[meta['window']] = Polygon(pixels_to_mercator([[0, 0], [w, 0], [w, h], [0, h]], scene))
        objects[meta['window']] = {k: scene[k] for k in ('centroid', 'pixel_to_meters', 'image_width', 'image_height', 'id')}
    entries, scanned = body_index(frames)
    tree = STRtree([e['geo'] for e in entries]) if entries else None
    results = {}
    for row in rows:
        fl = flags(row)
        if row['kind'] != 'building' or not fl['frame']:
            continue
        key = (row['window'], row['id'])
        if key in results:
            continue
        scene = objects[row['window']]
        obj_ring = row['_ring']
        geo = Polygon(pixels_to_mercator(obj_ring, scene))
        if not geo.is_valid:
            geo = polygonal(make_valid(geo))
        best, same_res = None, None
        if tree is not None and geo is not None and geo.area > 0:
            for k in tree.query(geo):
                e = entries[int(k)]
                if e['scene_id'] == row['window'] or e['touches_own_frame']:
                    continue
                inter = e['geo'].intersection(geo).area
                cover = inter/geo.area
                beyond = e['geo'].difference(frames[row['window']]).area/geo.area
                if beyond < .02:
                    continue
                cand = {'scene_id': e['scene_id'], 'object_id': e['object_id'], 'cover': r(cover, 3),
                        'beyond_window_share': r(beyond, 3), 'body_to_object_area': r(e['geo'].area/geo.area, 3),
                        'body_status': e['status'], 'other_window': e['scene_id'].startswith(WINDOW_PREFIXES),
                        'body_m_per_px': r(e['m_per_px'], 4), 'resolution_ratio': r(e['m_per_px']/row['m_per_px'], 3),
                        'body_vertices': e['vertices']}
                if best is None or cover > best['cover']:
                    best = cand
                if cover >= BODY_COVER[0] and cand['resolution_ratio'] <= 1.25 and (same_res is None or cover > same_res['cover']):
                    same_res = cand
        tier = 'none' if best is None or best['cover'] < BODY_COVER[1] else 'whole' if best['cover'] >= BODY_COVER[0] else 'partial'
        # The best whole body at the window's own resolution (<= 1.25 x its metres per pixel), when one exists.
        results[key] = {'tier': tier, 'best': best, 'same_resolution_whole': same_res}
    return results, {'scenes_scanned': scanned, 'buildings_indexed': len(entries)}


# --- renders -----------------------------------------------------------------------------------------------------------

def font(size):
    for name in ('/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Helvetica.ttc'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def dashed(draw, pts, colour, width, dash=7., gap=5.):
    pts = [tuple(map(float, p)) for p in pts]
    on, left = True, dash
    for a, b in zip(pts[:-1], pts[1:], strict=True):
        ax, ay = a
        bx, by = b
        seg = math.hypot(bx-ax, by-ay)
        pos = 0.
        while pos < seg-1e-9:
            step = min(left, seg-pos)
            t0, t1 = pos/seg, (pos+step)/seg
            if on:
                draw.line([(ax+(bx-ax)*t0, ay+(by-ay)*t0), (ax+(bx-ax)*t1, ay+(by-ay)*t1)], fill=colour, width=width)
            pos += step
            left -= step
            if left <= 1e-9:
                on = not on
                left = dash if on else gap


def class_polylines(points, cls, code):
    """Consecutive samples of one class as polylines (cyclic join ignored for drawing)."""
    lines, current = [], []
    for p, c in zip(points, cls, strict=True):
        if c == code:
            current.append(p)
        elif current:
            lines.append(current)
            current = []
    if current:
        lines.append(current)
    return lines


def render_object(row, poly_by_id, kinds_by_id, image, window, out_path, title):
    rings = row['_rings']
    xs = np.concatenate([d['points'][:, 0] for d in rings])
    ys = np.concatenate([d['points'][:, 1] for d in rings])
    partner_ids = sorted({x['partner_id'] for x in row['runs']})
    bx0, by0, bx1, by1 = xs.min(), ys.min(), xs.max(), ys.max()
    pad = max(20., .2*max(bx1-bx0, by1-by0))
    W, H = window['image_width'], window['image_height']
    # The crop may reach beyond the window (grey): the frame line then shows where the outline is cut.
    margin = 12
    x0, y0 = max(-margin, int(bx0-pad)), max(-margin, int(by0-pad))
    x1, y1 = min(W+margin, int(math.ceil(bx1+pad))), min(H+margin, int(math.ceil(by1+pad)))
    scale = max(1., min(8., RENDER_LONG_SIDE/max(x1-x0, y1-y0)))
    size = (max(1, int((x1-x0)*scale)), max(1, int((y1-y0)*scale)))
    base = Image.new('RGB', (x1-x0, y1-y0), (70, 70, 70))
    ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    base.paste(image.crop((ix0, iy0, ix1, iy1)), (ix0-x0, iy0-y0))
    raw = base.resize(size, Image.LANCZOS)
    ann = raw.copy()
    draw = ImageDraw.Draw(ann)
    def tr(pts):
        return [((p[0]-x0)*scale, (p[1]-y0)*scale) for p in pts]
    for d0 in (raw, ann):
        ImageDraw.Draw(d0).line(tr([(0, 0), (W, 0), (W, H), (0, H), (0, 0)]), fill=COLOURS['window'], width=1)
    context = {'building': (240, 140, 120), 'tree': (60, 220, 90), 'vegetation': (200, 240, 60), 'linear': (110, 140, 255),
               'other': (170, 170, 170)}
    crop = box(x0, y0, x1, y1)
    for pid, p in poly_by_id.items():
        if pid == row['id'] or pid in partner_ids or p is None or not p.intersects(crop):
            continue
        for g in (p.geoms if p.geom_type == 'MultiPolygon' else [p]):
            draw.line(tr(list(g.exterior.coords)), fill=context[pair_group(kinds_by_id.get(pid, 'other'))], width=1)
    for pid in partner_ids:
        p = poly_by_id.get(pid)
        if p is None:
            continue
        for g in (p.geoms if p.geom_type == 'MultiPolygon' else [p]):
            dashed(draw, tr(list(g.exterior.coords)), COLOURS['partner'], 1, 4, 4)
    for d in rings:
        pts = list(d['points'])+[d['points'][0]]
        draw.line(tr(pts), fill=COLOURS['outline'], width=1)
        for name, w in (('carve', 4), ('shared_strong', 3), ('source_frame', 3)):
            for line in class_polylines(d['points'], d['cls'], CLASS_CODE[name]):
                if len(line) >= 2:
                    if name == 'source_frame':
                        dashed(draw, tr(line), COLOURS[name], w)
                    else:
                        draw.line(tr(line), fill=COLOURS[name], width=w)
        for line in class_polylines(d['points'], d['cls'], CLASS_CODE['frame']):
            if len(line) >= 2:
                dashed(draw, tr(line), COLOURS['frame'], 4)
    for c in row.get('chamfer_corners', []):
        if c['canopy_chopped']:
            cx, cy = tr([c['at_px']])[0]
            draw.rectangle([cx-6, cy-6, cx+6, cy+6], outline=(0, 255, 120), width=3)
    for sp in row['spikes']:
        cx, cy = tr([sp['at_px']])[0]
        rad = max(10., (sp['width_m'] or 0)/row['m_per_px']*scale*.8+8)
        draw.ellipse([cx-rad, cy-rad, cx+rad, cy+rad], outline=COLOURS['spike'], width=3)
    gap = 8
    banner = 58
    canvas = Image.new('RGB', (size[0]*2+gap, size[1]+banner), COLOURS['box'])
    canvas.paste(raw, (0, banner))
    canvas.paste(ann, (size[0]+gap, banner))
    d = ImageDraw.Draw(canvas)
    f = font(13)
    L = row['lengths_m']
    d.text((4, 3), title, fill=COLOURS['text'], font=f)
    d.text((4, 21), f"{row['kind']} {row['layer']} status={row['status']} decision={row['decision']}  area {row['area_m2']} m2  "
                    f"perimeter {row['perimeter_m']} m", fill=COLOURS['text'], font=f)
    d.text((4, 39), f"frame {L['frame']} m (red dashed)  carve {L['carve']} m (magenta; partner dashed pink)  shared-strong "
                    f"{L['shared_strong']} m (cyan)  spikes {len(row['spikes'])} (yellow)  src-frame {L['source_frame']} m",
           fill=COLOURS['text'], font=f)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path


def render_named_bodies(rows, bodies, out):
    """For each named object with a cross-frame body: the body's own image with the body (green), the window object projected
    into it (white) and the window frame projected (red dashed). Georeferenced projection; no residual measured."""
    paths = []
    for w, oid in NAMED:
        b = bodies.get((w, oid))
        row = next((x for x in rows if x['window'] == w and x['id'] == oid and x['layer'] == 'review'), None)
        if not b or not b.get('best') or row is None:
            continue
        window = load_json(SCENES/w/'latest.json')
        sid = b['best']['scene_id']
        scene = load_json(SCENES/sid/'latest.json')
        body = next(o for o in scene['objects'] if o['id'] == b['best']['object_id'])
        image = Image.open(SCENES/sid/f'{scene["image_sha256"]}.png').convert('RGB')
        obj_px = mercator_to_pixels(pixels_to_mercator(row['_ring'], window), scene)
        ww, wh = window['image_width'], window['image_height']
        frame_px = mercator_to_pixels(pixels_to_mercator([[0, 0], [ww, 0], [ww, wh], [0, wh], [0, 0]], window), scene)
        pts = np.vstack([np.asarray(body['ring'], dtype=float), obj_px])
        x0, y0 = np.floor(pts.min(axis=0)-30).astype(int)
        x1, y1 = np.ceil(pts.max(axis=0)+30).astype(int)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(scene['image_width'], x1), min(scene['image_height'], y1)
        scale = max(1., min(6., RENDER_LONG_SIDE/max(x1-x0, y1-y0)))
        size = (int((x1-x0)*scale), int((y1-y0)*scale))
        raw = image.crop((x0, y0, x1, y1)).resize(size, Image.LANCZOS)
        ann = raw.copy()
        draw = ImageDraw.Draw(ann)
        def tr(p, x0=x0, y0=y0, scale=scale):
            return [((q[0]-x0)*scale, (q[1]-y0)*scale) for q in p]
        dashed(draw, tr(frame_px), COLOURS['frame'], 3)
        ring = [tuple(q) for q in body['ring']]
        draw.line(tr(ring+[ring[0]]), fill=(40, 255, 60), width=3)
        o = [tuple(q) for q in obj_px]
        draw.line(tr(o+[o[0]]), fill=COLOURS['outline'], width=2)
        canvas = Image.new('RGB', (size[0]*2+8, size[1]+40), COLOURS['box'])
        canvas.paste(raw, (0, 40))
        canvas.paste(ann, (size[0]+8, 40))
        d = ImageDraw.Draw(canvas)
        d.text((4, 3), f'body for {w} {oid}: {sid} {body["id"]} (green; cover {b["best"]["cover"]}, {b["best"]["body_to_object_area"]}x area)',
               fill=COLOURS['text'], font=font(13))
        d.text((4, 21), 'white = the window object projected; red dashed = the window frame projected (georeferenced, unverified)',
               fill=COLOURS['text'], font=font(13))
        path = out/'renders'/f'body.{w}.{oid}.png'
        canvas.save(path)
        paths.append(path)
    return paths


def contact_sheet(paths, out, cell=440, columns=4):
    if not paths:
        return None
    tiles = []
    for p in paths:
        im = Image.open(p)
        im.thumbnail((cell*2, cell))
        tiles.append(im)
    w = max(t.width for t in tiles)
    h = max(t.height for t in tiles)
    rows = math.ceil(len(tiles)/columns)
    sheet = Image.new('RGB', (w*columns, h*rows), (20, 20, 20))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % columns)*w, (i//columns)*h))
    sheet.save(out)
    return out


def pick_renders(rows, extra=20):
    """The named objects plus `extra` across categories (review layer first, distinct windows preferred, deterministic)."""
    chosen, seen_windows = [], Counter()
    named = [(w, oid) for w, oid in NAMED]
    for w, oid in named:
        for x in rows:
            if x['window'] == w and x['id'] == oid and x['layer'] == 'review':
                chosen.append((x, 'named'))
    taken = {(x['window'], x['layer'], x['id']) for x, _ in chosen}
    def add(category, pool, n, key):
        pool = sorted(pool, key=key)
        picked = 0
        for x in sorted(pool, key=lambda x: seen_windows[x['window']]):
            if picked >= n:
                break
            k = (x['window'], x['layer'], x['id'])
            if k in taken:
                continue
            taken.add(k)
            seen_windows[x['window']] += 1
            chosen.append((x, category))
            picked += 1
    review = [x for x in rows if x['layer'] == 'review']
    # Every other object the reviewer marked needs_adjustment that is cut by the window frame (not a carried copy).
    add('reviewer_flagged_frame_cut', [x for x in review if x['decision'] == 'needs_adjustment' and flags(x)['frame']
                                       and not x['carried_role']], 20, lambda x: (x['window'], x['id']))
    def carve_len(x, kinds):
        return sum(run['length_m'] for run in x['runs'] if run['class'] == 'carve' and pair_group(run['partner_kind']) in kinds)
    add('building_frame', [x for x in review if x['kind'] == 'building' and flags(x)['frame']], 3, lambda x: -x['lengths_m']['frame'])
    add('tree_frame', [x for x in review if x['kind'] == 'tree' and flags(x)['frame']], 2, lambda x: -x['lengths_m']['frame'])
    add('building_carved_by_tree', [x for x in review if x['kind'] == 'building' and 'tree' in flags(x)['carve_pairs']], 3,
        lambda x: -carve_len(x, ('tree',)))
    add('tree_carved_by_building', [x for x in review if x['kind'] == 'tree' and 'building' in flags(x)['carve_pairs']], 3,
        lambda x: -carve_len(x, ('building',)))
    add('building_carved_by_building', [x for x in review if x['kind'] == 'building' and 'building' in flags(x)['carve_pairs']], 2,
        lambda x: -carve_len(x, ('building',)))
    add('vegetation_carve', [x for x in review if x['kind'] == 'vegetation' and flags(x)['carve']], 2,
        lambda x: -carve_len(x, ('building', 'tree', 'vegetation', 'linear', 'other')))
    for kind in KINDS:
        add('spike', [x for x in review if flags(x)['spike'] and x['kind'] == kind], 1,
            lambda x: -max(sp['depth_m'] or 0 for sp in x['spikes']))
    add('canopy_chopped_corner', [x for x in review if flags(x)['canopy_chopped_corner']], 2,
        lambda x: -max(c['triangle_m2'] for c in x['chamfer_corners'] if c['canopy_chopped']))
    add('occlusion_candidate', [x for x in review if flags(x)['occlusion_candidate']], 2,
        lambda x: -sum(run['length_m'] for run in x['runs'] if run['class'] == 'shared_strong' and run['partner_kind'] == 'tree'))
    return chosen


# --- report ------------------------------------------------------------------------------------------------------------

def named_summary(rows, bodies):
    out = []
    for w, oid in NAMED:
        for x in rows:
            if x['window'] == w and x['id'] == oid:
                fl = flags(x)
                out.append({'window': w, 'id': oid, 'layer': x['layer'], 'kind': x['kind'], 'status': x['status'],
                            'decision': x['decision'], 'area_m2': x['area_m2'], 'lengths_m': x['lengths_m'],
                            'perimeter_m': x['perimeter_m'], 'frame_sides': x['frame_sides'], 'spikes': x['spikes'],
                            'runs': x['runs'], 'sensitivity': {k: {kk: vv for kk, vv in v.items() if kk != 'runs'} | {
                                'runs': v['runs']} for k, v in x['sensitivity'].items()},
                            'flags': fl, 'construction_reason': x['construction_reason'], 'chamfer_corners': x.get('chamfer_corners'),
                            'body': bodies.get((w, oid))})
    return out


def table(agg, key, layer):
    return ' | '.join(str(agg[layer][k][key]) for k in KINDS)


def write_md(path, header, agg, named, body_summary, render_rows):
    lines = [f'# Object-cut census ({VERSION})', '',
             f'Generated {header["generated_at"]} by `scripts/ml/experiments/cut_census.py` (sha256 `{CODE_SHA256[:16]}…`). '
             'Read-only over data/scenes; no inference. Definitions in the module docstring and census.json `definitions`.', '',
             f'Population: {header["windows"]} windows ({header["window_sizes"]}); classified objects (live building / tree / '
             f'vegetation): model {header["objects"]["model"]}, review {header["objects"]["review"]}.', '',
             '## Counts by layer and kind (objects)', '']
    for layer in ('review', 'model'):
        lines += [f'### {layer} layer', '', '| | building | tree | vegetation |', '|---|---|---|---|']
        for key, label in (('objects', 'live objects'), ('with_frame_edge', 'with a frame edge (>= 0.5 m on the frame)'),
                           ('frame_touch_only', 'touching the frame < 0.5 m'), ('with_source_frame_edge', 'with a carried source-frame edge'),
                           ('with_spike', 'with a frame spike'), ('spikes', 'spikes'),
                           ('spikes_deeper_than_0_5m', 'spikes deeper than 0.5 m'),
                           ('shallow_dents', 'shallow frame dents (not spikes)'),
                           ('with_carve_edge', 'with a carve edge (1 px, >= 1.5 m, weak step)'),
                           ('with_shared_strong_edge', 'with a shared strong-step edge (1 px)'),
                           ('occlusion_candidates', 'buildings sharing a strong edge with a tree (occlusion candidates)'),
                           ('with_canopy_chopped_corner', 'buildings with a canopy-chopped corner (55ca pattern)'),
                           ('canopy_chopped_with_frame_edge', '... of which also frame-cut'),
                           ('chamfer_corners_all', 'chamfer corners found (any)')):
            lines.append(f'| {label} | {table(agg, key, layer)} |')
        lines.append('| carve partners (objects per partner group) | ' + ' | '.join(
            ', '.join(f'{k} {v}' for k, v in agg[layer][kd]['carve_partner_groups'].items()) or '-' for kd in KINDS) + ' |')
        lines.append('| shared-strong partners | ' + ' | '.join(
            ', '.join(f'{k} {v}' for k, v in agg[layer][kd]['shared_strong_partner_groups'].items()) or '-' for kd in KINDS) + ' |')
        lines.append('| with carve edge at 2 / 3 / 4 px | ' + ' | '.join(
            ' / '.join(str(agg[layer][kd]['sensitivity'][f'{t:g}px']['with_carve']) for t in SENSITIVITY_TOL_PX) for kd in KINDS) + ' |')
        lines.append('| carve partners at 4 px | ' + ' | '.join(
            ', '.join(f'{k} {v}' for k, v in agg[layer][kd]['sensitivity']['4px']['carve_partner_groups'].items()) or '-' for kd in KINDS) + ' |')
        lines.append('| occlusion candidates at 2 / 3 / 4 px | ' + ' | '.join(
            ' / '.join(str(agg[layer][kd]['sensitivity'][f'{t:g}px']['occlusion_candidates']) for t in SENSITIVITY_TOL_PX) for kd in KINDS) + ' |')
        lines.append('| boundary length share frame / carve / shared-strong / spike | ' + ' | '.join(
            ' / '.join(f"{(agg[layer][kd]['length_share'][c] or 0)*100:.1f}%" for c in ('frame', 'carve', 'shared_strong', 'spike'))
            for kd in KINDS) + ' |')
        lines.append('')
        lines += [f'Reviewer decisions ({layer} layer; the review.json status of the same id):', '']
        for kd in KINDS:
            dec = agg[layer][kd]['decisions']
            lines.append(f'- {kd}: frame edge {dec["frame_edge"]}; carve {dec["carve_edge"]}; spike {dec["spike"]}; '
                         f'occlusion candidate {dec["occlusion_candidate"]}; canopy-chopped corner {dec["canopy_chopped_corner"]}; '
                         f'none of these {dec["none_of_these"]}')
        lines.append('')
    lines += ['## The two named objects (win-3dd37155248c04ef)', '']
    for n in named:
        L = n['lengths_m']
        lines.append(f"- {n['id']} ({n['layer']}): {n['kind']} {n['area_m2']} m2, status {n['status']}, decision {n['decision']}; "
                     f"perimeter {n['perimeter_m']} m = frame {L['frame']} ({', '.join(n['frame_sides'])}) + spike {L['spike']} + "
                     f"carve {L['carve']} + shared-strong {L['shared_strong']} + imagery {L['imagery']} m; spikes "
                     f"{[(s['shape'], s['width_m'], s['depth_m'], s['at_px']) for s in n['spikes']]}; 1 px runs "
                     f"{[(x['partner_id'][:8], x['partner_kind'], x['length_m'], x['median_step_rgb'], x['class']) for x in n['runs']]}; "
                     f"4 px runs {[(x['partner_id'][:8], x['partner_kind'], x['length_m'], x['median_step_rgb'], x['class']) for x in n['sensitivity']['4px']['runs']]}; "
                     f"chamfer corners {n['chamfer_corners']}; canopy-eaves {n['construction_reason']}; cross-frame body {n['body']}")
    lines += ['', '## Cross-frame bodies for frame-cut buildings (georeferenced, unverified)', '',
              f"Scenes scanned {body_summary.get('scenes_scanned')}, buildings indexed {body_summary.get('buildings_indexed')}.", '']
    for layer, tiers in body_summary.get('tiers', {}).items():
        lines.append(f'- {layer}: {tiers}')
    lines += ['', '## Renders', '']
    for x, cat, p in render_rows:
        lines.append(f"- [{cat}] {x['window']} {x['id']} ({x['layer']}, {x['kind']}, decision {x['decision']}): `{rel(p, path.parent)}`")
    lines.append('')
    path.write_text('\n'.join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--skip-renders', action='store_true')
    parser.add_argument('--skip-bodies', action='store_true')
    parser.add_argument('--windows', nargs='*')
    parser.add_argument('--out', type=Path, default=OUT)
    args = parser.parse_args(argv)
    warnings.filterwarnings('ignore', category=RuntimeWarning)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    heartbeat(f'{VERSION} start')
    windows = [p for p in population() if not args.windows or p.name in args.windows]
    rows, metas = [], []
    for i, path in enumerate(windows):
        window_rows, meta = analyse_window(path)
        latest = load_json(path/'latest.json')
        review = load_json(path/'review.json') if (path/'review.json').exists() else {}
        rings = {('model', o['id']): o['ring'] for o in latest.get('objects', [])}
        rings.update({('review', o['id']): o['ring'] for o in review.get('objects', [])})
        for x in window_rows:
            x['_ring'] = rings[(x['layer'], x['id'])]
        rows.extend(window_rows)
        metas.append(meta)
        heartbeat(f'window {i+1}/{len(windows)} {path.name} objects {len(window_rows)} ({time.time()-started:.0f}s)')
    agg = aggregate(rows)
    bodies, body_meta = {}, {}
    if not args.skip_bodies:
        heartbeat('body index start')
        bodies, body_meta = body_search(rows, metas)
        tiers = defaultdict(Counter)
        for x in rows:
            b = bodies.get((x['window'], x['id']))
            if b is not None and x['kind'] == 'building' and flags(x)['frame']:
                tiers[x['layer']][b['tier']] += 1
                tiers[f"{x['layer']}:{x['decision']}"][b['tier']] += 1
                tiers[f"{x['layer']}:whole_at_window_resolution"]['yes' if b['same_resolution_whole'] else 'no'] += 1
                if b['tier'] == 'whole':
                    tiers[f"{x['layer']}:whole_source"]['other_window' if b['best']['other_window'] else 'other_frame'] += 1
        body_meta['tiers'] = {k: dict(v) for k, v in sorted(tiers.items())}
        heartbeat(f'body search done {body_meta.get("buildings_indexed")} indexed')
    for x in rows:
        b = bodies.get((x['window'], x['id']))
        x['cross_frame_body'] = b if (b and x['kind'] == 'building' and flags(x)['frame']) else None
        x['flags'] = flags(x)
    render_rows = []
    if not args.skip_renders:
        chosen = pick_renders(rows)
        by_window = defaultdict(list)
        for x, cat in chosen:
            by_window[x['window']].append((x, cat))
        for w, items in sorted(by_window.items()):
            details, image, latest = analyse_window(SCENES/w, detail_ids={(x['layer'], x['id']) for x, _ in items})
            for x, cat in items:
                row, poly_by_id, kinds_by_id = details[(x['layer'], x['id'])]
                p = out/'renders'/f'{cat}.{w}.{x["id"]}.{x["layer"]}.png'
                render_object(row, poly_by_id, kinds_by_id, image, latest, p, f'[{cat}] {w} {x["id"]} ({x["layer"]})')
                render_rows.append((x, cat, p))
        contact_sheet([p for _, _, p in render_rows], out/'renders'/'contact-sheet.png')
        if bodies:
            for p in render_named_bodies(rows, bodies, out):
                render_rows.append((next(x for x in rows if p.name.endswith(f"{x['id']}.png") and x['layer'] == 'review'), 'named_body', p))
        heartbeat(f'renders {len(render_rows)}')
    named = named_summary(rows, bodies)
    header = {'version': VERSION, 'code_sha256': CODE_SHA256, 'generated_at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'windows': len(windows), 'window_sizes': dict(Counter(f'{m["image"][0]}x{m["image"][1]}' for m in metas)),
              'objects': dict(Counter(x['layer'] for x in rows)), 'runtime_s': round(time.time()-started, 1)}
    definitions = {'frame_tol_px': FRAME_TOL_PX, 'partner_tol_px': PARTNER_TOL_PX, 'sensitivity_tol_px': SENSITIVITY_TOL_PX,
                   'carve_min_m': CARVE_MIN_M, 'has_frame_m': HAS_FRAME_M, 'spike_max_width_m': SPIKE_MAX_WIDTH_M,
                   'spike_area_share': SPIKE_AREA_SHARE, 'spike_depths_m': SPIKE_DEPTHS_M, 'step_rgb': STEP_RGB,
                   'step_source': 'scripts/ml/roof_boundary_guidance.py LIMITS minimum_step_rgb and its contrast line',
                   'sample_px': SAMPLE_PX, 'bridge_px': BRIDGE_PX, 'body_cover': BODY_COVER, 'classes': CLASSES,
                   'live_excluded': LIVE_EXCLUDED, 'kinds': KINDS}
    census = {**header, 'definitions': definitions, 'inputs': metas, 'aggregate': agg, 'named': named,
              'cross_frame_bodies': body_meta,
              'renders': [{'category': cat, 'window': x['window'], 'id': x['id'], 'layer': x['layer'], 'kind': x['kind'],
                           'decision': x['decision'], 'path': rel(p, ROOT)} for x, cat, p in render_rows]}
    (out/'census.json').write_text(json.dumps(census, indent=1))
    with open(out/'objects.jsonl', 'w') as handle:
        for x in rows:
            handle.write(json.dumps({k: v for k, v in x.items() if not k.startswith('_')})+'\n')
    write_md(out/'census.md', header, agg, named, body_meta, render_rows)
    heartbeat(f'{VERSION} done {time.time()-started:.0f}s rows {len(rows)}')
    print(json.dumps({'windows': len(windows), 'rows': len(rows), 'renders': len(render_rows), 'runtime_s': header['runtime_s']}))


if __name__ == '__main__':
    main()
