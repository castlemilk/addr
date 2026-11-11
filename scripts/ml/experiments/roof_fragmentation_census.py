"""Roof-fragmentation, vertex-load and default-view census (2026-10-06). READ-ONLY over data/scenes.

Reviewer observations (verbatim, 2026-10-06 evening, six editor links):
  (1) win-088ce9350360b877 cb0c275d8d047eee "need to get better at estimating an object and not doing too many points"
  (2) win-36e1a99bfe889daf 1ddf5a8ce600afc8 "isn't a seperate surfzce comparte to the other boundary outline"
  (3) win-69468b2b783cd964 d86ebd7f0565eeac "was initially cutoff in the default zoom for the lot size"
  (4) win-6dbb766c398af77e 133736ada6ace260 "has a bad cutoff and looks just a backyard concrete area"
  (5) win-b6901e4edcbc5495 afd10322539fe730 "needs to better handle that this is all part of the one roof surface with a tree
      over it and other random objects"
  (6) win-fa2e955f73294188 23f916def2008f85 "why so many polygons and miss the proper roof outline"

This is a census, not an experiment: nothing is re-inferred, no model is loaded, nothing under data/scenes, the registry,
RULES / WINDOW / DESIGN or the eyes index is written, and every figure is a count or a geometry (never accuracy). Nothing is
promoted. It imports shapely, numpy, PIL and json only (nothing from scene_extract, which pulls torch / onnx).

How a building piece is produced today (read from scripts/ml/scene_extract.py extract_objects and scripts/ml/segmentation.py
mask_to_polygons, not changed here): each 'building roof' SAM3 proposal mask (score >= .45, up to 80) is vectorised with
mask_to_polygons(min_area_px = max(12, 1 m2), simplify_px = .9) and EVERY connected component of one mask becomes its own
object (same score, same prompt); a ring over 150 vertices (or a hole over 200) is simplified at 2 px; a ring still over 200
vertices (or 32 holes / 2000 points) is dropped; the duplicate rule drops a candidate whose IoU with a retained building
exceeds .8 or whose area is > .95 inside it (other kinds: intersection > .8 of the smaller area). On a window the takeover
rule (WINDOW.json 2026-10-01) supersedes a carried slice only when ONE window detection covers >= .8 of it and no other
covers >= .5; a slice covered in the .5-.8 band stays a separate object beside the detection.

The editor's default view (read from src/components/scenes/SceneCanvas.tsx and src/app/scenes/page.tsx, not changed here):
page.tsx selects the URL's `object` (else model.objects[0]); SceneCanvas starts `focused` (mode !== "draw") and frames
objectImageFrame([current.ring, original.ring], image, viewport): the selected object's bounding box, widened to
max(extent + 24 px, min(image, max(55 px, extent) * 1.6)) per axis and then to the viewport's aspect, centred on the object
(src/lib/scene-image-frame.ts). "Fit parcel" (parcel bbox +- 12 px) and "Whole scene" are clicks, never the default. So the
default view is fitted to the ASKED object, not to the window, the lot or the house; what it clips is every OTHER object.

Run from the repo root:
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.roof_fragmentation_census [--skip-renders]
Outputs ONLY under data/reviews/eyes/experiments/roof-fragmentation-2026-10-06/: census.json, census.md, clusters.jsonl,
renders/*.png, renders/contact-sheet.png.

Definitions (metres through the scene's pixel_to_meters affine unless marked px):
  piece            one live object (status not rejected / superseded) of kind building. Model layer = latest.json objects;
                   review layer = review.json objects (what the editor shows as layer=edited).
  touching pair    two building pieces with metric gap <= 0.5 m (overlap counts as gap 0).
  cover            intersection area / the smaller piece's area; band: <0.05 touching only, 0.05-0.5, 0.5-0.8 (under the
                   takeover floor), >= 0.8 (would be a dedupe duplicate at extraction; survives only through lineage).
  same mask        the two pieces share (prompt, score to 4 dp): components of one SAM3 proposal mask.
  occluder between a live tree / vegetation / solar_panel / car object of the same layer covering >= min(0.5 m2, 25 %) of the
                   pair's bridge = convex hull(a u b) - a - b. occluder over: one covering >= 1 m2 of a u b.
  cluster          a connected component of touching pairs (gap <= 0.5 m); the sensitivity row reports components of
                   unary_union(piece.buffer(0.5 m)) (gap <= 1.0 m) as well. Cluster area = the union of its pieces; the hull
                   is the convex hull of that union.
  confirmed beside undecided  a cluster with >= 1 confirmed piece and >= 1 candidate / needs_adjustment piece.
  vertices         exterior ring vertices as stored (hole vertices counted separately); v/m = vertices / metric perimeter.
  DP t px          shapely simplify(t, preserve_topology=True) of the stored pixel polygon; area change = (after - before) /
                   before; symmetric difference / before; Hausdorff distance in px.
  asked object     a live review object carrying carried_from, or the live replacement_ids of a superseded carried copy
                   (takeover / body carry); the objects the reviewer is asked to decide on a window.
  default view     objectImageFrame of the asked object at viewport aspect 1.0 (strict: the 264 x 264 fallback and a square
                   pane) and 1.6 (a wide pane); an object is inside when its bounding box lies within the frame, clipped
                   when it crosses the frame edge, outside otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely import make_valid
from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

VERSION = 'roof-fragmentation-census-v1'
ROOT = Path(__file__).resolve().parents[3]
CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
OUT = ROOT/'data/reviews/eyes/experiments/roof-fragmentation-2026-10-06'
RENDERS = OUT/'renders'
SCENES = ROOT/'data/scenes'
PARCEL_DIRS = (ROOT/'data/scene-parcels', ROOT/'data/reviews/eyes/parcels')   # frozen Vicmap parcel caches (key, sha256, data)
READINESS = ROOT/'data/reviews/eyes/recapture/window-readiness-2026-10-06-after-carries.json'
WINDOW_PREFIXES = ('win-', 'lot-')
REVIEW_SET_PREFIXES = ('house-', 'roof_training_250-')
LIVE_EXCLUDED = ('rejected', 'superseded')
DECIDED = ('confirmed', 'rejected', 'needs_adjustment')
UNDECIDED = ('candidate', 'needs_adjustment')
OCCLUDER_KINDS = ('tree', 'vegetation', 'solar_panel', 'car')
VERTEX_KINDS = ('building', 'tree', 'road', 'driveway', 'footpath', 'vegetation', 'car', 'solar_panel', 'pool', 'water')
TOUCH_M = 0.5
SENSITIVITY_BUFFER_M = 0.5          # unary_union(piece.buffer(0.5)) joins gaps up to 1.0 m
BAND = (0.5, 0.8)
COVER_TOUCH_ONLY = 0.05
OCCLUDER_BETWEEN = (0.5, 0.25)      # m2 floor, share of the bridge
OCCLUDER_OVER_M2 = 1.0
SIMPLIFY_PX = (1.0, 2.0, 3.0)
SMALL_PIECE_M2 = 12.0               # analysis band only (the extraction floor BUILDING_FLOOR_M2 is 1 m2): a sub-12 m2 building piece
SUBSTANTIAL_M2 = 20.0               # two pieces >= this that merely touch read as separate houses (party walls / terrace rows)
DEDUPE = {'iou': .8, 'containment': .95}   # scene_extract building duplicate rule: iou > .8 or intersection / candidate area > .95
VERTEX_MARKS = (50, 100, 150, 200)
EDITOR_FIT = {'pad_px': 24, 'factor': 1.6, 'min_px': 55, 'aspects': (1.0, 1.6), 'parcel_pad_px': 12,
              'source': 'src/lib/scene-image-frame.ts objectImageFrame; src/components/scenes/SceneCanvas.tsx frame'}
FRAME_TOL_PX = 1.0
WORLD_SPAN_M = 40075016.68557849    # scripts/ml/tiles.py
WORST_CLUSTERS = 12
RENDER_SCALE = 3
NAMED = (
    ('win-088ce9350360b877', 'cb0c275d8d047eee', 'need to get better at estimating an object and not doing too many points'),
    ('win-36e1a99bfe889daf', '1ddf5a8ce600afc8', "isn't a seperate surfzce comparte to the other boundary outline"),
    ('win-69468b2b783cd964', 'd86ebd7f0565eeac', 'was initially cutoff in the default zoom for the lot size'),
    ('win-6dbb766c398af77e', '133736ada6ace260', 'has a bad cutoff and looks just a backyard concrete area'),
    ('win-b6901e4edcbc5495', 'afd10322539fe730', 'needs to better handle that this is all part of the one roof surface with a tree over it and other random objects'),
    ('win-fa2e955f73294188', '23f916def2008f85', 'why so many polygons and miss the proper roof outline'),
)
PALETTE = [(230, 25, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180), (255, 225, 25), (70, 240, 240), (240, 50, 230),
           (210, 245, 60), (250, 190, 212), (0, 128, 128), (220, 190, 255), (170, 110, 40), (255, 250, 200), (128, 0, 0),
           (128, 128, 0), (255, 215, 180), (0, 0, 128), (128, 128, 128)]
OCCLUDER_GREEN = (40, 200, 60)


# --- loading -----------------------------------------------------------------------------------------------------------

def load_json(path):
    with open(path) as handle:
        return json.load(handle)


def population(root=SCENES):
    """(scene_dir, group) for the 54 windows and every review-set scene with a review.json; sorted, read-only."""
    rows = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not (path/'latest.json').exists():
            continue
        if path.name.startswith(WINDOW_PREFIXES):
            rows.append((path, 'windows'))
        elif path.name.startswith(REVIEW_SET_PREFIXES) and (path/'review.json').exists():
            rows.append((path, 'review_set'))
    return rows


def affine_params(pixel_to_meters):
    m = np.asarray(pixel_to_meters, dtype=float)      # 3x2: [x y 1] @ m = [X Y]
    return [m[0, 0], m[1, 0], m[0, 1], m[1, 1], m[2, 0], m[2, 1]]


def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type == 'Polygon':
        return geometry
    parts = [g for g in getattr(geometry, 'geoms', []) if g.geom_type in ('Polygon', 'MultiPolygon') and not g.is_empty]
    if not parts:
        return None
    merged = unary_union(parts)
    return merged if not merged.is_empty else None


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


def percentile(values, q):
    values = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return round(float(np.percentile(values, q)), 3) if values else None


def median(values):
    return percentile(values, 50)


def share(count, total):
    return round(count/total, 4) if total else None


def frame_distance_px(polygon, width, height):
    minx, miny, maxx, maxy = polygon.bounds
    return round(float(min(minx, miny, width-maxx, height-maxy)), 1)


def exterior_vertices(polygon):
    if polygon.geom_type == 'Polygon':
        return len(polygon.exterior.coords)-1, sum(len(h.coords)-1 for h in polygon.interiors)
    return (sum(len(p.exterior.coords)-1 for p in polygon.geoms),
            sum(len(h.coords)-1 for p in polygon.geoms for h in p.interiors))


def layer_rows(scene, objects, layer):
    """Per-object rows of one layer: geometry in px and m, identity, status, lineage. Superseded objects are skipped."""
    params = affine_params(scene['pixel_to_meters'])
    width, height = scene['image_width'], scene['image_height']
    rows, invalid = [], Counter()
    for obj in objects:
        if obj.get('status') == 'superseded':
            continue
        pixel = object_polygon(obj)
        if pixel is None:
            invalid[obj.get('kind')] += 1
            continue
        metric = polygonal(make_valid(affine_transform(pixel, params)))
        if metric is None:
            invalid[obj.get('kind')] += 1
            continue
        vertices, hole_vertices = len(obj['ring']), sum(len(h) for h in (obj.get('holes') or []))
        rows.append({'scene': scene['id'], 'layer': layer, 'id': obj['id'], 'kind': obj.get('kind'), 'status': obj.get('status'),
                     'live': obj.get('status') not in LIVE_EXCLUDED, 'source': obj.get('source'), 'score': obj.get('score'),
                     'prompt': obj.get('prompt'), 'carried': bool(obj.get('carried_from')),
                     'carried_role': (obj.get('carried_from') or {}).get('role') if isinstance(obj.get('carried_from'), dict) else None,
                     'replaces': len(obj.get('replaces') or []), 'geometry_reviewed': bool(obj.get('geometry_reviewed')),
                     'area_m2': round(metric.area, 3), 'perimeter_m': round(metric.length, 3),
                     'vertices': vertices, 'hole_vertices': hole_vertices, 'holes': len(obj.get('holes') or []),
                     'frame_distance_px': frame_distance_px(pixel, width, height),
                     '_pixel': pixel, '_metric': metric, '_ring': obj['ring']})
    return rows, invalid


# --- (A) fragmentation ----------------------------------------------------------------------------------------------------

def cover_band(cover):
    if cover < COVER_TOUCH_ONLY:
        return 'touch_only'
    if cover < BAND[0]:
        return 'overlap_lt_0.5'
    if cover < BAND[1]:
        return 'band_0.5_0.8'
    return 'ge_0.8'


def building_pairs(buildings, occluders):
    """Every touching pair (gap <= TOUCH_M) of live building pieces with its cover band, mask identity and occluders."""
    tree = STRtree([o['_metric'] for o in occluders]) if occluders else None
    pairs = []
    for i, a in enumerate(buildings):
        for b in buildings[i+1:]:
            gap = a['_metric'].distance(b['_metric'])
            if gap > TOUCH_M:
                continue
            smaller = min(a['area_m2'], b['area_m2'])
            inter = a['_metric'].intersection(b['_metric']).area
            cover = inter/smaller if smaller > 0 else 0.
            union = unary_union([a['_metric'], b['_metric']])
            bridge = union.convex_hull.difference(union)
            between, over = [], []
            if tree is not None:
                for idx in tree.query(union.convex_hull):
                    o = occluders[idx]
                    if bridge.area > 0:
                        hit = o['_metric'].intersection(bridge).area
                        if hit >= min(OCCLUDER_BETWEEN[0], OCCLUDER_BETWEEN[1]*bridge.area) and hit > 0:
                            between.append(o)
                    if o['_metric'].intersection(union).area >= OCCLUDER_OVER_M2:
                        over.append(o)
            same_mask = (a['prompt'] == b['prompt'] and a['score'] is not None and a['score'] == b['score']
                         and a['source'] == b['source'] == 'sam3-text-onnx')
            iou = inter/union.area if union.area else 0.
            pairs.append({'a': a['id'], 'b': b['id'], 'gap_m': round(float(gap), 3), 'cover_of_smaller': round(float(cover), 4),
                          'iou': round(float(iou), 4), 'smaller_m2': round(float(smaller), 2),
                          'small_piece': bool(smaller < SMALL_PIECE_M2),
                          'both_substantial': bool(min(a['area_m2'], b['area_m2']) >= SUBSTANTIAL_M2),
                          'dedupe_gap': bool(cover >= BAND[1] and iou <= DEDUPE['iou'] and cover < DEDUPE['containment']),
                          'band': cover_band(cover), 'same_mask': bool(same_mask),
                          'carried': bool(a['carried'] or b['carried']),
                          'bridge_m2': round(float(bridge.area), 3),
                          'occluders_between': sorted({o['kind'] for o in between}),
                          'occluders_over': sorted({o['kind'] for o in over}),
                          '_between_ids': [o['id'] for o in between], '_over_ids': [o['id'] for o in over]})
    return pairs


def components(ids, edges):
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    groups = defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    return [sorted(g) for g in groups.values()]


def party_wall(pair):
    """Two substantial pieces (>= 20 m2 each) that only touch (cover < .05) and are not one mask: adjoining houses, not fragments."""
    return pair['both_substantial'] and pair['band'] == 'touch_only' and not pair['same_mask'] and not pair['occluders_between']


def fragmentation_pair(pair):
    return not party_wall(pair)


def pair_category(pair):
    """One label per touching pair, in precedence order: same_mask > occluded > band > carried_slice > overlap > touch."""
    if pair['same_mask']:
        return 'same_mask'
    if pair['occluders_between']:
        return 'occluder_between'
    if pair['band'] == 'band_0.5_0.8':
        return 'band_0.5_0.8'
    if pair['band'] == 'ge_0.8':
        return 'ge_0.8'
    if pair['carried']:
        return 'carried_slice'
    if pair['band'] == 'overlap_lt_0.5':
        return 'overlap_lt_0.5'
    return 'touch_only'


def building_clusters(buildings, pairs, scene_id, layer, group):
    by_id = {b['id']: b for b in buildings}
    edges = [(p['a'], p['b']) for p in pairs]
    clusters = []
    for ids in components([b['id'] for b in buildings], edges):
        members = [by_id[i] for i in ids]
        union = unary_union([m['_metric'] for m in members])
        largest = max(members, key=lambda m: m['area_m2'])
        inner = [p for p in pairs if p['a'] in ids and p['b'] in ids]
        statuses = Counter(m['status'] for m in members)
        categories = Counter(pair_category(p) for p in inner)
        occluder_ids = sorted({i for p in inner for i in p['_between_ids']+p['_over_ids']})
        clusters.append({'scene': scene_id, 'layer': layer, 'group': group, 'ids': ids, 'pieces': len(ids),
                         'area_m2': round(float(union.area), 2), 'largest_m2': round(largest['area_m2'], 2),
                         'largest_id': largest['id'], 'largest_share': round(largest['area_m2']/union.area, 4) if union.area else None,
                         'hull_m2': round(float(union.convex_hull.area), 2),
                         'fill_of_hull': round(float(union.area/union.convex_hull.area), 4) if union.convex_hull.area else None,
                         'statuses': dict(statuses), 'pairs': len(inner), 'pair_categories': dict(categories),
                         'confirmed_beside_undecided': bool(statuses.get('confirmed') and any(statuses.get(s) for s in UNDECIDED)),
                         'carried_pieces': sum(1 for m in members if m['carried']),
                         'fragment_pieces': sum(1 for m in members if m['area_m2'] < SMALL_PIECE_M2),
                         'substantial_pieces': sum(1 for m in members if m['area_m2'] >= SUBSTANTIAL_M2),
                         'fragmentation_pairs': sum(1 for p in inner if fragmentation_pair(p)),
                         'party_wall_pairs': sum(1 for p in inner if party_wall(p)),
                         'same_mask_pieces': len({i for p in inner if p['same_mask'] for i in (p['a'], p['b'])}),
                         'occluder_kinds': sorted({k for p in inner for k in p['occluders_between']+p['occluders_over']}),
                         'min_frame_distance_px': min(m['frame_distance_px'] for m in members),
                         'piece_vertices': [m['vertices'] for m in members],
                         '_members': members, '_pairs': inner, '_occluder_ids': occluder_ids, '_union': union})
    return clusters


def sensitivity_components(buildings, d):
    if not buildings:
        return 0
    merged = unary_union([b['_metric'].buffer(d) for b in buildings])
    return len(merged.geoms) if merged.geom_type.startswith('Multi') else 1


# --- (B) vertex load ------------------------------------------------------------------------------------------------------

def simplify_stats(pixel, tolerance):
    simplified = pixel.simplify(tolerance, preserve_topology=True)
    if simplified.is_empty or not simplified.is_valid:
        simplified = polygonal(make_valid(simplified))
    if simplified is None or simplified.is_empty:
        return None
    before, after = pixel.area, simplified.area
    v_before, _ = exterior_vertices(pixel)
    v_after, _ = exterior_vertices(simplified)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        hausdorff = pixel.hausdorff_distance(simplified)
    return {'vertices_before': v_before, 'vertices_after': v_after,
            'area_change': round((after-before)/before, 5) if before else None,
            'symdiff_share': round(pixel.symmetric_difference(simplified).area/before, 5) if before else None,
            'hausdorff_px': round(float(hausdorff), 2)}


def vertex_summary(rows):
    vertices = [r['vertices'] for r in rows]
    per_m = [r['vertices']/r['perimeter_m'] for r in rows if r['perimeter_m'] > 0]
    n = len(rows)
    return {'objects': n, 'vertices_total': int(sum(vertices)), 'median': median(vertices), 'p90': percentile(vertices, 90),
            'max': max(vertices) if vertices else None,
            'over': {str(m): sum(1 for v in vertices if v > m) for m in VERTEX_MARKS},
            'share_over': {str(m): share(sum(1 for v in vertices if v > m), n) for m in VERTEX_MARKS},
            'vertices_per_m_median': median(per_m), 'vertices_per_m_p90': percentile(per_m, 90),
            'with_holes': sum(1 for r in rows if r['holes']), 'hole_vertices_total': int(sum(r['hole_vertices'] for r in rows))}


def simplification_summary(results):
    out = {}
    for t in SIMPLIFY_PX:
        rs = [r for r in results if r['tolerance'] == t and r['stats']]
        changes = [abs(r['stats']['area_change']) for r in rs]
        out[str(t)] = {'objects': len(rs),
                       'vertices_before_total': int(sum(r['stats']['vertices_before'] for r in rs)),
                       'vertices_after_total': int(sum(r['stats']['vertices_after'] for r in rs)),
                       'vertices_removed_share': share(sum(r['stats']['vertices_before']-r['stats']['vertices_after'] for r in rs),
                                                      sum(r['stats']['vertices_before'] for r in rs)),
                       'abs_area_change_median': median(changes), 'abs_area_change_p90': percentile(changes, 90),
                       'abs_area_change_max': max(changes) if changes else None,
                       'over_1pct': sum(1 for c in changes if c > .01), 'over_2pct': sum(1 for c in changes if c > .02),
                       'over_5pct': sum(1 for c in changes if c > .05),
                       'symdiff_share_median': median([r['stats']['symdiff_share'] for r in rs]),
                       'symdiff_share_p90': percentile([r['stats']['symdiff_share'] for r in rs], 90),
                       'hausdorff_px_median': median([r['stats']['hausdorff_px'] for r in rs]),
                       'hausdorff_px_p90': percentile([r['stats']['hausdorff_px'] for r in rs], 90),
                       'hausdorff_px_max': max((r['stats']['hausdorff_px'] for r in rs), default=None),
                       'still_over_100': sum(1 for r in rs if r['stats']['vertices_after'] > 100),
                       'still_over_50': sum(1 for r in rs if r['stats']['vertices_after'] > 50),
                       'to_under_8': sum(1 for r in rs if r['stats']['vertices_after'] < 8)}
    return out


# --- (C) default view -----------------------------------------------------------------------------------------------------

def editor_frame(rings, width, height, aspect, edge_context=False, fit=EDITOR_FIT):
    """objectImageFrame (src/lib/scene-image-frame.ts) in Python: (x, y, w, h) of the viewBox the editor opens on."""
    points = [p for ring in rings for p in ring if all(math.isfinite(c) for c in p)]
    if not points:
        return (0., 0., float(width), float(height))
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    if edge_context:
        extra = min(96, max(32, max(right-left, bottom-top)*.3))
        if left <= .5:
            left -= extra
        if right >= width-.5:
            right += extra
        if top <= .5:
            top -= extra
        if bottom >= height-.5:
            bottom += extra
    w = max(right-left+fit['pad_px'], min(width, max(fit['min_px'], right-left)*fit['factor']))
    h = max(bottom-top+fit['pad_px'], min(height, max(fit['min_px'], bottom-top)*fit['factor']))
    if w/h < aspect:
        w = max(w, min(width, h*aspect))
    else:
        h = max(h, min(height, w/aspect))
    return ((left+right-w)/2, (top+bottom-h)/2, w, h)


def placement(polygon, frame):
    x, y, w, h = frame
    view = box(x, y, x+w, y+h)
    if view.contains(polygon):
        return 'inside'
    if view.intersects(polygon):
        return 'clipped'
    return 'outside'


def asked_objects(review_objects):
    """Live carried copies plus the live replacers of superseded carried copies (takeover / body carry)."""
    by_id = {o['id']: o for o in review_objects}
    asked = {}
    for o in review_objects:
        if not o.get('carried_from'):
            continue
        if o.get('status') == 'superseded':
            for rid in o.get('replacement_ids') or []:
                target = by_id.get(rid)
                if target and target.get('status') != 'superseded':
                    asked[rid] = {'id': rid, 'role': 'replacer', 'copy_id': o['id'], 'kind': target.get('kind'), 'status': target.get('status')}
        else:
            asked[o['id']] = {'id': o['id'], 'role': 'slice' if (o['carried_from'].get('role') != 'whole_roof_body') else 'body',
                              'copy_id': o['id'], 'kind': o.get('kind'), 'status': o.get('status')}
    return [asked[k] for k in sorted(asked)]


def lonlat_to_pixel(scene):
    """Inverse of scene_confidence.geographic: WGS84 -> window pixels through the centroid-anchored Web Mercator metre frame."""
    centre = scene['centroid']
    lat = max(min(centre['lat'], 85.05112878), -85.05112878)
    origin = np.array([(centre['lon']+180.)/360., (1.-math.asinh(math.tan(math.radians(lat)))/math.pi)/2.])
    scale = WORLD_SPAN_M*math.cos(math.radians(centre['lat']))
    to_pixel = np.asarray(scene['meters_to_pixel'], dtype=float)

    def convert(lon, lat):
        lat = max(min(lat, 85.05112878), -85.05112878)
        tile = np.array([(lon+180.)/360., (1.-math.asinh(math.tan(math.radians(lat)))/math.pi)/2.])
        metres = (tile-origin)*scale*np.array([1., -1.])
        return tuple((np.r_[metres, 1.] @ to_pixel).tolist())
    return convert


def parcel_index(roots=PARCEL_DIRS):
    """pfi -> WGS84 geometry over every frozen parcel cache (read-only; the window's lots name their pfi)."""
    index = {}
    for path in sorted(path for root in roots if root.exists() for path in root.glob('*.json')):
        try:
            saved = load_json(path)
        except (OSError, ValueError):
            continue
        data = saved.get('data') if isinstance(saved, dict) else None
        for feature in (data or {}).get('features', []) if isinstance(data, dict) else []:
            pfi = str((feature.get('properties') or {}).get('pfi') or '')
            geometry = feature.get('geometry') or {}
            if pfi and pfi not in index and geometry.get('type') in ('Polygon', 'MultiPolygon'):
                try:
                    if geometry['type'] == 'Polygon':
                        shape = Polygon(geometry['coordinates'][0], geometry['coordinates'][1:])
                    else:
                        shape = MultiPolygon([Polygon(part[0], part[1:]) for part in geometry['coordinates']])
                    if not shape.is_valid:
                        shape = make_valid(shape)
                    index[pfi] = shape
                except (ValueError, TypeError, IndexError):
                    continue
    return index


def project_shape(shape, convert):
    def ring(coords):
        return [convert(x, y) for x, y in coords]
    parts = [shape] if shape.geom_type == 'Polygon' else [g for g in getattr(shape, 'geoms', []) if g.geom_type == 'Polygon']
    projected = [Polygon(ring(p.exterior.coords), [ring(h.coords) for h in p.interiors]) for p in parts]
    return polygonal(make_valid(unary_union(projected))) if projected else None


def view_census(scene, review_rows, model_rows, review_objects, buildings_clusters, parcels, px_m):
    """Per window: the asked objects, the editor's default view on each, what it clips, the lot fit and the lot span."""
    width, height = scene['image_width'], scene['image_height']
    edge_context = bool(scene.get('region')) and width == 512 and height == 512
    rows_by_id = {r['id']: r for r in review_rows}
    model_by_id = {r['id']: r for r in model_rows}
    asked = asked_objects(review_objects)
    first_model = next((o['id'] for o in scene.get('objects', []) if o.get('status') != 'superseded'), None)
    decided_non_asked = [r for r in review_rows if r['live'] and r['status'] in DECIDED and r['id'] not in {a['id'] for a in asked}
                         and not r['carried']]
    frame_info = scene.get('window_frame') or {}
    window = frame_info.get('window') or {}
    lots = frame_info.get('lots') or []
    convert = lonlat_to_pixel(scene)
    lot_shapes = [project_shape(parcels[str(lot.get('pfi'))], convert) for lot in lots if str(lot.get('pfi')) in parcels]
    lot_shapes = [s for s in lot_shapes if s is not None]
    lot_union = unary_union(lot_shapes) if lot_shapes else None
    lot_fit = None
    if lot_union is not None:
        minx, miny, maxx, maxy = lot_union.bounds
        pad = EDITOR_FIT['parcel_pad_px']
        lot_fit = (minx-pad, miny-pad, maxx-minx+2*pad, maxy-miny+2*pad)
    # The house extent: the review-layer building cluster(s) holding the asked building objects.
    house_ids = set()
    for a in asked:
        for c in buildings_clusters:
            if a['id'] in c['ids']:
                house_ids.update(c['ids'])
    house_union = unary_union([rows_by_id[i]['_pixel'] for i in house_ids if i in rows_by_id]) if house_ids else None
    out = {'scene': scene['id'], 'identity': frame_info.get('identity'), 'rule': window.get('rule'), 'planned_span_m': window.get('span_m'),
           'frame_px': [width, height], 'pixel_m': round(px_m, 4), 'asked': [], 'asked_count': len(asked),
           'first_model_object': first_model, 'lots': [{'pfi': lot.get('pfi'), 'lot': lot.get('lot'), 'plan': lot.get('plan'), 'share': lot.get('share')} for lot in lots],
           'lot_resolved': lot_union is not None,
           'lot_span_m': round(max(lot_union.bounds[2]-lot_union.bounds[0], lot_union.bounds[3]-lot_union.bounds[1])*px_m, 1) if lot_union is not None else None,
           'lot_fit_frame_px': [round(v, 1) for v in lot_fit] if lot_fit else None,
           'lot_fit_inside_image': bool(lot_fit and lot_fit[0] >= 0 and lot_fit[1] >= 0 and lot_fit[0]+lot_fit[2] <= width and lot_fit[1]+lot_fit[3] <= height) if lot_fit else None,
           'house_extent_m': round(max(house_union.bounds[2]-house_union.bounds[0], house_union.bounds[3]-house_union.bounds[1])*px_m, 1) if house_union is not None else None,
           'house_pieces': len(house_ids), 'decided_non_asked': len(decided_non_asked)}
    out['lot_span_over_house'] = (round(out['lot_span_m']/out['house_extent_m'], 2)
                                  if out['lot_span_m'] and out['house_extent_m'] else None)
    frames = {}
    if first_model and first_model in model_by_id:
        rings = [rows_by_id[first_model]['_ring'] if first_model in rows_by_id else [], model_by_id[first_model]['_ring']]
        frames['first_model_object'] = {str(a): editor_frame(rings, width, height, a, edge_context) for a in EDITOR_FIT['aspects']}
    for a in asked:
        row = rows_by_id.get(a['id'])
        if row is None:
            out['asked'].append({**a, 'missing_geometry': True})
            continue
        rings = [row['_ring'], model_by_id[a['id']]['_ring'] if a['id'] in model_by_id else []]
        views = {str(asp): editor_frame(rings, width, height, asp, edge_context) for asp in EDITOR_FIT['aspects']}
        frames[a['id']] = views
        others = [b for b in asked if b['id'] != a['id'] and b['id'] in rows_by_id]
        entry = {**a, 'area_m2': row['area_m2'], 'vertices': row['vertices'], 'frame_distance_px': row['frame_distance_px'],
                 'touches_frame': row['frame_distance_px'] <= FRAME_TOL_PX,
                 'default_view_px': {k: [round(v, 1) for v in f] for k, f in views.items()},
                 'default_view_span_m': {k: round(max(f[2], f[3])*px_m, 1) for k, f in views.items()},
                 'other_asked': {k: Counter(placement(rows_by_id[b['id']]['_pixel'], f) for b in others) for k, f in views.items()},
                 'other_asked_not_inside': {k: [b['id'] for b in others if placement(rows_by_id[b['id']]['_pixel'], f) != 'inside'] for k, f in views.items()},
                 'decided_non_asked': {k: Counter(placement(r['_pixel'], f) for r in decided_non_asked) for k, f in views.items()},
                 'in_lot_fit': placement(row['_pixel'], lot_fit) if lot_fit else None,
                 'in_first_object_view': {k: placement(row['_pixel'], f) for k, f in frames.get('first_model_object', {}).items()} or None}
        entry['other_asked'] = {k: dict(v) for k, v in entry['other_asked'].items()}
        entry['decided_non_asked'] = {k: dict(v) for k, v in entry['decided_non_asked'].items()}
        out['asked'].append(entry)
    out['lot_fit_asked'] = dict(Counter(a.get('in_lot_fit') for a in out['asked'] if a.get('in_lot_fit'))) if lot_fit else None
    out['lot_fit_decided_non_asked'] = dict(Counter(placement(r['_pixel'], lot_fit) for r in decided_non_asked)) if lot_fit else None
    out['_frames'] = frames
    out['_lot_fit'] = lot_fit
    return out


# --- rendering -------------------------------------------------------------------------------------------------------------

def font(size):
    for path in ('/System/Library/Fonts/Helvetica.ttc', '/System/Library/Fonts/Supplemental/Arial.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def dashed_polygon(draw, coords, fill, width, dash=(10, 6)):
    on, off = dash
    for (x0, y0), (x1, y1) in zip(coords, coords[1:], strict=False):
        length = math.hypot(x1-x0, y1-y0)
        if length == 0:
            continue
        ux, uy = (x1-x0)/length, (y1-y0)/length
        t = 0.
        while t < length:
            end = min(t+on, length)
            draw.line([(x0+ux*t, y0+uy*t), (x0+ux*end, y0+uy*end)], fill=fill, width=width)
            t = end+off


def draw_polygon(overlay_draw, line_draw, polygon, transform, colour, fill_alpha, width=2, dashed=False):
    parts = [polygon] if polygon.geom_type == 'Polygon' else list(polygon.geoms)
    for part in parts:
        exterior = [transform(x, y) for x, y in part.exterior.coords]
        if fill_alpha:
            overlay_draw.polygon(exterior, fill=(*colour, fill_alpha))
            for hole in part.interiors:
                overlay_draw.polygon([transform(x, y) for x, y in hole.coords], fill=(0, 0, 0, 0))
        if dashed:
            dashed_polygon(line_draw, exterior, (*colour, 255), width)
        else:
            line_draw.line(exterior, fill=(*colour, 255), width=width)
            for hole in part.interiors:
                line_draw.line([transform(x, y) for x, y in hole.coords], fill=(*colour, 255), width=max(1, width-1))


def render_scene_crop(scene_dir, scene, pieces, occluders, hull, title_lines, path, *, extra_boxes=(), margin_share=.25,
                      scale=RENDER_SCALE, label_pieces=True):
    """Crop the native frame around `hull` (+ extra boxes), upscale x3 nearest, draw pieces in distinct colours, occluders
    green, the hull dotted white, the extra boxes dotted orange, and the title in a band above."""
    image_path = scene_dir/f"{scene['image_sha256']}.png"
    image = Image.open(image_path).convert('RGB')
    width, height = image.size
    bounds = [hull.bounds] + [b for b in extra_boxes]
    minx = min(b[0] for b in bounds)
    miny = min(b[1] for b in bounds)
    maxx = max(b[2] for b in bounds)
    maxy = max(b[3] for b in bounds)
    span = max(maxx-minx, maxy-miny)
    margin = max(24., span*margin_share)
    x0, y0 = int(max(0, math.floor(minx-margin))), int(max(0, math.floor(miny-margin)))
    x1, y1 = int(min(width, math.ceil(maxx+margin))), int(min(height, math.ceil(maxy+margin)))
    crop = image.crop((x0, y0, x1, y1)).resize(((x1-x0)*scale, (y1-y0)*scale), Image.NEAREST).convert('RGBA')

    def transform(x, y):
        return ((x-x0)*scale, (y-y0)*scale)
    overlay = Image.new('RGBA', crop.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    lines = Image.new('RGBA', crop.size, (0, 0, 0, 0))
    line_draw = ImageDraw.Draw(lines)
    for o in occluders:
        draw_polygon(overlay_draw, line_draw, o['_pixel'], transform, OCCLUDER_GREEN, 50, width=2)
    for i, p in enumerate(pieces):
        colour = PALETTE[i % len(PALETTE)]
        draw_polygon(overlay_draw, line_draw, p['_pixel'], transform, colour, 70, width=3)
    draw_polygon(overlay_draw, line_draw, hull, transform, (255, 255, 255), 0, width=2, dashed=True)
    for b in extra_boxes:
        dashed_polygon(line_draw, [transform(b[0], b[1]), transform(b[2], b[1]), transform(b[2], b[3]), transform(b[0], b[3]), transform(b[0], b[1])],
                       (255, 140, 0, 255), 3, dash=(14, 8))
    composed = Image.alpha_composite(Image.alpha_composite(crop, overlay), lines)
    small = font(13)
    label_draw = ImageDraw.Draw(composed)
    if label_pieces:
        for i, p in enumerate(pieces):
            cx, cy = p['_pixel'].representative_point().coords[0]
            text = f"{i+1} {p['area_m2']:.0f}m2 v{p['vertices']} {p['status'][:4]}"
            tx, ty = transform(cx, cy)
            tw = label_draw.textlength(text, font=small)
            label_draw.rectangle([tx-2, ty-8, tx+tw+2, ty+8], fill=(0, 0, 0, 170))
            label_draw.text((tx, ty-7), text, fill=PALETTE[i % len(PALETTE)], font=small)
    title_font = font(15)
    band = 20*len(title_lines)+10
    canvas = Image.new('RGB', (composed.width, composed.height+band), (18, 18, 18))
    canvas.paste(composed.convert('RGB'), (0, band))
    draw = ImageDraw.Draw(canvas)
    for i, line in enumerate(title_lines):
        draw.text((8, 5+20*i), line, fill=(235, 235, 235), font=title_font)
    # scale bar: 5 m
    bar_px = 5./scene_pixel_m(scene)*scale
    draw.line([(10, canvas.height-12), (10+bar_px, canvas.height-12)], fill=(255, 255, 255), width=3)
    draw.text((12, canvas.height-30), '5 m', fill=(255, 255, 255), font=small)
    canvas.save(path)
    return path


def scene_pixel_m(scene):
    return float(math.sqrt(abs(np.linalg.det(np.asarray(scene['pixel_to_meters'], dtype=float)[:2]))))


def contact_sheet(entries, path, columns=3, thumb_width=560):
    tiles = []
    for entry in entries:
        image = Image.open(entry['render']).convert('RGB')
        ratio = thumb_width/image.width
        tiles.append((image.resize((thumb_width, max(1, int(image.height*ratio)))), entry['caption']))
    if not tiles:
        return None
    row_heights = []
    for r in range(0, len(tiles), columns):
        row_heights.append(max(t[0].height for t in tiles[r:r+columns])+34)
    sheet = Image.new('RGB', (columns*(thumb_width+10)+10, sum(row_heights)+10), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)
    small = font(13)
    y = 10
    for r, start in enumerate(range(0, len(tiles), columns)):
        for c, (thumb, caption) in enumerate(tiles[start:start+columns]):
            x = 10+c*(thumb_width+10)
            sheet.paste(thumb, (x, y))
            draw.text((x, y+thumb.height+4), caption[:95], fill=(230, 230, 230), font=small)
            if len(caption) > 95:
                draw.text((x, y+thumb.height+18), caption[95:190], fill=(230, 230, 230), font=small)
        y += row_heights[r]
    sheet.save(path)
    return path


# --- census ------------------------------------------------------------------------------------------------------------------

def census_scene(path, group, parcels):
    scene = load_json(path/'latest.json')
    px_m = scene_pixel_m(scene)
    layers = {'model': scene.get('objects', [])}
    review = load_json(path/'review.json') if (path/'review.json').exists() else None
    if review is not None:
        layers['review'] = review.get('objects', [])
    result = {'scene': scene['id'], 'group': group, 'frame_px': [scene['image_width'], scene['image_height']], 'pixel_m': round(px_m, 4),
              'review_revision': review.get('revision') if review else None,
              'review_signature_matches_latest': (review.get('model_signature') == scene.get('model_signature')) if review else None,
              'layers': {}}
    rows_all, pairs_all, clusters_all, simplify_all = [], [], [], []
    rows_by_layer = {}
    for layer, objects in layers.items():
        rows, invalid = layer_rows(scene, objects, layer)
        rows_by_layer[layer] = rows
        live = [r for r in rows if r['live']]
        buildings = [r for r in live if r['kind'] == 'building']
        occluders = [r for r in live if r['kind'] in OCCLUDER_KINDS]
        pairs = building_pairs(buildings, occluders)
        clusters = building_clusters(buildings, pairs, scene['id'], layer, group)
        multi = [c for c in clusters if c['pieces'] >= 2]
        entry = {'objects_live': len(live), 'invalid_rings': dict(invalid), 'buildings': len(buildings),
                 'building_statuses': dict(Counter(b['status'] for b in buildings)),
                 'buildings_carried': sum(1 for b in buildings if b['carried']),
                 'touching_pairs': len(pairs), 'pair_categories': dict(Counter(pair_category(p) for p in pairs)),
                 'pair_bands': dict(Counter(p['band'] for p in pairs)), 'pairs_same_mask': sum(1 for p in pairs if p['same_mask']),
                 'pairs_occluder_between': sum(1 for p in pairs if p['occluders_between']),
                 'pairs_occluder_over': sum(1 for p in pairs if p['occluders_over']),
                 'pairs_band_0.5_0.8': sum(1 for p in pairs if p['band'] == 'band_0.5_0.8'),
                 'occluder_between_kinds': dict(Counter(k for p in pairs for k in p['occluders_between'])),
                 'clusters': len(clusters), 'clusters_multi': len(multi), 'pieces_in_multi': sum(c['pieces'] for c in multi),
                 'clusters_at_buffer_0.5': sensitivity_components(buildings, SENSITIVITY_BUFFER_M),
                 'clusters_confirmed_beside_undecided': sum(1 for c in multi if c['confirmed_beside_undecided']),
                 'largest_cluster_pieces': max((c['pieces'] for c in clusters), default=0),
                 'vertex_load': {k: vertex_summary([r for r in live if r['kind'] == k]) for k in VERTEX_KINDS if any(r['kind'] == k for r in live)},
                 'objects_over_100_vertices': dict(Counter(r['kind'] for r in live if r['vertices'] > 100)),
                 'objects_over_200_vertices': dict(Counter(r['kind'] for r in live if r['vertices'] > 200))}
        if group == 'windows':
            simplify = [{'scene': scene['id'], 'layer': layer, 'id': b['id'], 'tolerance': t, 'stats': simplify_stats(b['_pixel'], t)}
                        for b in buildings for t in SIMPLIFY_PX]
            simplify_all.extend(simplify)
            entry['simplification'] = simplification_summary(simplify)
        result['layers'][layer] = entry
        rows_all.extend(rows)
        pairs_all.extend({**p, 'scene': scene['id'], 'layer': layer, 'group': group} for p in pairs)
        clusters_all.extend(clusters)
    view = None
    if group == 'windows' and review is not None:
        review_clusters = [c for c in clusters_all if c['layer'] == 'review']
        view = view_census(scene, rows_by_layer['review'], rows_by_layer['model'], review.get('objects', []), review_clusters, parcels, px_m)
    return {'scene': scene, 'path': path, 'summary': result, 'rows': rows_all, 'pairs': pairs_all, 'clusters': clusters_all,
            'simplify': simplify_all, 'view': view}


def pooled_fragmentation(scene_results, group, layer):
    pairs = [p for s in scene_results for p in s['pairs'] if p['group'] == group and p['layer'] == layer]
    clusters = [c for s in scene_results for c in s['clusters'] if c['group'] == group and c['layer'] == layer]
    buildings = [r for s in scene_results for r in s['rows'] if s['summary']['group'] == group and r['layer'] == layer and r['live'] and r['kind'] == 'building']
    multi = [c for c in clusters if c['pieces'] >= 2]
    scenes = [s for s in scene_results if s['summary']['group'] == group and layer in s['summary']['layers']]
    sizes = Counter(c['pieces'] for c in multi)
    return {'scenes': len(scenes), 'buildings': len(buildings), 'buildings_carried': sum(1 for b in buildings if b['carried']),
            'building_statuses': dict(Counter(b['status'] for b in buildings)),
            'touching_pairs': len(pairs), 'pair_categories': dict(Counter(pair_category(p) for p in pairs)),
            'pair_bands': dict(Counter(p['band'] for p in pairs)),
            'pairs_same_mask': sum(1 for p in pairs if p['same_mask']),
            'pairs_occluder_between': sum(1 for p in pairs if p['occluders_between']),
            'pairs_occluder_over': sum(1 for p in pairs if p['occluders_over']),
            'pairs_band_0.5_0.8': sum(1 for p in pairs if p['band'] == 'band_0.5_0.8'),
            'pairs_ge_0.8': sum(1 for p in pairs if p['band'] == 'ge_0.8'),
            'pairs_with_carried': sum(1 for p in pairs if p['carried']),
            'pairs_small_piece': sum(1 for p in pairs if p['small_piece']),
            'pairs_party_wall': sum(1 for p in pairs if party_wall(p)),
            'pairs_fragmentation': sum(1 for p in pairs if fragmentation_pair(p)),
            'pairs_dedupe_gap': sum(1 for p in pairs if p['dedupe_gap']),
            'pairs_small_piece_categories': dict(Counter(pair_category(p) for p in pairs if p['small_piece'])),
            'smaller_piece_m2_median': median([p['smaller_m2'] for p in pairs]),
            'occluder_between_kinds': dict(Counter(k for p in pairs for k in p['occluders_between'])),
            'occluder_over_kinds': dict(Counter(k for p in pairs for k in p['occluders_over'])),
            'gap_median_m': median([p['gap_m'] for p in pairs]), 'gap_p90_m': percentile([p['gap_m'] for p in pairs], 90),
            'pairs_gap_zero': sum(1 for p in pairs if p['gap_m'] == 0),
            'clusters': len(clusters), 'clusters_multi': len(multi), 'pieces_in_multi': sum(c['pieces'] for c in multi),
            'share_buildings_in_multi': share(sum(c['pieces'] for c in multi), len(buildings)),
            'clusters_at_buffer_0.5': sum(s['summary']['layers'][layer]['clusters_at_buffer_0.5'] for s in scenes),
            'pieces_per_cluster_distribution': {str(k): sizes[k] for k in sorted(sizes)},
            'pieces_per_multi_cluster_median': median([c['pieces'] for c in multi]),
            'largest_share_median': median([c['largest_share'] for c in multi]),
            'largest_share_p10': percentile([c['largest_share'] for c in multi], 10),
            'multi_cluster_area_median_m2': median([c['area_m2'] for c in multi]),
            'clusters_confirmed_beside_undecided': sum(1 for c in multi if c['confirmed_beside_undecided']),
            'clusters_with_carried_piece': sum(1 for c in multi if c['carried_pieces']),
            'clusters_all_same_mask': sum(1 for c in multi if c['same_mask_pieces'] == c['pieces']),
            'clusters_any_same_mask': sum(1 for c in multi if c['same_mask_pieces']),
            'clusters_any_occluder_between': sum(1 for c in multi if c['pair_categories'].get('occluder_between') or any(p['occluders_between'] for p in c['_pairs'])),
            'clusters_any_band': sum(1 for c in multi if any(p['band'] == 'band_0.5_0.8' for p in c['_pairs'])),
            'clusters_under_fill_0.7': sum(1 for c in multi if c['fill_of_hull'] is not None and c['fill_of_hull'] < .7),
            'clusters_only_party_walls': sum(1 for c in multi if c['fragmentation_pairs'] == 0),
            'clusters_with_fragment_piece': sum(1 for c in multi if c['fragment_pieces']),
            'fragment_pieces_in_multi': sum(c['fragment_pieces'] for c in multi),
            'buildings_under_12m2': sum(1 for b in buildings if b['area_m2'] < SMALL_PIECE_M2),
            'buildings_under_12m2_in_multi': sum(1 for c in multi for m in c['_members'] if m['area_m2'] < SMALL_PIECE_M2)}


def pooled_vertices(scene_results, group, layer):
    rows = [r for s in scene_results for r in s['rows'] if s['summary']['group'] == group and r['layer'] == layer and r['live']]
    return {k: vertex_summary([r for r in rows if r['kind'] == k]) for k in VERTEX_KINDS if any(r['kind'] == k for r in rows)}


def named_figures(scene_results, name, object_id):
    result = next((s for s in scene_results if s['scene']['id'] == name), None)
    if result is None:
        return {'scene': name, 'id': object_id, 'missing': 'scene not in population'}
    rows = [r for r in result['rows'] if r['layer'] == 'review']
    row = next((r for r in rows if r['id'] == object_id), None)
    if row is None:
        return {'scene': name, 'id': object_id, 'missing': 'object not in review layer'}
    live = [r for r in rows if r['live'] and r['id'] != object_id]
    neighbours = []
    for other in live:
        gap = row['_metric'].distance(other['_metric'])
        if gap <= TOUCH_M:
            inter = row['_metric'].intersection(other['_metric']).area
            neighbours.append({'id': other['id'], 'kind': other['kind'], 'status': other['status'], 'area_m2': other['area_m2'],
                               'vertices': other['vertices'], 'gap_m': round(float(gap), 3),
                               'cover_of_named': round(inter/row['area_m2'], 4) if row['area_m2'] else None,
                               'cover_of_smaller': round(inter/min(row['area_m2'], other['area_m2']), 4) if min(row['area_m2'], other['area_m2']) else None,
                               'same_mask': bool(other['kind'] == row['kind'] and other['prompt'] == row['prompt'] and other['score'] == row['score'])})
    neighbours.sort(key=lambda n: (-n['cover_of_named'] if n['cover_of_named'] else 0, n['gap_m']))
    cluster = next((c for c in result['clusters'] if c['layer'] == 'review' and object_id in c['ids']), None)
    simplify = {str(t): simplify_stats(row['_pixel'], t) for t in SIMPLIFY_PX}
    buildings = [r for r in rows if r['live'] and r['kind'] == 'building']
    view = result['view']
    view_entry = None
    if view:
        # Where the named object sits in the default view of each asked object of its window.
        frames = view['_frames']
        view_entry = {'asked_ids': [a['id'] for a in view['asked']], 'named_is_asked': object_id in {a['id'] for a in view['asked']},
                      'placement_in_asked_views': {aid: {k: placement(row['_pixel'], f) for k, f in fr.items()} for aid, fr in frames.items() if aid != 'first_model_object'},
                      'placement_in_first_object_view': {k: placement(row['_pixel'], f) for k, f in frames.get('first_model_object', {}).items()},
                      'placement_in_lot_fit': placement(row['_pixel'], view['_lot_fit']) if view['_lot_fit'] else None,
                      'own_default_view_span_m': {k: round(max(f[2], f[3])*result['summary']['pixel_m'], 1) for k, f in editor_frames_for(row, result).items()}}
    return {'scene': name, 'id': object_id, 'kind': row['kind'], 'status': row['status'], 'carried': row['carried'], 'carried_role': row['carried_role'],
            'area_m2': row['area_m2'], 'perimeter_m': row['perimeter_m'], 'vertices': row['vertices'], 'holes': row['holes'],
            'vertices_per_m': round(row['vertices']/row['perimeter_m'], 2) if row['perimeter_m'] else None,
            'frame_distance_px': row['frame_distance_px'], 'score': row['score'],
            'window_buildings': len(buildings), 'window_building_vertices_median': median([b['vertices'] for b in buildings]),
            'cluster': ({k: v for k, v in cluster.items() if not k.startswith('_')} if cluster else None),
            'neighbours_within_0.5m': neighbours, 'simplification': simplify, 'view': view_entry}


def editor_frames_for(row, result):
    scene = result['scene']
    edge_context = bool(scene.get('region')) and scene['image_width'] == 512 and scene['image_height'] == 512
    return {str(a): editor_frame([row['_ring']], scene['image_width'], scene['image_height'], a, edge_context) for a in EDITOR_FIT['aspects']}


def worst_clusters(scene_results, n=WORST_CLUSTERS, key='fragmentation'):
    """Review-layer clusters ranked by fragmentation pairs (pairs that are not party walls), then fragment pieces, then pieces;
    key='pieces' ranks by piece count alone (terrace rows of adjoining houses lead that list)."""
    clusters = [c for s in scene_results for c in s['clusters'] if c['layer'] == 'review' and c['pieces'] >= 2]
    if key == 'pieces':
        clusters.sort(key=lambda c: (-c['pieces'], -(1-(c['largest_share'] or 1)), -c['area_m2']))
    else:
        clusters.sort(key=lambda c: (-c['fragmentation_pairs'], -c['fragment_pieces'], -c['pieces'], -c['area_m2']))
    return clusters[:n]


def render_cluster(result, cluster, path, note=None):
    scene = result['scene']
    rows = {r['id']: r for r in result['rows'] if r['layer'] == 'review'}
    occluders = [rows[i] for i in cluster['_occluder_ids'] if i in rows]
    hull_px = unary_union([m['_pixel'] for m in cluster['_members']]).convex_hull
    categories = ', '.join(f'{k} {v}' for k, v in sorted(cluster['pair_categories'].items())) or 'none'
    title = [f"{scene['id']} review layer: cluster of {cluster['pieces']} building pieces, {cluster['area_m2']} m2 union, largest {cluster['largest_m2']} m2 ({cluster['largest_share']:.0%}); "
             f"fragmentation pairs {cluster['fragmentation_pairs']} / party-wall pairs {cluster['party_wall_pairs']}; pieces < 12 m2: {cluster['fragment_pieces']}",
             f"pairs {cluster['pairs']}: {categories}; statuses {cluster['statuses']}; occluders (green) {cluster['occluder_kinds'] or 'none'}; hull fill {cluster['fill_of_hull']:.2f}",
             f"vertices per piece {cluster['piece_vertices']}; m/px {result['summary']['pixel_m']}; native x{RENDER_SCALE}; hull dotted white"]
    if note:
        title.append(note)
    return render_scene_crop(result['path'], scene, cluster['_members'], occluders, hull_px, title, path)


def render_named(result, figures, path):
    scene = result['scene']
    rows = {r['id']: r for r in result['rows'] if r['layer'] == 'review'}
    row = rows[figures['id']]
    neighbour_rows = [rows[n['id']] for n in figures['neighbours_within_0.5m'] if n['id'] in rows]
    pieces = [row] + [r for r in neighbour_rows if r['kind'] == 'building']
    occluders = [r for r in neighbour_rows if r['kind'] in OCCLUDER_KINDS]
    others = [r for r in neighbour_rows if r['kind'] not in OCCLUDER_KINDS and r['kind'] != 'building']
    hull_px = unary_union([p['_pixel'] for p in pieces+others]).convex_hull
    extra = []
    note = figures['_note']
    title = [f"{scene['id']} {figures['id']} ({row['kind']}, {row['status']}, {row['area_m2']} m2, v{row['vertices']}, carried {row['carried']}): \"{note[:70]}\"",
             "neighbours <= 0.5 m: " + ('; '.join(f"{n['kind'][:5]} {n['id'][:8]} {n['area_m2']:.0f}m2 cover_named {n['cover_of_named']:.2f} gap {n['gap_m']}" for n in figures['neighbours_within_0.5m'][:5]) or 'none'),
             f"DP 1/2/3 px -> v{figures['simplification']['1.0']['vertices_after'] if figures['simplification']['1.0'] else '-'}/"
             f"v{figures['simplification']['2.0']['vertices_after'] if figures['simplification']['2.0'] else '-'}/"
             f"v{figures['simplification']['3.0']['vertices_after'] if figures['simplification']['3.0'] else '-'}; "
             f"cluster pieces {figures['cluster']['pieces'] if figures['cluster'] else '-'}; m/px {result['summary']['pixel_m']}; native x{RENDER_SCALE}; piece 1 = named"]
    view = figures.get('view')
    if view and not view['named_is_asked'] and view['placement_in_asked_views']:
        # The default view the reviewer's link to the ASKED target opens (strict square aspect), dotted orange; the asked object
        # itself is drawn as the next piece so the frame reads against what it was fitted to.
        frames = result['view']['_frames']
        for aid, fr in frames.items():
            if aid == 'first_model_object':
                continue
            x, y, w, h = fr['1.0']
            extra.append((x, y, x+w, y+h))
            if aid in rows and aid not in {p['id'] for p in pieces}:
                pieces.append(rows[aid])
            title.append(f"orange dotted: editor default view on asked {aid} (aspect 1.0, {round(max(w, h)*result['summary']['pixel_m'], 1)} m, piece {len(pieces)} = asked); named object is {view['placement_in_asked_views'][aid]['1.0']}")
    # Other building pieces of the named object's cluster, drawn as pieces too.
    cluster = next((c for c in result['clusters'] if c['layer'] == 'review' and figures['id'] in c['ids']), None)
    if cluster:
        seen = {p['id'] for p in pieces}
        pieces += [m for m in cluster['_members'] if m['id'] not in seen]
        occ_ids = {o['id'] for o in occluders}
        occluders += [rows[i] for i in cluster['_occluder_ids'] if i in rows and i not in occ_ids]
        hull_px = unary_union([p['_pixel'] for p in pieces+others]).convex_hull
    # Non-building, non-occluder neighbours (a pool, a driveway) are drawn as pieces after the buildings, so they get a colour.
    pieces += others
    return render_scene_crop(result['path'], scene, pieces, occluders, hull_px, title, path, extra_boxes=extra)


# --- markdown ----------------------------------------------------------------------------------------------------------------

def table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |', '|' + '---|'*len(headers)]
    out += ['| ' + ' | '.join('' if v is None else str(v) for v in r) + ' |' for r in rows]
    return '\n'.join(out)


def write_markdown(census, path):
    lines = [f"# Roof fragmentation, vertex load and default view census ({census['written_at'][:10]})", '',
             f"Read-only over data/scenes: {census['population']['windows']} windows (win-/lot-) and {census['population']['review_set']} "
             f"review-set scenes with a review.json; layers model (latest.json) and review (review.json, the editor's `edited`). Every "
             f"figure is a count or a geometry, never accuracy; nothing is promoted. Code `scripts/ml/experiments/roof_fragmentation_census.py` "
             f"sha256 {census['code_sha256'][:16]}.", '',
             '## Reviewer observations (verbatim)', '']
    lines += [f"{i+1}. `{s}` `{o}`: \"{n}\"" for i, (s, o, n) in enumerate(NAMED)]
    lines += ['', '## A. Roof fragmentation', '',
              'A touching pair is two live building pieces with metric gap <= 0.5 m; a cluster is a connected component of touching pairs. '
              'Pair category precedence: same_mask (one SAM3 mask split into components: same prompt and 4 dp score) > occluder_between '
              '(a tree / vegetation / solar_panel / car object covers >= min(0.5 m2, 25 %) of the pair\'s hull bridge) > band_0.5_0.8 '
              '(the smaller piece is covered 0.5-0.8 by the other: under the 0.8 takeover floor) > ge_0.8 > carried_slice > overlap_lt_0.5 > touch_only.', '']
    rows = []
    for group in ('windows', 'review_set'):
        for layer in ('model', 'review'):
            f = census['fragmentation'][group][layer]
            rows.append([group, layer, f['scenes'], f['buildings'], f['touching_pairs'], f['pairs_party_wall'], f['pairs_fragmentation'], f['pairs_small_piece'],
                         f['pairs_same_mask'], f['pairs_occluder_between'], f['pairs_band_0.5_0.8'], f['pairs_ge_0.8'], f['pairs_dedupe_gap'], f['pairs_with_carried'],
                         f['clusters_multi'], f['clusters_only_party_walls'], f['clusters_with_fragment_piece'], f['pieces_in_multi'],
                         f['share_buildings_in_multi'], f['clusters_at_buffer_0.5'], f['clusters_confirmed_beside_undecided'],
                         f['largest_share_median'], f['pieces_per_cluster_distribution']])
    lines.append(table(['group', 'layer', 'scenes', 'buildings', 'touching pairs', 'party wall', 'fragmentation', 'smaller < 12 m2', 'same mask', 'occluder between',
                        'band .5-.8', '>= .8', 'dedupe gap', 'with carried', 'multi clusters', 'only party walls', 'with piece < 12 m2', 'pieces in multi',
                        'share of buildings', 'components at buffer .5 m', 'confirmed beside undecided', 'largest share median', 'pieces per cluster'], rows))
    lines += ['', 'A party-wall pair is two pieces >= 20 m2 that only touch (cover < .05), are not one mask and have no occluder between: adjoining houses on '
              'terrace rows and unit blocks, not fragments. A fragmentation pair is every other touching pair. The dedupe gap is a pair whose smaller piece is '
              'covered >= .8 but < .95 by the other at IoU <= .8: the building duplicate rule (IoU > .8 or containment > .95) lets both through, where every other '
              'kind would have dropped the lower-scored one at .8 of the smaller area. Buildings under 12 m2 (analysis band; the extraction floor is 1 m2): '
              + ', '.join(f"{g}/{layer} {census['fragmentation'][g][layer]['buildings_under_12m2']} of which {census['fragmentation'][g][layer]['buildings_under_12m2_in_multi']} in multi clusters"
                          for g in ('windows', 'review_set') for layer in ('model', 'review')) + '.']
    lines += ['', 'Pair categories (precedence applied once per pair):', '']
    rows = [[g, layer, json.dumps(census['fragmentation'][g][layer]['pair_categories'], sort_keys=True), json.dumps(census['fragmentation'][g][layer]['occluder_between_kinds'], sort_keys=True),
             json.dumps(census['fragmentation'][g][layer]['occluder_over_kinds'], sort_keys=True), census['fragmentation'][g][layer]['pairs_gap_zero'], census['fragmentation'][g][layer]['gap_median_m']]
            for g in ('windows', 'review_set') for layer in ('model', 'review')]
    lines.append(table(['group', 'layer', 'categories', 'occluder between kinds', 'occluder over kinds', 'pairs at gap 0', 'gap median m'], rows))
    lines += ['', 'Pairs whose smaller piece is under 12 m2 (the fragments proper), by category:', '']
    rows = [[g, layer, census['fragmentation'][g][layer]['pairs_small_piece'], json.dumps(census['fragmentation'][g][layer]['pairs_small_piece_categories'], sort_keys=True),
             census['fragmentation'][g][layer]['smaller_piece_m2_median']] for g in ('windows', 'review_set') for layer in ('model', 'review')]
    lines.append(table(['group', 'layer', 'pairs', 'categories', 'smaller piece m2 median (all pairs)'], rows))
    lines += ['', 'Clusters of >= 2 pieces by what holds them apart (a cluster can count in several columns):', '']
    rows = [[g, layer, census['fragmentation'][g][layer]['clusters_multi'], census['fragmentation'][g][layer]['clusters_all_same_mask'], census['fragmentation'][g][layer]['clusters_any_same_mask'],
             census['fragmentation'][g][layer]['clusters_any_occluder_between'], census['fragmentation'][g][layer]['clusters_any_band'], census['fragmentation'][g][layer]['clusters_with_carried_piece'],
             census['fragmentation'][g][layer]['clusters_confirmed_beside_undecided'], census['fragmentation'][g][layer]['clusters_under_fill_0.7'], census['fragmentation'][g][layer]['multi_cluster_area_median_m2']]
            for g in ('windows', 'review_set') for layer in ('model', 'review')]
    lines.append(table(['group', 'layer', 'multi clusters', 'all same mask', 'any same mask', 'any occluder between', 'any .5-.8 band', 'with carried piece',
                        'confirmed beside undecided', 'hull fill < .7', 'area median m2'], rows))
    lines += ['', f'### The {len(census["worst_clusters"])} worst clusters (review layer, ranked by fragmentation pairs, then pieces under 12 m2, then pieces)', '']
    rows = [[i+1, c['scene'], c['group'], c['pieces'], c['fragment_pieces'], c['fragmentation_pairs'], c['party_wall_pairs'], c['area_m2'], c['largest_m2'], c['largest_share'],
             json.dumps(c['pair_categories'], sort_keys=True), json.dumps(c['statuses'], sort_keys=True), ','.join(c['occluder_kinds']) or '-', c['fill_of_hull'], c['render']]
            for i, c in enumerate(census['worst_clusters'])]
    lines.append(table(['#', 'scene', 'group', 'pieces', '< 12 m2', 'fragmentation pairs', 'party-wall pairs', 'union m2', 'largest m2', 'largest share', 'pair categories',
                        'statuses', 'occluders', 'hull fill', 'render'], rows))
    lines += ['', f'### The {len(census["largest_clusters"])} largest clusters by piece count (review layer; terrace rows and unit blocks lead; not rendered unless also above)', '']
    rows = [[i+1, c['scene'], c['group'], c['pieces'], c['fragment_pieces'], c['fragmentation_pairs'], c['party_wall_pairs'], c['area_m2'], c['largest_share'],
             json.dumps(c['statuses'], sort_keys=True)] for i, c in enumerate(census['largest_clusters'])]
    lines.append(table(['#', 'scene', 'group', 'pieces', '< 12 m2', 'fragmentation pairs', 'party-wall pairs', 'union m2', 'largest share', 'statuses'], rows))
    lines += ['', '### The six named objects', '']
    for n in census['named']:
        if n.get('missing'):
            lines.append(f"- `{n['scene']}` `{n['id']}`: {n['missing']}")
            continue
        cl = n['cluster']
        lines.append(f"- **`{n['scene']}` `{n['id']}`** ({n['kind']}, {n['status']}, carried {n['carried']}): {n['area_m2']} m2, {n['vertices']} vertices "
                     f"({n['vertices_per_m']} v/m; window buildings median {n['window_building_vertices_median']}), {n['frame_distance_px']} px from the frame. "
                     f"Cluster: {cl['pieces'] if cl else 1} piece(s)" + (f", union {cl['area_m2']} m2, largest {cl['largest_m2']} m2, pair categories {cl['pair_categories']}, fragmentation pairs {cl['fragmentation_pairs']}, party-wall pairs {cl['party_wall_pairs']}, pieces < 12 m2 {cl['fragment_pieces']}, occluders {cl['occluder_kinds']}" if cl and cl['pieces'] > 1 else '') + '.')
        for nb in n['neighbours_within_0.5m'][:6]:
            lines.append(f"  - neighbour {nb['kind']} `{nb['id']}` {nb['status']} {nb['area_m2']} m2 v{nb['vertices']}: gap {nb['gap_m']} m, covers {nb['cover_of_named']:.2f} of the named object"
                         f" (cover of smaller {nb['cover_of_smaller']:.2f}), same mask {nb['same_mask']}")
        s = n['simplification']
        lines.append('  - DP 1 / 2 / 3 px: vertices ' + ' / '.join(str(s[str(t)]['vertices_after']) if s[str(t)] else '-' for t in SIMPLIFY_PX) +
                     ', area change ' + ' / '.join(f"{s[str(t)]['area_change']:+.2%}" if s[str(t)] else '-' for t in SIMPLIFY_PX) +
                     ', Hausdorff px ' + ' / '.join(str(s[str(t)]['hausdorff_px']) if s[str(t)] else '-' for t in SIMPLIFY_PX))
        v = n.get('view')
        if v:
            lines.append(f"  - default view: asked objects of the window {v['asked_ids']}; named is asked: {v['named_is_asked']}; "
                         f"placement in the asked objects' default views {v['placement_in_asked_views']}; in the first-object view {v['placement_in_first_object_view']}; "
                         f"in the lot fit {v['placement_in_lot_fit']}; own default view span m {v['own_default_view_span_m']}")
        lines.append(f"  - render `{n['render']}`")
    lines += ['', '## B. Vertex load', '',
              'Live objects, exterior ring vertices as stored; v/m = vertices per metre of metric perimeter. The extraction path simplifies at 0.9 px, '
              'then at 2 px above 150 vertices, and drops a ring above 200 (scene_extract.py extract_objects).', '']
    rows = []
    for group in ('windows', 'review_set'):
        for layer in ('model', 'review'):
            for kind, v in census['vertex_load'][group][layer].items():
                rows.append([group, layer, kind, v['objects'], v['median'], v['p90'], v['max'], v['share_over']['50'], v['share_over']['100'], v['share_over']['150'],
                             v['over']['100'], v['vertices_per_m_median'], v['with_holes']])
    lines.append(table(['group', 'layer', 'kind', 'objects', 'median v', 'p90 v', 'max v', 'share > 50', 'share > 100', 'share > 150', 'n > 100', 'v/m median', 'with holes'], rows))
    lines += ['', f"Objects the editor receives above 100 vertices on the {census['population']['windows']} windows (live, by layer and kind): "
              f"{json.dumps(census['editor_over_100'], sort_keys=True)}; above 200: {json.dumps(census['editor_over_200'], sort_keys=True)}.", '',
              '### Douglas-Peucker on the windows\' building pieces (stored pixel polygon, preserve_topology)', '']
    rows = []
    for layer in ('model', 'review'):
        for t in SIMPLIFY_PX:
            s = census['simplification'][layer][str(t)]
            rows.append([layer, t, s['objects'], s['vertices_before_total'], s['vertices_after_total'], s['vertices_removed_share'], s['abs_area_change_median'],
                         s['abs_area_change_p90'], s['abs_area_change_max'], s['over_1pct'], s['over_2pct'], s['over_5pct'], s['symdiff_share_median'], s['symdiff_share_p90'],
                         s['hausdorff_px_median'], s['hausdorff_px_p90'], s['hausdorff_px_max'], s['still_over_100'], s['to_under_8']])
    lines.append(table(['layer', 'tol px', 'pieces', 'v before', 'v after', 'removed share', '|area change| median', 'p90', 'max', '> 1 %', '> 2 %', '> 5 %',
                        'symdiff median', 'symdiff p90', 'Hausdorff px median', 'p90', 'max', 'still > 100 v', 'to < 8 v'], rows))
    lines += ['', '## C. Default view', '',
              census['default_view']['reading'], '']
    d = census['default_view']
    lines += [f"Windows {d['windows']}, asked objects {d['asked_objects']} ({json.dumps(d['asked_roles'], sort_keys=True)}; readiness file agreement: {json.dumps(d['readiness_agreement'])}). "
              f"Asked objects touching the window frame: {d['asked_touching_frame']}. Default view span on the asked object (aspect 1.0): median {d['asked_view_span_m_median']} m, "
              f"p90 {d['asked_view_span_m_p90']} m against planned window spans median {d['planned_span_m_median']} m.", '',
              f"Multi-target windows {d['multi_target_windows']}: opening one asked object leaves another asked object not inside the view in {d['windows_other_asked_not_inside']['1.0']} windows "
              f"(aspect 1.0) / {d['windows_other_asked_not_inside']['1.6']} (aspect 1.6); asked objects outside the first-model-object view (no `object` in the URL): "
              f"{json.dumps(d['asked_in_first_object_view']['1.0'], sort_keys=True)} (1.0), {json.dumps(d['asked_in_first_object_view']['1.6'], sort_keys=True)} (1.6).", '',
              f"Decided non-asked objects (the reviewer's confirmations beyond the targets): {d['decided_non_asked_total']} on {d['windows_with_decided_non_asked']} windows; "
              f"placement in the asked object's default view (aspect 1.0, pooled over asked views): {json.dumps(d['decided_non_asked_placement']['1.0'], sort_keys=True)}.", '',
              '### lot-* windows: planned lot span vs the house, the parcel fit and the asked objects', '']
    rows = [[w['scene'], w['rule'], w['planned_span_m'], w['lot_span_m'], w['house_extent_m'], w['lot_span_over_house'], w['house_pieces'], w['asked_count'],
             json.dumps(w['lot_fit_asked']) if w['lot_fit_asked'] is not None else '-', w['lot_fit_inside_image'],
             json.dumps(w['lot_fit_decided_non_asked']) if w['lot_fit_decided_non_asked'] is not None else '-',
             '; '.join(f"{a['id'][:8]} {a.get('default_view_span_m', {}).get('1.0')} m" for a in w['asked'] if not a.get('missing_geometry'))]
            for w in d['lot_windows']]
    lines.append(table(['window', 'rule', 'planned span m', 'lot span m', 'house extent m', 'lot / house', 'house pieces', 'asked', 'asked in lot fit', 'lot fit inside image',
                        'decided non-asked in lot fit', 'asked default view spans'], rows))
    lines += ['', '### Windows where an asked object lies outside or clipped by a default view (per window; the full list is census.json default_view.clipping_windows)', '']
    if d['clipping_windows']:
        per_window = defaultdict(lambda: {'asked': 0, 'pairs_1.0': 0, 'pairs_1.6': 0, 'first_1.0': 0, 'first_1.6': 0, 'lot_fit': 0, 'placements': Counter()})
        asked_by_window = {w['scene']: w['asked_count'] for w in d['windows_detail']}
        for c in d['clipping_windows']:
            entry = per_window[c['scene']]
            entry['asked'] = asked_by_window.get(c['scene'], 0)
            entry['placements'][c['placement']] += 1
            if c['view'] == 'asked_object_default':
                entry[f"pairs_{c['aspect']}"] += 1
            elif c['view'] == 'first_model_object_default':
                entry[f"first_{c['aspect']}"] += 1
            else:
                entry['lot_fit'] += 1
        rows = [[w, e['asked'], e['pairs_1.0'], e['pairs_1.6'], e['first_1.0'], e['first_1.6'], e['lot_fit'], json.dumps(dict(e['placements']), sort_keys=True)]
                for w, e in sorted(per_window.items())]
        lines.append(table(['window', 'asked', 'asked->other not inside (1.0)', '(1.6)', 'asked not inside first-object view (1.0)', '(1.6)', 'asked not inside lot fit', 'placements'], rows))
        lines.append('')
        lines.append(f"Windows listed: {len(per_window)} of {d['windows']}; the asked->other columns count ordered pairs (opening A leaves B not inside), so a window with n asked "
                     'objects can reach n(n-1).')
    else:
        lines.append('none')
    lines += ['', '### Named object 3 (the car)', '', d['car_reading'], '',
              '## Renders', '', f"{len(census['renders'])} renders under `renders/` (native x{RENDER_SCALE}, pieces in distinct colours with index / m2 / vertices / status, "
              f"occluders green, cluster hull dotted white, an editor default view dotted orange, 5 m bar); contact sheet `{census['contact_sheet']}`.", '',
              '## Open issues', '']
    lines += [f'- {issue}' for issue in census['open_issues']]
    lines += ['', '## Method notes', '',
              f"- Population: {census['population']['windows']} windows + {census['population']['review_set']} review-set scenes; superseded objects skipped; rejected objects "
              'excluded from geometry. Invalid rings made valid (counts per scene in census.json).',
              "- Same mask = equal (prompt, score) and source sam3-text-onnx; a 4 dp tie between two proposals is possible but rare.",
              "- Default view = objectImageFrame([review ring, model ring]) exactly as SceneCanvas computes it when `focused` (the review-mode default); the viewport aspect is "
              "unknown offline, so 1.0 (the 264 px fallback, strict) and 1.6 are both evaluated; a wider pane only widens the frame.",
              '- The lot fit is SceneCanvas\'s "Fit parcel" frame (parcel bbox +- 12 px) over the window\'s planned lots (window_frame.lots pfi, geometry from the frozen parcel caches, '
              'projected through the inverse of scene_confidence.geographic). It is a click, not the default.',
              '- Nothing under data/scenes, the registry, RULES / WINDOW / DESIGN or the eyes index was written.']
    path.write_text('\n'.join(lines)+'\n')


# --- main --------------------------------------------------------------------------------------------------------------------

def strip_private(value):
    if isinstance(value, dict):
        return {k: strip_private(v) for k, v in value.items() if not str(k).startswith('_')}
    if isinstance(value, list):
        return [strip_private(v) for v in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--skip-renders', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    RENDERS.mkdir(parents=True, exist_ok=True)
    parcels = parcel_index()
    scene_results = [census_scene(path, group, parcels) for path, group in population()]
    groups = Counter(s['summary']['group'] for s in scene_results)
    census = {'version': VERSION, 'written_at': datetime.now(UTC).isoformat(timespec='seconds'), 'code_sha256': CODE_SHA256,
              'population': {'windows': groups['windows'], 'review_set': groups['review_set'], 'parcel_pfis_indexed': len(parcels)},
              'constants': {'touch_m': TOUCH_M, 'band': BAND, 'occluder_kinds': OCCLUDER_KINDS, 'occluder_between': OCCLUDER_BETWEEN,
                            'occluder_over_m2': OCCLUDER_OVER_M2, 'simplify_px': SIMPLIFY_PX, 'vertex_marks': VERTEX_MARKS, 'editor_fit': EDITOR_FIT,
                            'sensitivity_buffer_m': SENSITIVITY_BUFFER_M, 'frame_tol_px': FRAME_TOL_PX},
              'fragmentation': {g: {layer: pooled_fragmentation(scene_results, g, layer) for layer in ('model', 'review')} for g in ('windows', 'review_set')},
              'vertex_load': {g: {layer: pooled_vertices(scene_results, g, layer) for layer in ('model', 'review')} for g in ('windows', 'review_set')},
              'simplification': {layer: simplification_summary([r for s in scene_results for r in s['simplify'] if r['layer'] == layer]) for layer in ('model', 'review')},
              'editor_over_100': {layer: dict(Counter(r['kind'] for s in scene_results if s['summary']['group'] == 'windows' for r in s['rows'] if r['layer'] == layer and r['live'] and r['vertices'] > 100)) for layer in ('model', 'review')},
              'editor_over_200': {layer: dict(Counter(r['kind'] for s in scene_results if s['summary']['group'] == 'windows' for r in s['rows'] if r['layer'] == layer and r['live'] and r['vertices'] > 200)) for layer in ('model', 'review')},
              'scenes': [s['summary'] for s in scene_results]}
    # (C) pooled
    views = [s['view'] for s in scene_results if s['view']]
    asked_entries = [a for v in views for a in v['asked'] if not a.get('missing_geometry')]
    readiness = load_json(READINESS) if READINESS.exists() else None
    agreement = None
    if readiness:
        ready_asked = {w['window_id']: sorted(a['id'] for a in w.get('asked', []) if a.get('status') != 'superseded') for w in readiness.get('windows', [])}
        ours = {v['scene']: sorted(a['id'] for a in v['asked']) for v in views}
        agreement = {'windows_compared': sum(1 for k in ours if k in ready_asked), 'equal': sum(1 for k in ours if k in ready_asked and ready_asked[k] == ours[k]),
                     'differ': [k for k in ours if k in ready_asked and ready_asked[k] != ours[k]], 'readiness_file': str(READINESS.relative_to(ROOT)),
                     'note': 'live ids only: the readiness file also lists superseded slices under asked'}
    clipping = []
    for v in views:
        for a in v['asked']:
            if a.get('missing_geometry'):
                continue
            for asp, ids in a['other_asked_not_inside'].items():
                for other in ids:
                    _other_row = next(x for x in v['asked'] if x['id'] == other)        # unused; kept so a missing id still raises
                    frame = v['_frames'][a['id']][asp]
                    clipping.append({'scene': v['scene'], 'view': 'asked_object_default', 'aspect': asp, 'opened_on': a['id'], 'object': other,
                                     'placement': placement(next(r['_pixel'] for s in scene_results if s['scene']['id'] == v['scene'] for r in s['rows'] if r['layer'] == 'review' and r['id'] == other), frame)})
            if a.get('in_first_object_view'):
                for asp, pl in a['in_first_object_view'].items():
                    if pl != 'inside':
                        clipping.append({'scene': v['scene'], 'view': 'first_model_object_default', 'aspect': asp, 'opened_on': v['first_model_object'], 'object': a['id'], 'placement': pl})
            if a.get('in_lot_fit') and a['in_lot_fit'] != 'inside':
                clipping.append({'scene': v['scene'], 'view': 'lot_fit_click', 'aspect': '-', 'opened_on': 'parcel', 'object': a['id'], 'placement': a['in_lot_fit']})
    car = next((s for s in scene_results if s['scene']['id'] == NAMED[2][0]), None)
    car_reading = 'scene missing'
    if car and car['view']:
        rows = {r['id']: r for r in car['rows'] if r['layer'] == 'review'}
        car_row = rows.get(NAMED[2][1])
        if car_row:
            parts = []
            for aid, fr in car['view']['_frames'].items():
                for asp, f in fr.items():
                    parts.append(f"{aid} aspect {asp}: car {placement(car_row['_pixel'], f)} (view {round(max(f[2], f[3])*car['summary']['pixel_m'], 1)} m)")
            lot = car['view']['_lot_fit']
            parts.append(f"lot fit: {placement(car_row['_pixel'], lot) if lot else 'no lot resolved (window rule ' + str(car['view']['rule']) + ')'}")
            car_reading = (f"`{NAMED[2][0]}` is a `{car['view']['identity']}` window (rule {car['view']['rule']}, planned span {car['view']['planned_span_m']} m, frame {car['view']['frame_px']}); "
                           f"its asked object(s) {[a['id'] for a in car['view']['asked']]}; the car `{NAMED[2][1]}` ({car_row['area_m2']} m2, {car_row['frame_distance_px']} px from the frame) is not asked. "
                           'Placement of the car in each default view: ' + '; '.join(parts) + '.')
    aspect_keys = [str(a) for a in EDITOR_FIT['aspects']]
    census['default_view'] = {
        'reading': ("Read from src/app/scenes/page.tsx and src/components/scenes/SceneCanvas.tsx: the page selects the URL's `object` (else model.objects[0]); the canvas "
                    "starts focused in review mode and frames objectImageFrame([current ring, model ring]) = the selected object's bounding box widened to "
                    "max(extent + 24 px, min(image, max(55 px, extent) x 1.6)) per axis, then to the viewport aspect, centred on the object (src/lib/scene-image-frame.ts). "
                    "A 512 px captured scene reserves extra room on a cut edge. \"Fit parcel\" (parcel bbox +- 12 px) and \"Whole scene\" are buttons. The initial zoom is "
                    "therefore fitted to the ASKED object, never to the window frame, the lot or the house; the lot planner's span only sets what imagery exists."),
        'windows': len(views), 'asked_objects': len(asked_entries), 'asked_roles': dict(Counter(a['role'] for a in asked_entries)),
        'asked_kinds': dict(Counter(a['kind'] for a in asked_entries)), 'asked_statuses': dict(Counter(a['status'] for a in asked_entries)),
        'asked_touching_frame': sum(1 for a in asked_entries if a['touches_frame']),
        'asked_view_span_m_median': median([a['default_view_span_m']['1.0'] for a in asked_entries]),
        'asked_view_span_m_p90': percentile([a['default_view_span_m']['1.0'] for a in asked_entries], 90),
        'planned_span_m_median': median([v['planned_span_m'] for v in views if v['planned_span_m']]),
        'multi_target_windows': sum(1 for v in views if v['asked_count'] >= 2),
        'windows_other_asked_not_inside': {k: sum(1 for v in views if any(a.get('other_asked_not_inside', {}).get(k) for a in v['asked'])) for k in aspect_keys},
        'asked_in_first_object_view': {k: dict(Counter((a.get('in_first_object_view') or {}).get(k) for a in asked_entries if a.get('in_first_object_view'))) for k in aspect_keys},
        'decided_non_asked_total': sum(v['decided_non_asked'] for v in views),
        'windows_with_decided_non_asked': sum(1 for v in views if v['decided_non_asked']),
        'decided_non_asked_placement': {k: dict(sum((Counter(a['decided_non_asked'][k]) for a in asked_entries), Counter())) for k in aspect_keys},
        'lot_windows': [strip_private(v) for v in views if v['identity'] == 'lot' or v['scene'].startswith('lot-')],
        'lot_fit_summary': {'lot_resolved': sum(1 for v in views if v['lot_resolved']), 'windows_with_lots': sum(1 for v in views if v['lots']),
                            'asked_in_lot_fit': dict(sum((Counter(v['lot_fit_asked'] or {}) for v in views), Counter())),
                            'decided_non_asked_in_lot_fit': dict(sum((Counter(v['lot_fit_decided_non_asked'] or {}) for v in views), Counter()))},
        'readiness_agreement': agreement, 'clipping_windows': clipping, 'car_reading': car_reading,
        'windows_detail': [strip_private(v) for v in views]}
    # named objects + worst clusters + renders
    named = []
    for scene_id, object_id, note in NAMED:
        figures = named_figures(scene_results, scene_id, object_id)
        figures['note'] = note
        figures['_note'] = note
        named.append(figures)
    worst = worst_clusters(scene_results)
    largest = worst_clusters(scene_results, key='pieces')
    renders, sheet_entries = [], []
    if not args.skip_renders:
        for stale in list(RENDERS.glob('cluster-*.png'))+list(RENDERS.glob('named-*.png'))+list(RENDERS.glob('contact-sheet.png')):
            stale.unlink()          # an earlier run's renders under this folder only; nothing else is ever removed
        for i, c in enumerate(worst):
            result = next(s for s in scene_results if s['scene']['id'] == c['scene'])
            out = RENDERS/f"cluster-{i+1:02d}-{c['scene']}-{c['largest_id'][:8]}.png"
            render_cluster(result, c, out)
            c['render'] = str(out.relative_to(ROOT))
            renders.append(c['render'])
            sheet_entries.append({'render': out, 'caption': f"worst {i+1}: {c['scene']} {c['pieces']} pieces ({c['fragment_pieces']} < 12 m2), {c['fragmentation_pairs']} fragmentation / {c['party_wall_pairs']} party-wall pairs, {c['area_m2']} m2"})
        for i, n in enumerate(named):
            if n.get('missing'):
                continue
            result = next(s for s in scene_results if s['scene']['id'] == n['scene'])
            out = RENDERS/f"named-{i+1}-{n['scene']}-{n['id'][:8]}.png"
            render_named(result, n, out)
            n['render'] = str(out.relative_to(ROOT))
            renders.append(n['render'])
            sheet_entries.append({'render': out, 'caption': f"named {i+1}: {n['scene']} {n['id'][:8]} {n['kind']} {n['area_m2']} m2 v{n['vertices']}: {n['note']}"})
        sheet = contact_sheet(sheet_entries, RENDERS/'contact-sheet.png')
        census['contact_sheet'] = str(sheet.relative_to(ROOT)) if sheet else None
    else:
        census['contact_sheet'] = None
        for c in worst:
            c['render'] = None
        for n in named:
            n['render'] = None
    census['named'] = [strip_private(n) for n in named]
    census['worst_clusters'] = [strip_private(c) for c in worst]
    census['largest_clusters'] = [strip_private(c) for c in largest]
    census['renders'] = renders
    census['open_issues'] = open_issues(census, views)
    (OUT/'census.json').write_text(json.dumps(census, indent=1, sort_keys=False, default=str))
    with open(OUT/'clusters.jsonl', 'w') as handle:
        for s in scene_results:
            for c in s['clusters']:
                if c['pieces'] >= 2:
                    handle.write(json.dumps(strip_private(c), default=str)+'\n')
    write_markdown(census, OUT/'census.md')
    print(json.dumps({'windows': groups['windows'], 'review_set': groups['review_set'],
                      'touching_pairs_windows_review': census['fragmentation']['windows']['review']['touching_pairs'],
                      'multi_clusters_windows_review': census['fragmentation']['windows']['review']['clusters_multi'],
                      'renders': len(renders), 'out': str(OUT)}))


def open_issues(census, views):
    issues = []
    f = census['fragmentation']['windows']['review']
    if f['pairs_ge_0.8']:
        issues.append(f"{f['pairs_ge_0.8']} touching pairs on the windows' review layer have the smaller piece covered >= 0.8 by the other: the extraction dedupe would have dropped "
                      'one of them, so they survive only through lineage (a carried slice beside a detection the takeover did not pair, e.g. shared_cover or detection_touches_frame).')
    agreement = census['default_view'].get('readiness_agreement') or {}
    if agreement.get('differ'):
        issues.append(f"asked-object sets differ from the 2026-10-06 00:35 readiness file on {len(agreement['differ'])} windows ({agreement['differ'][:6]}): later carries or saves; "
                      'this census reads review.json directly.')
    unresolved = [v['scene'] for v in views if v['lots'] and not v['lot_resolved']]
    if unresolved:
        issues.append(f"{len(unresolved)} windows name lots whose pfi is in no frozen parcel cache ({unresolved[:6]}): the lot fit could not be evaluated there.")
    issues.append('The viewport aspect is unknown offline; aspects 1.0 and 1.6 bracket the editor, and the SVG letterboxes (meet), so a view never shows less than the frame.')
    issues.append('Occluder-between uses a convex-hull bridge; for L-shaped neighbours the bridge includes yard area, so a tree in the yard notch can count as between.')
    issues.append('Same-mask identity is a (prompt, 4 dp score) tie; the review layer of a window holds carried copies with source-scene scores, never tied with window detections.')
    if any(n.get('missing') for n in census['named']):
        issues.append('A named object is missing from its review layer: ' + '; '.join(f"{n['scene']} {n['id']}: {n['missing']}" for n in census['named'] if n.get('missing')))
    return issues


if __name__ == '__main__':
    main()
