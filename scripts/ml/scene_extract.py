"""Whole-image object proposals in the same georeferenced frame as roof reviews.

SAM3 masks are suggestions. Class/dimension priors are explicitly labelled and
never imply measured heights. Model snapshots are content-addressed; reviews
live separately and cannot be overwritten by extraction.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely import make_valid
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .sam3 import Sam3TextSegmenter
from .segmentation import iter_polygons, mask_to_polygons

PROMPTS = {'tree': 'tree canopy', 'road': 'road', 'car': 'car', 'building': 'building roof',
           'solar_panel': 'solar panel', 'pool': 'swimming pool', 'driveway': 'driveway',
           'footpath': 'sidewalk', 'vegetation': 'grass', 'water': 'water'}
HEIGHTS = {'tree': 7., 'road': .08, 'car': 1.5, 'building': 3.2, 'solar_panel': .15,
           'pool': .1, 'driveway': .05, 'footpath': .15, 'vegetation': .3, 'water': .02, 'other': 1.}


def polygon_details(ring, image, affine, kind, holes=None):
    polygon = Polygon(ring, holes)
    xy = np.c_[ring, np.ones(len(ring))] @ affine
    metric = Polygon(xy, [np.c_[h, np.ones(len(h))] @ affine for h in holes or []])
    corners = list(metric.minimum_rotated_rectangle.exterior.coords)
    lengths = [np.linalg.norm(np.subtract(corners[i + 1], corners[i])) for i in range(4)]
    longest = int(np.argmax(lengths))
    a, b = corners[longest], corners[longest + 1]
    mask = Image.new('L', image.size)
    ImageDraw.Draw(mask).polygon(ring, fill=255)
    for hole in holes or []:
        ImageDraw.Draw(mask).polygon([tuple(p) for p in hole], fill=0)
    pixels = np.asarray(image)[np.asarray(mask) > 0]
    rgb = np.median(pixels, axis=0).astype(int) if len(pixels) else [128, 128, 128]
    height = HEIGHTS.get(kind, 1.)
    if kind == 'tree':
        height = round(float(np.clip(np.sqrt(metric.area) * 1.15, 2., 24.)), 1)
    return {'height_m': height, 'base_m': 0., 'height_source': 'class-prior', 'height_verified': False,
            'width_m': round(float(min(lengths)), 2), 'length_m': round(float(max(lengths)), 2),
            'area_m2': round(metric.area, 2), 'rotation_deg': round(float(np.degrees(np.arctan2(b[1]-a[1], b[0]-a[0]))), 1),
            'color': '#'+''.join(f'{v:02x}' for v in rgb), 'footprint_area_px': round(polygon.area, 2)}


def group_objects(objects):
    buildings = [o for o in objects if o['kind'] == 'building']
    # A generated building group includes its owner, not just attached panels.
    # This runs on extraction proposals; existing CRM memberships are separate.
    for building in buildings:
        building['group_id'] = building['id']
    for obj in objects:
        if obj['kind'] not in ('solar_panel', 'skylight', 'chimney'):
            continue
        polygon = Polygon(obj['ring'], obj.get('holes'))
        matches = [(Polygon(b['ring'], b.get('holes')).intersection(polygon).area / max(polygon.area, .01), b) for b in buildings]
        if matches:
            overlap, parent = max(matches, key=lambda item: item[0])
            if overlap >= .65:
                obj['parent_id'] = parent['id']
                obj['group_id'] = parent['id']
                obj['model']['base_m'] = parent['model']['height_m']
    return [{'id': b['id'], 'label': b['label']} for b in buildings]


CANOPY_KINDS = ('tree', 'vegetation')
# Canopy fragment merge (design values, not tuned on the reviewer's set): a proposed tree or vegetation polygon smaller
# than fragment_m2 whose area is >= cover covered by a live or proposed canopy polygon >= container_ratio times its
# size is one bush split by the model, not another object. No distance-based or same-kind closure merging exists
# here (a 0.5 m same-kind rule would rewrite 10 of 15 vegetation objects on roof_training_250-30371).
CANOPY_MERGE = {'fragment_m2': 1., 'cover': .9, 'container_ratio': 10.}
# Post-assembly floor: a building polygon under this many square metres is dropped after refine/assemble (the pixel
# floor at mask_to_polygons/`polygon.area < 5` keeps sub-metre rings alive at fine pixel sizes).
BUILDING_FLOOR_M2 = 1.


def _protected(obj):
    """Objects extraction never modifies: reviewed geometry, CRM/roof-pipeline sources, reviewer roof feedback or line edits."""
    return bool(obj.get('geometry_reviewed') or obj.get('roof_feedback') or obj.get('roof_line_edits')
                or obj.get('source') in ('roof-pipeline', 'crm-roof-pipeline'))


def merge_canopy_fragments(proposed, retained, to_meters):
    """Drop proposed canopy fragments inside a much larger canopy polygon; the container records them in `replaces`.

    proposed: new tree|vegetation objects of this scan; retained: objects already in the scene (any kind). Returns
    (kept proposed, {fragment_id: container_id}). Containers that are protected (see _protected) are never modified,
    so their fragments stay. Cross-kind containment (vegetation inside tree) counts; pure and order-stable.
    """
    pixel_area = abs(float(np.linalg.det(np.asarray(to_meters, dtype=float)[:2])))
    candidates = [o for o in retained if o['kind'] in CANOPY_KINDS and o.get('status') not in ('rejected', 'superseded')]
    containers = []
    for obj in [*candidates, *proposed]:
        try:
            polygon = Polygon(obj['ring'], obj.get('holes') or [])
        except (ValueError, TypeError):
            continue
        if polygon.is_valid and polygon.area > 0:
            containers.append((obj, polygon))
    replaces, kept = {}, []
    for obj in proposed:
        polygon = Polygon(obj['ring'], obj.get('holes') or [])
        area_m2 = polygon.area*pixel_area
        container = None
        if polygon.is_valid and 0 < area_m2 < CANOPY_MERGE['fragment_m2']:
            for other, shape in containers:
                if other is obj or other['id'] == obj['id'] or other['id'] in replaces or shape.area < CANOPY_MERGE['container_ratio']*polygon.area:
                    continue
                if polygon.intersection(shape).area/polygon.area >= CANOPY_MERGE['cover']:
                    container = other if not _protected(other) else None
                    if container is None:
                        break   # a protected container keeps the fragment visible rather than editing the container
                    break
        if container is None:
            kept.append(obj)
            continue
        replaces[obj['id']] = container['id']
        container['replaces'] = sorted({*(container.get('replaces') or []), obj['id']})
    # A container that was itself merged passes its fragments up; ids stay stable.
    for fragment, container in list(replaces.items()):
        while container in replaces:
            container = replaces[container]
        replaces[fragment] = container
    return kept, replaces


def apply_building_floor(candidates, pixel_area, floor_m2=BUILDING_FLOOR_M2):
    """Drop assembled building candidates under floor_m2 (pixel area x |det(to_meters[:2])|); pure.

    The pixel floor at mask_to_polygons keeps rings alive that refine/assemble later shrink below a square metre
    (roof_training_250-29501 587e522a2986ccea: 0.04 m² today).
    """
    return [c for c in candidates if c[0].area*pixel_area >= floor_m2]


LINEAR_KINDS = ('road', 'footpath', 'driveway')
# Linear-feature continuity merge. REVIEWER AUTHORISATION (verbatim, 2026-10-03 15:10 AEST): "yes to defaults" on
# data/reviews/eyes/experiments/linear-continuity-2026-10-03/DECISION-CARD.md. Design values, not tuned on the reviewer's set.
# `enabled` False: merge_linear_fragments is NOT called from the extraction path (extract_objects, analyse_scene, build_scene
# or any other writer of latest.json) until the sidecar right/wrong tally on the 9 rendered windows gates promotion; promotion
# itself is a dated RULES / DESIGN note plus a re-extract of windows only. Until then the function runs in sidecars
# (scripts/ml/experiments/) and tests only. Step 1 re-joins the components of ONE SAM3 mask (same kind + score to score_round
# dp: 533 of 1055 window pieces, diagnosis cause 1); step 2 is the guarded cross-mask join of the kind's remaining candidates.
# d_m is the closing radius and the largest gap tried (closed = (a (+) d | b (+) d) (-) d in metres; a pair further apart than
# d_m is never tried). Guards in order: bridge area <= bridge_max_ratio x the smallest ORIGINAL PIECE the bridge touches (the
# card's "2 x the smaller piece"; the wrong prototype joins ran 7x to 20x; per piece, not per agglomerated side, since the
# 2026-10-03 repair r2: a speck welded onto a body must not anchor a bridge 7x its own size); feature area <= union_ratio_max x the UNION, not the sum, of every original piece (cumulative: a chain of specks cannot
# build a bridge network); the bridge may not touch a live car / tree / vegetation / pool object or a live piece of another
# linear class (23 of 38 prototype bridges did); a reviewer-decided object of any class blocks a bridge; the ring must survive
# rounding to ring_decimals + make_valid within valid_area_tol (23 of 200 prototype rings were invalid). Candidates only:
# confirmed / needs_adjustment / rejected / superseded pieces never merge. Members stay in the list as status superseded with
# replacement_ids = [feature id] and the feature carries replaces / derived_from / merged_from (the whole-roof takeover
# convention, scene_focus.TAKEOVER_RULE), so lineage is explicit and reversible. Training labels stay pieces.
# REVIEWER AUTHORISATION (verbatim, 2026-10-03 19:40 AEST): "yes to defaults" on the marks card (docs/handover-2026-10-01.md
# section 17 "Card for the reviewer"), adopting the skeptic reading of run-v2/MARKS-2026-10-03.md (right 3, 4, 7; wrong 2, 5, 6, 8;
# unclear 1) and the card's defaults: (2) RESULT FLOOR result_floor_m2 = 1: a merged feature must reach 1 m2 AND its largest member
# must reach 1 m2, else the whole cluster is refused ('result_floor', checked before the feature is emitted, so a refused floor
# leaves the pieces exactly as they came in; the single-piece 1 m2 metric floor stays deferred); (3) material-edge adjacency
# (line 6) is accepted as the lane's apron, no surface test; (4) blocker_kinds gain water and building (line 2's bridge lay inside
# a 48 m2 water candidate); (5) a merged ring over 150 vertices is simplified at 2 px exactly as the extraction path does
# (simplify_long_ring, the budget of extract_objects), and a feature still over the 200-vertex cap afterwards is REPORTED under
# 'vertex_cap', never dropped; (6) promotion = dated RULES / DESIGN notes, the join enabled for WINDOW re-extracts only through an
# explicit opt-in (`enabled` here stays False), the 99 review-set scenes untouched.
# 2026-10-03 rings (the 53-window promotion, 5 refused by the app at the module's own scratch POST): the ring must also pass the
# app's outline rules (app_ring_issues, the port of src/lib/scene-types.ts polygonIssues) after the two lossless repairs of
# finalise_app_ring; otherwise 'app_ring_rules'. Same counting bar, same design values; the repairs change no area.
LINEAR_MERGE = {'kinds': LINEAR_KINDS, 'd_m': {'driveway': .6, 'footpath': .8, 'road': 1.},
                'bridge_max_ratio': 2., 'union_ratio_max': 1.15,
                'blocker_kinds': ('car', 'tree', 'vegetation', 'pool', 'water', 'building'),
                'decided_statuses': ('confirmed', 'rejected', 'needs_adjustment', 'superseded'), 'result_floor_m2': 1.,
                'score_round': 4, 'touch_tol_m': .02, 'touch_eps_m2': 1e-3, 'weld_m': 1e-3, 'valid_area_tol': .005, 'ring_decimals': 2,
                'source': 'linear-merge-v1', 'enabled': False}
LINEAR_REFUSALS = ('no_bridge', 'not_connected', 'bridge_too_large', 'union_ratio', 'touches_blocker', 'touches_decided',
                   'invalid_after_rounding', 'app_ring_rules', 'result_floor')
# The extraction path's ring budget (extract_objects, inline): a detector ring over `vertices` exterior vertices, or with a hole
# over `hole_vertices`, is simplified at `tolerance_px` with topology preserved; a rounded ring still over `cap` vertices (or more
# than `max_holes` holes, a hole over `cap`, or over `total` coordinates) is dropped there. Merged linear features reuse the same
# simplification through simplify_long_ring and REPORT what still exceeds the cap (exceeds_vertex_cap) instead of dropping it.
RING_SIMPLIFY = {'vertices': 150, 'hole_vertices': 200, 'tolerance_px': 2., 'cap': 200, 'max_holes': 32, 'total': 2000}


def simplify_long_ring(polygon, *, budget=RING_SIMPLIFY):
    """extract_objects' ring simplification as a helper: a polygon whose exterior exceeds budget['vertices'] vertices or whose
    hole exceeds budget['hole_vertices'] comes back as polygon.simplify(budget['tolerance_px'], preserve_topology=True); any
    other polygon is returned as the same object."""
    if len(polygon.exterior.coords)-1 > budget['vertices'] or any(len(h.coords)-1 > budget['hole_vertices'] for h in polygon.interiors):
        return polygon.simplify(budget['tolerance_px'], preserve_topology=True)
    return polygon


def exceeds_vertex_cap(ring, holes, *, budget=RING_SIMPLIFY):
    """extract_objects' drop condition on a rounded ring + holes; the linear merge reports it rather than dropping."""
    return (len(ring) > budget['cap'] or len(holes) > budget['max_holes'] or any(len(h) > budget['cap'] for h in holes)
            or len(ring)+sum(map(len, holes)) > budget['total'])


# --- the app's outline rules, ported (2026-10-03 rings session) ------------------------------------------------------------
# src/lib/scene-types.ts polygonIssues is what POST /api/scenes runs on every object (scene-store.ts:353, with the model's
# image_width / image_height). Five of the 53 windows' merged rings passed shapely's is_valid on 2026-10-03 and were refused
# there: three 'Outline points overlap' (a consecutive duplicate vertex after 2 dp rounding, which GEOS allows and the app does
# not; win-150d924743036f24, win-b6901e4edcbc5495, win-bebda78b7127ffae) and two 'Outline edges cross' (a vertex lying on a
# non-adjacent edge, a zero-width collinear back-track; win-3fa3eb10672b7241, win-c2a076d238992634). The rules are ported here
# rule for rule with the app's tolerances (app_ring_issues) so a merged ring is judged by them before anything leaves the
# module; the two lossless repairs (drop a consecutive duplicate; drop a vertex collinear with both neighbours) run first
# (finalise_app_ring) and a ring that still fails is refused 'app_ring_rules', its pieces left exactly as they came in.
APP_RING_RULES = {'overlap_px': 1e-6, 'turn_tol': 1e-8, 'min_points': 3, 'max_points': 200, 'max_holes': 32, 'total_points': 2000,
                  'min_area_px2': 1., 'hole_min_area_px2': 1e-4}
# The three count rules relaxed (None): the merge judges GEOMETRY here and reports a ring over the cap under vertex_cap instead
# of dropping it (19:40 card item 5, exceeds_vertex_cap); the promotion refuses it (scene_linear_promote too_many_vertices).
APP_RING_RULES_UNCAPPED = {**APP_RING_RULES, 'max_points': None, 'max_holes': None, 'total_points': None}
APP_ISSUE_POINTS = 'Use at least three finite outline points'
APP_ISSUE_BOUNDS = 'Keep the outline inside the image'
APP_ISSUE_AREA = 'The outline needs a visible area'
APP_ISSUE_OVERLAP = 'Outline points overlap; remove the duplicate point'
APP_ISSUE_CROSS = 'Outline edges cross; move or remove a point'
APP_ISSUE_BUDGET = 'Use at most 32 interior gaps and 2,000 total outline points'
APP_ISSUE_GAP_OUTSIDE = 'Gap {n} must stay inside the outer outline without crossing or sharing an edge'
APP_ISSUE_GAP_DISCONNECT = 'Interior gaps cannot disconnect the outline'
APP_ISSUE_GAP_OVERLAP = 'Interior gaps cannot overlap or contain one another'
APP_ISSUE_REMAINING_AREA = 'The remaining outline needs a visible area'


def _turn(a, b, c):
    """The app's turn(a, b, c): the cross product (b - a) x (c - a) in px^2."""
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _on_segment(a, b, p, tol):
    """The app's onSegment: |turn| < tol and p inside the segment's bounding box widened by tol."""
    return (abs(_turn(a, b, p)) < tol and min(a[0], b[0])-tol <= p[0] <= max(a[0], b[0])+tol
            and min(a[1], b[1])-tol <= p[1] <= max(a[1], b[1])+tol)


def _same_point(a, b, tol):
    return math.hypot(a[0]-b[0], a[1]-b[1]) < tol


def _ring_area_px(ring):
    """The app's ringArea: |shoelace| / 2."""
    return abs(sum(p[0]*q[1]-q[0]*p[1] for p, q in zip(ring, ring[1:]+ring[:1], strict=True)))/2.


def _finite_point(p):
    return (isinstance(p, (list, tuple)) and len(p) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p))


def _any_points_overlap(ring, tol):
    """The app's overlap rule: ANY two vertices of the ring (not only consecutive ones) closer than tol."""
    return any(_same_point(ring[i], ring[j], tol) for i in range(len(ring)) for j in range(i+1, len(ring)))


def _edges_cross(ring, tol):
    """The app's non-adjacent edge test: a proper crossing (both straddle products under -tol) or an endpoint of either
    edge on the other segment; edges i and i+1 (and the first with the last) are adjacent and never compared."""
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i+1) % n]
        for j in range(i+2, n):
            if i == 0 and j == n-1:
                continue
            c, d = ring[j], ring[(j+1) % n]
            if (_turn(a, b, c)*_turn(a, b, d) < -tol and _turn(c, d, a)*_turn(c, d, b) < -tol) \
                    or _on_segment(a, b, c, tol) or _on_segment(a, b, d, tol) or _on_segment(c, d, a, tol) or _on_segment(c, d, b, tol):
                return True
    return False


def _contains(p, boundary):
    """The app's even-odd contains."""
    inside = False
    j = len(boundary)-1
    for i in range(len(boundary)):
        a, b = boundary[i], boundary[j]
        if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < (b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]:
            inside = not inside
        j = i
    return inside


def _on_boundary(p, boundary, tol):
    return any(_on_segment(boundary[i], boundary[(i+1) % len(boundary)], p, tol) for i in range(len(boundary)))


def _samples(boundary):
    """The app's samples: every vertex and every edge midpoint."""
    return [s for p, q in zip(boundary, boundary[1:]+boundary[:1], strict=True) for s in (p, [(p[0]+q[0])/2., (p[1]+q[1])/2.])]


def _contacts(a, b, tol, overlap):
    """The app's contacts(a, b): the isolated contact points of two rings; None on a proper crossing or a shared edge."""
    points = []
    for i in range(len(a)):
        p, s = a[i], a[(i+1) % len(a)]
        for j in range(len(b)):
            q, t = b[j], b[(j+1) % len(b)]
            if _turn(p, s, q)*_turn(p, s, t) < -tol and _turn(q, t, p)*_turn(q, t, s) < -tol:
                return None
            shared = []
            for v in (p, s, q, t):
                if _on_segment(p, s, v, tol) and _on_segment(q, t, v, tol) and not any(_same_point(v, w, overlap) for w in shared):
                    shared.append(v)
            if len(shared) > 1:
                return None
            for v in shared:
                if not any(_same_point(v, w, overlap) for w in points):
                    points.append(v)
    return points


def app_ring_issues(ring, holes=(), width=None, height=None, *, minimum_area=None, rules=APP_RING_RULES):
    """src/lib/scene-types.ts polygonIssues(ring, width, height, holes, minimumArea) rule for rule, in the app's order and in
    the app's words; [] = the app's save would accept the outline. ring / holes are pixel points as the scenes store them.
    width / height None skips the bounds rule only (the merge has no image: a closing of pieces inside the image stays inside
    it). rules = APP_RING_RULES are the app's constants; APP_RING_RULES_UNCAPPED skips the three count rules (see there)."""
    minimum_area = rules['min_area_px2'] if minimum_area is None else minimum_area
    overlap, tol = rules['overlap_px'], rules['turn_tol']
    if (not isinstance(ring, (list, tuple)) or len(ring) < rules['min_points']
            or (rules['max_points'] is not None and len(ring) > rules['max_points']) or not all(_finite_point(p) for p in ring)):
        return [APP_ISSUE_POINTS]
    ring = [list(p) for p in ring]
    errors = []
    if width is not None and height is not None and any(p[0] < 0 or p[1] < 0 or p[0] > width or p[1] > height for p in ring):
        errors.append(APP_ISSUE_BOUNDS)
    if _ring_area_px(ring) < minimum_area:
        errors.append(APP_ISSUE_AREA)
    if _any_points_overlap(ring, overlap):
        return [*errors, APP_ISSUE_OVERLAP]
    if _edges_cross(ring, tol):
        return [*errors, APP_ISSUE_CROSS]
    holes = list(holes or [])
    if ((rules['max_holes'] is not None and len(holes) > rules['max_holes']) or any(not isinstance(h, (list, tuple)) for h in holes)
            or (rules['total_points'] is not None and len(ring)+sum(len(h) for h in holes) > rules['total_points'])):
        return [*errors, APP_ISSUE_BUDGET]
    holes = [[list(p) for p in h] for h in holes]
    # The app's union-find over outline, gaps and their contact points: a cycle of contacts closes off a separate region.
    parents = list(range(len(holes)+1))
    contact_points, edges = [], set()

    def root(node):
        while parents[node] != node:
            node = parents[node]
        return node

    def connect(a, b, points):
        for point in points:
            index = next((k for k, p in enumerate(contact_points) if _same_point(p, point, overlap)), -1)
            if index < 0:
                index = len(contact_points)
                contact_points.append(point)
                parents.append(len(parents))
            node = len(holes)+1+index
            for boundary in (a, b):
                edge = f'{boundary}:{node}'
                if edge in edges:
                    continue
                edges.add(edge)
                first, second = root(boundary), root(node)
                if first == second:
                    return False
                parents[first] = second
        return True

    for i, hole in enumerate(holes):
        issue = app_ring_issues(hole, [], width, height, minimum_area=rules['hole_min_area_px2'], rules=rules)
        if issue:
            return [*errors, f'Gap {i+1}: {issue[0]}']
        outer = _contacts(hole, ring, tol, overlap)
        if outer is None or any(not _contains(p, ring) and not _on_boundary(p, ring, tol) for p in _samples(hole)):
            return [*errors, APP_ISSUE_GAP_OUTSIDE.format(n=i+1)]
        if not connect(i+1, 0, outer):
            return [*errors, APP_ISSUE_GAP_DISCONNECT]
        for j in range(i):
            shared = _contacts(hole, holes[j], tol, overlap)
            if (shared is None or any(_contains(p, holes[j]) and not _on_boundary(p, holes[j], tol) for p in _samples(hole))
                    or any(_contains(p, hole) and not _on_boundary(p, hole, tol) for p in _samples(holes[j]))):
                return [*errors, APP_ISSUE_GAP_OVERLAP]
            if not connect(i+1, j+1, shared):
                return [*errors, APP_ISSUE_GAP_DISCONNECT]
    if _ring_area_px(ring)-sum(_ring_area_px(h) for h in holes) < minimum_area:
        errors.append(APP_ISSUE_REMAINING_AREA)
    return list(dict.fromkeys(errors))


def _repair_ring(points, *, overlap, turn_tol):
    """The two lossless repairs on one ring, repeated until stable: a consecutive duplicate vertex (the first / last pair
    included) is dropped; a vertex collinear with both its neighbours within turn_tol (a straight-through point, or the tip of a
    zero-width back-track spike) is dropped. The point set changes by zero-area pieces only. Returns (points, dropped counts)."""
    pts = [list(p) for p in points]
    dropped = {'duplicate': 0, 'collinear': 0}
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        kept = []
        for p in pts:
            if kept and _same_point(kept[-1], p, overlap):
                dropped['duplicate'] += 1
                changed = True
            else:
                kept.append(p)
        while len(kept) > 1 and _same_point(kept[0], kept[-1], overlap):
            kept.pop()
            dropped['duplicate'] += 1
            changed = True
        pts = kept
        i = 0
        while len(pts) >= 3 and i < len(pts):
            if abs(_turn(pts[i-1], pts[i], pts[(i+1) % len(pts)])) < turn_tol:
                del pts[i]
                dropped['collinear'] += 1
                changed = True
            else:
                i += 1
    return pts, dropped


def finalise_app_ring(ring, holes=(), width=None, height=None, *, rules=APP_RING_RULES_UNCAPPED, drop_collapsed_holes=False):
    """A rounded pixel ring + holes made ready for the app: _repair_ring on the ring and on every hole, then app_ring_issues.
    Returns (ring, holes, issues, repairs): issues = the app's messages on the repaired outline ([] = accepted); repairs = None
    when nothing was dropped, else {'dropped': n, 'ring': {duplicate, collinear, vertices_before, vertices_after},
    'holes': [the same per repaired hole, with its index], 'holes_dropped': [...]}. A hole left with fewer than three points by
    the repairs had zero area (every point duplicate or collinear): drop_collapsed_holes True removes it (reported under
    holes_dropped, the app's own repairHoles drops such slivers), False keeps it so app_ring_issues reports it ('Gap n: ...')."""
    overlap, tol = rules['overlap_px'], rules['turn_tol']
    ring_out, ring_dropped = _repair_ring(ring, overlap=overlap, turn_tol=tol)
    holes_in = list(holes or [])
    repaired = [_repair_ring(h, overlap=overlap, turn_tol=tol) for h in holes_in]
    collapsed = [k for k, (out, _) in enumerate(repaired) if len(out) < 3] if drop_collapsed_holes else []
    holes_out = [out for k, (out, _) in enumerate(repaired) if k not in collapsed]
    issues = app_ring_issues(ring_out, holes_out, width, height, rules=rules)
    total = sum(ring_dropped.values())+sum(sum(d.values()) for _, d in repaired)+len(collapsed)
    repairs = None if not total else {
        'dropped': total,
        'ring': {**ring_dropped, 'vertices_before': len(ring), 'vertices_after': len(ring_out)},
        'holes': [{'index': k, **d, 'vertices_before': len(h), 'vertices_after': len(out)}
                  for k, (h, (out, d)) in enumerate(zip(holes_in, repaired, strict=True)) if sum(d.values())],
        **({'holes_dropped': [{'index': k, 'vertices_before': len(holes_in[k]), 'points_after_repair': len(repaired[k][0])} for k in collapsed]}
           if collapsed else {})}
    return ring_out, holes_out, issues, repairs


def _degenerate(ring, holes):
    """True when shapely could not even build the outline (a ring or hole under three points): judged by app_ring_issues, never
    handed to Polygon()."""
    return len(ring) < 3 or any(len(h) < 3 for h in holes)


def _simplified_feature_ring(ring, holes, decimals, *, budget=RING_SIMPLIFY, image_size=None):
    """simplify_long_ring on a merged feature's pixel ring, rounded to `decimals` as the extraction path rounds. Returns
    (ring, holes, record | None): record None when the ring was under the budget; a simplification whose result is not one
    valid polygon, or fails the app's outline rules after finalise_app_ring (record app_issues), is not applied (record
    applied False) and the unsimplified ring is kept."""
    polygon = Polygon(ring, holes)
    simplified = simplify_long_ring(polygon, budget=budget)
    if simplified is polygon:
        return ring, holes, None
    record = {'vertices_before': len(ring), 'tolerance_px': budget['tolerance_px']}
    if simplified.geom_type != 'Polygon' or simplified.is_empty:
        return ring, holes, {**record, 'vertices_after': len(ring), 'applied': False}
    new_ring = [[round(float(x), decimals), round(float(y), decimals)] for x, y in simplified.exterior.coords[:-1]]
    new_holes = [[[round(float(x), decimals), round(float(y), decimals)] for x, y in h.coords[:-1]] for h in simplified.interiors]
    candidate = Polygon(new_ring, new_holes) if len(new_ring) >= 3 else None
    if candidate is None or not candidate.is_valid or candidate.is_empty:
        return ring, holes, {**record, 'vertices_after': len(ring), 'applied': False}
    new_ring, new_holes, issues, repairs = finalise_app_ring(new_ring, new_holes, *(image_size or (None, None)))
    if issues or _degenerate(new_ring, new_holes) or not Polygon(new_ring, new_holes).is_valid:
        return ring, holes, {**record, 'vertices_after': len(ring), 'applied': False, 'app_issues': issues or ['shapely: invalid after repair']}
    return new_ring, new_holes, {**record, 'vertices_after': len(new_ring), 'applied': True,
                                 'area_change_pct': round((candidate.area-polygon.area)/polygon.area*100., 3) if polygon.area else None,
                                 **({'repairs': repairs} if repairs else {})}


def _metric_shape(obj, affine):
    """The stored ring (+ holes) as a valid shapely geometry in metres, or None when the ring is unusable."""
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None

    def transform(points):
        pts = np.asarray(points, dtype=float)[:, :2]
        return np.c_[pts, np.ones(len(pts))] @ affine
    try:
        shape = make_valid(Polygon(transform(ring), [transform(h) for h in obj.get('holes') or [] if len(h) >= 3]))
    except (ValueError, TypeError):
        return None
    parts = [p for p in iter_polygons(shape) if p.area > 0]
    if not parts:
        return None
    shape = unary_union(parts)
    return None if shape.is_empty else shape


def _ring_details(ring, holes, affine, kind):
    """polygon_details without the image: metric area / sides / rotation of a pixel ring; colour comes from a member."""
    pixel = Polygon(ring, holes)
    xy = np.c_[np.asarray(ring, dtype=float), np.ones(len(ring))] @ affine
    metric = Polygon(xy, [np.c_[np.asarray(h, dtype=float), np.ones(len(h))] @ affine for h in holes or []])
    corners = list(metric.minimum_rotated_rectangle.exterior.coords)
    lengths = [np.linalg.norm(np.subtract(corners[i + 1], corners[i])) for i in range(4)]
    longest = int(np.argmax(lengths))
    a, b = corners[longest], corners[longest + 1]
    return {'height_m': HEIGHTS.get(kind, 1.), 'base_m': 0., 'height_source': 'class-prior', 'height_verified': False,
            'width_m': round(float(min(lengths)), 2), 'length_m': round(float(max(lengths)), 2),
            'area_m2': round(metric.area, 2), 'rotation_deg': round(float(np.degrees(np.arctan2(b[1]-a[1], b[0]-a[0]))), 1),
            'footprint_area_px': round(pixel.area, 2)}


def _linear_bridge(a, b, gap_m, d_m, pieces, blockers, decided, inverse, design, image_size=None):
    """One guarded closing of two linear clusters in metres. Returns (accepted: dict | None, record: dict).

    a, b: cluster dicts (geom in metres, pieces = original member shapes); pieces: every original piece of the would-be
    feature (the 2x rule reads the smallest piece the bridge touches; the union ratio is cumulative); blockers: (id, kind, shape) live car/tree/vegetation/pool and other-linear-class
    objects; decided: (id, kind, status, shape) reviewer-decided objects of any class; inverse: metres -> pixels (3x3);
    image_size: (width, height) px for the app's bounds rule, None skips that one rule.
    """
    record = {'a_ids': sorted(a['ids']), 'b_ids': sorted(b['ids']), 'gap_m': round(float(gap_m), 3)}
    ga, gb = a['geom'], b['geom']
    sides = unary_union([ga, gb])
    closed = unary_union([ga.buffer(d_m, quad_segs=8), gb.buffer(d_m, quad_segs=8)]).buffer(-d_m, quad_segs=8)
    tol, eps = design['touch_tol_m'], design['touch_eps_m2']
    connecting = [g for g in iter_polygons(closed.difference(sides))
                  if g.area > eps and g.distance(ga) <= tol and g.distance(gb) <= tol]
    if not connecting:
        return None, {**record, 'reason': 'no_bridge'}
    bridge = unary_union(connecting)
    merged = unary_union([sides, bridge])
    if merged.geom_type != 'Polygon':
        # A piece smaller than the closing radius comes back from the buffer round trip slightly inside its own outline, so the
        # fill meets it at points, not along an edge, and the union keeps it apart (28 of 32 refusals in the first probe run).
        # A weld_m (1 mm) dilation of the fill restores the shared edge; the bridge area and guards use the undilated fill.
        merged = unary_union([sides, bridge.buffer(design['weld_m'], join_style='mitre')])
        if merged.geom_type != 'Polygon':
            return None, {**record, 'reason': 'not_connected'}
    # The 2x rule is measured against the smallest original piece the bridge touches (both sides are unions of pieces; a
    # bridge that reaches a 0.26 m2 speck welded onto a 90 m2 cluster is still a speck-anchored bridge).
    touched = [p.area for p in pieces if p.distance(bridge) <= tol]
    smaller = min(touched) if touched else 0.
    pieces_union = unary_union(pieces).area
    record.update({'bridge_m2': round(float(bridge.area), 3), 'smallest_piece_m2': round(float(smaller), 3),
                   'smaller_side_m2': round(float(min(ga.area, gb.area)), 3),
                   'bridge_ratio': round(float(bridge.area/smaller), 3) if smaller else None,
                   'feature_m2': round(float(merged.area), 3), 'pieces_union_m2': round(float(pieces_union), 3),
                   'union_ratio': round(float(merged.area/pieces_union), 4) if pieces_union else None})
    if not smaller or bridge.area > design['bridge_max_ratio']*smaller:
        return None, {**record, 'reason': 'bridge_too_large'}
    if not pieces_union or merged.area > design['union_ratio_max']*pieces_union:
        return None, {**record, 'reason': 'union_ratio'}
    hits = sorted((oid, kind) for oid, kind, shape in blockers if bridge.intersection(shape).area > eps)
    if hits:
        return None, {**record, 'reason': 'touches_blocker', 'blockers': [{'id': i, 'kind': k} for i, k in hits]}
    hits = sorted((oid, kind, status) for oid, kind, status, shape in decided if bridge.intersection(shape).area > eps)
    if hits:
        return None, {**record, 'reason': 'touches_decided', 'blockers': [{'id': i, 'kind': k, 'status': s} for i, k, s in hits]}
    decimals = design['ring_decimals']

    def to_pixels(coords):
        pts = np.c_[np.asarray(coords, dtype=float), np.ones(len(coords))] @ inverse
        return [[round(float(x), decimals), round(float(y), decimals)] for x, y in pts[:, :2]]
    ring = to_pixels(merged.exterior.coords[:-1])
    holes = [to_pixels(h.coords[:-1]) for h in merged.interiors]
    rounded = Polygon(ring, holes)
    valid = make_valid(rounded)
    parts = sorted((p for p in iter_polygons(valid) if p.area > 0), key=lambda p: -p.area)
    reference = abs(rounded.area)
    if not parts or not reference or abs(sum(p.area for p in parts)-reference) > design['valid_area_tol']*reference \
            or sum(p.area for p in parts[1:]) > design['valid_area_tol']*reference:
        return None, {**record, 'reason': 'invalid_after_rounding'}
    main = parts[0]
    ring = [[round(float(x), decimals), round(float(y), decimals)] for x, y in main.exterior.coords[:-1]]
    holes = [[[round(float(x), decimals), round(float(y), decimals)] for x, y in h.coords[:-1]] for h in main.interiors]
    # 2026-10-03 rings: the two lossless repairs first (a consecutive duplicate after rounding, a collinear back-track), then
    # shapely's verdict as before, then the app's outline rules; GEOS allows a repeated point and the app does not, so shapely's
    # verdict alone let five of 53 windows reach a 400 at POST /api/scenes.
    ring, holes, issues, repairs = finalise_app_ring(ring, holes, *(image_size or (None, None)))
    if not _degenerate(ring, holes) and not Polygon(ring, holes).is_valid:
        return None, {**record, 'reason': 'invalid_after_rounding', **({'ring_repairs': repairs} if repairs else {})}
    if issues:
        return None, {**record, 'reason': 'app_ring_rules', 'app_issues': issues, **({'ring_repairs': repairs} if repairs else {})}
    # added_m2 = the surface the feature carries beyond the union of its pieces (a later fill may overlap an earlier one, so
    # it is not the sum of the per-pair fills); the cluster's bridge_m2 is this, the merge record keeps the pair's fill.
    return {'geom': merged, 'ring': ring, 'holes': holes, 'bridge_m2': float(bridge.area),
            'added_m2': float(merged.area-pieces_union), 'ring_repairs': repairs}, record


def _agglomerate(clusters, keys, step, kind, d_m, blockers, decided, inverse, design, merges, refusals, refused, image_size=None):
    """Repeatedly join the nearest pair of clusters (keyed by smallest member id) among `keys` whose gap <= d_m and whose
    bridge passes every guard; a refused pair (`refused`, shared by both steps of a kind) is not retried until one side has
    changed. Mutates clusters / merges / refusals / refused."""
    keys = set(keys)
    while True:
        active = sorted(k for k in keys if k in clusters)
        best = None
        for i, ka in enumerate(active):
            for kb in active[i+1:]:
                if (frozenset(clusters[ka]['ids']), frozenset(clusters[kb]['ids'])) in refused:
                    continue
                gap = clusters[ka]['geom'].distance(clusters[kb]['geom'])
                if gap <= d_m and (best is None or (gap, ka, kb) < best):
                    best = (gap, ka, kb)
        if best is None:
            return
        gap, ka, kb = best
        a, b = clusters[ka], clusters[kb]
        accepted, record = _linear_bridge(a, b, gap, d_m, a['pieces']+b['pieces'], blockers, decided, inverse, design, image_size=image_size)
        record = {'kind': kind, 'step': step, **record}
        if accepted is None:
            refused.add((frozenset(a['ids']), frozenset(b['ids'])))
            refusals.append(record)
            continue
        joined = {**a['joined'], **b['joined']}
        for member in [*a['ids'], *b['ids']]:
            joined.setdefault(member, {'step': step, 'gap_m': record['gap_m'], 'bridge_m2': record['bridge_m2']})
        key, other = min(ka, kb), max(ka, kb)
        clusters[key] = {'ids': sorted(a['ids']+b['ids']), 'geom': accepted['geom'], 'pieces': a['pieces']+b['pieces'],
                         'score': max(a['score'], b['score']), 'joined': joined, 'ring': accepted['ring'],
                         'holes': accepted['holes'], 'bridge_m2': accepted['added_m2'], 'ring_repairs': accepted.get('ring_repairs')}
        del clusters[other]
        keys.discard(other)
        merges.append(record)


def merge_linear_fragments(objects, to_meters, *, design=LINEAR_MERGE, image_size=None):
    """Re-join fragmented road / footpath / driveway candidates of one layer; pure, deterministic, nothing written.

    objects: the full object list of one layer (latest.json or review.json objects, dicts as stored); to_meters: the
    scene's pixel_to_meters (3x2); image_size: (image_width, image_height) px for the app's bounds rule, None skips that one
    rule. Only status-candidate objects of LINEAR_MERGE['kinds'] (not _protected) are joinable;
    every other object is returned unchanged (same dict). Step 1 joins, within each (kind, round(score, score_round))
    group, the nearest pair whose gap <= d_m[kind] and whose bridge passes every guard, repeatedly; step 2 does the same
    across the kind's remaining candidate features. Returns (merged_objects, report): the input order with members
    re-emitted as status 'superseded' + replacement_ids and the new features (status candidate, source linear-merge-v1,
    id = sha256 of the sorted member ids, replaces / derived_from / merged_from) appended; report = design, per-kind
    counts (pieces_in, candidates, features_out, merges by step, merges_undone_by_floor, refusals by reason, gaps closed n /
    median / p90 / max, bridge area, simplified, vertex_cap) and the merge / refusal / vertex_cap records. A cluster under
    result_floor_m2 (feature area or largest member) is refused whole ('result_floor') and its pieces come back unchanged; an
    emitted ring over 150 vertices is simplified at 2 px (simplify_long_ring) and reported under vertex_cap when still over 200.
    Every emitted ring has passed finalise_app_ring (the app's polygonIssues after the two lossless repairs; a ring that still
    fails is refused 'app_ring_rules'); a repaired ring is reported under ring_repairs / ring_repaired.
    NOT called from the extraction path (LINEAR_MERGE['enabled'] False).
    """
    affine = np.asarray(to_meters, dtype=float)
    inverse = np.linalg.inv(np.c_[affine, [0., 0., 1.]])
    kinds = tuple(design['kinds'])
    shapes = {o['id']: shape for o in objects if (shape := _metric_shape(o, affine)) is not None}
    live = [o for o in objects if o['id'] in shapes and o.get('status') not in ('rejected', 'superseded')]
    decided = [(o['id'], o.get('kind'), o.get('status'), shapes[o['id']]) for o in objects
               if o['id'] in shapes and o.get('status') in design['decided_statuses']]
    report = {'version': design['source'], 'design': {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v)
                                                      for k, v in design.items()},
              'kinds': {}, 'merges': [], 'refusals': [], 'features': [], 'vertex_cap': []}
    superseded, features = {}, []
    floor = float(design.get('result_floor_m2') or 0.)
    for kind in kinds:
        d_m = float(design['d_m'][kind])
        kind_objects = [o for o in objects if o.get('kind') == kind]
        joinable = sorted((o for o in kind_objects if o.get('status') == 'candidate' and not _protected(o) and o['id'] in shapes),
                          key=lambda o: o['id'])
        blockers = [(o['id'], o['kind'], shapes[o['id']]) for o in live
                    if o.get('kind') in design['blocker_kinds'] or (o.get('kind') in kinds and o.get('kind') != kind)]
        clusters = {o['id']: {'ids': [o['id']], 'geom': shapes[o['id']], 'pieces': [shapes[o['id']]], 'score': float(o.get('score') or 0.),
                              'joined': {}, 'ring': None, 'holes': None, 'bridge_m2': 0.} for o in joinable}
        merges, refusals, refused = [], [], set()
        groups = defaultdict(list)
        for o in joinable:
            groups[round(float(o.get('score') or 0.), design['score_round'])].append(o['id'])
        context = (kind, d_m, blockers, decided, inverse, design, merges, refusals, refused)
        for score in sorted(groups):                     # step 1: the components of one mask
            _agglomerate(clusters, groups[score], 1, *context, image_size=image_size)
        _agglomerate(clusters, list(clusters), 2, *context, image_size=image_size)   # step 2: across masks of the kind
        by_id = {o['id']: o for o in joinable}
        emitted, simplified_n, capped_n, repaired_n = [], 0, 0, 0
        for key in sorted(clusters):
            cluster = clusters[key]
            if len(cluster['ids']) < 2:
                continue
            # 19:40 card item 2, the result floor: checked on the agglomerated cluster before anything is emitted. A refused
            # cluster is not a feature; its pieces are returned exactly as they came in and its bridges are marked undone.
            feature_m2 = float(cluster['geom'].area)
            largest_m2 = max(float(shapes[i].area) for i in cluster['ids'])
            if feature_m2 < floor or largest_m2 < floor:
                ids = set(cluster['ids'])
                refusals.append({'kind': kind, 'step': 'result', 'ids': list(cluster['ids']), 'reason': 'result_floor',
                                 'feature_m2': round(feature_m2, 3), 'largest_member_m2': round(largest_m2, 3), 'floor_m2': floor,
                                 'bridge_m2': round(cluster['bridge_m2'], 3)})
                for m in merges:
                    if set(m['a_ids']) | set(m['b_ids']) <= ids:
                        m['undone'] = 'result_floor'
                continue
            feature_id = hashlib.sha256(','.join(cluster['ids']).encode()).hexdigest()[:16]
            members = [by_id[i] for i in cluster['ids']]
            largest = max(members, key=lambda o: shapes[o['id']].area)
            prompts = {o.get('prompt') for o in members if o.get('prompt')}
            # 19:40 card item 5: the extraction path's 2 px simplification above 150 vertices; still over the cap = reported.
            ring, holes, simplified = _simplified_feature_ring(cluster['ring'], cluster['holes'], design['ring_decimals'], image_size=image_size)
            model = _ring_details([tuple(p) for p in ring], holes, affine, kind)
            if (largest.get('model') or {}).get('color'):
                model['color'] = largest['model']['color']
            feature = {'id': feature_id, 'kind': kind, 'label': kind.replace('_', ' ').capitalize(), 'ring': ring,
                       'status': 'candidate', 'geometry_reviewed': False, **({'holes': holes} if holes else {}),
                       'source': design['source'], 'score': round(cluster['score'], design['score_round']),
                       **({'prompt': prompts.pop()} if len(prompts) == 1 else {}), 'model': model,
                       'replaces': list(cluster['ids']), 'derived_from': list(cluster['ids']),
                       'merged_from': [{'id': i, **cluster['joined'][i]} for i in cluster['ids']]}
            features.append(feature)
            emitted.append(cluster)
            over_cap = exceeds_vertex_cap(ring, holes)
            report['features'].append({'id': feature_id, 'kind': kind, 'members': list(cluster['ids']), 'area_m2': model['area_m2'],
                                       'pieces_union_m2': round(float(unary_union(cluster['pieces']).area), 3),
                                       'bridge_m2': round(cluster['bridge_m2'], 3), 'vertices': len(ring),
                                       'largest_member_m2': round(largest_m2, 3), 'vertex_cap': over_cap,
                                       **({'ring_repairs': cluster['ring_repairs']} if cluster.get('ring_repairs') else {}),
                                       # the guard-tested ring, so a sidecar can measure the fill apart from simplification slop
                                       **({'simplified': simplified, 'ring_unsimplified': cluster['ring'],
                                           'holes_unsimplified': cluster['holes']} if simplified and simplified.get('applied') else {})})
            simplified_n += bool(simplified and simplified.get('applied'))
            repaired_n += bool(cluster.get('ring_repairs') or (simplified or {}).get('repairs'))
            if over_cap:
                capped_n += 1
                report['vertex_cap'].append({'id': feature_id, 'kind': kind, 'members': list(cluster['ids']), 'vertices': len(ring),
                                             'holes': len(holes), 'cap': RING_SIMPLIFY['cap'],
                                             **({'simplified': simplified} if simplified else {})})
            for member in cluster['ids']:
                superseded[member] = feature_id
        kept = [m for m in merges if not m.get('undone')]
        gaps = sorted(m['gap_m'] for m in kept)
        pieces_superseded = sum(len(c['ids']) for c in emitted)
        report['kinds'][kind] = {
            'pieces_in': len(kind_objects), 'candidates': len(joinable),
            'features_out': len(joinable)-pieces_superseded+len(emitted),      # unmerged pieces (incl. floor-refused) + features
            'multi_piece_features': len(emitted), 'pieces_superseded': pieces_superseded,
            'merges': {'step1': sum(1 for m in kept if m['step'] == 1), 'step2': sum(1 for m in kept if m['step'] == 2)},
            'merges_undone_by_floor': len(merges)-len(kept),
            'refusals': {reason: sum(1 for r in refusals if r['reason'] == reason) for reason in LINEAR_REFUSALS if any(r['reason'] == reason for r in refusals)},
            'gaps_closed_m': {'n': len(gaps), 'median': round(float(np.median(gaps)), 3) if gaps else None,
                              'p90': round(float(np.percentile(gaps, 90)), 3) if gaps else None, 'max': gaps[-1] if gaps else None},
            'bridge_area_m2': round(sum(c['bridge_m2'] for c in emitted), 3),
            'simplified': simplified_n, 'vertex_cap': capped_n, 'ring_repaired': repaired_n}
        report['merges'] += merges
        report['refusals'] += refusals
    merged_objects = [{**o, 'status': 'superseded', 'replacement_ids': [superseded[o['id']]]} if o['id'] in superseded else o
                      for o in objects]
    return merged_objects + sorted(features, key=lambda f: (f['kind'], f['id'])), report


# --- the building join (2026-10-07) ------------------------------------------------------------------------------------------
# REVIEWER AUTHORISATION (verbatim, 2026-10-07 ~00:15 AEDT): "yes to defaults" on
# data/reviews/eyes/experiments/roof-fragmentation-2026-10-06/DECISION-CARD.md (docs/handover-2026-10-01.md sections 34-35). Item 1(a),
# the bold default, is the spec of merge_building_fragments: "A guarded BUILDING join in scene_extract post-processing beside
# merge_linear_fragments, same machinery (result_floor_m2 1, app ring rules, replaces / merged_from lineage; candidates only, a decided
# piece blocks any bridge): first re-join the components of ONE mask (8 window / 56 review-set pairs); then a cross-mask join of two
# candidates when the smaller is covered >= 0.5 by the other (31 / 228 pairs, dedupe gap included), or the facing-edge strip between
# them lies inside one tree / vegetation / solar_panel / car object and touches no third building, or they touch within 0.5 m and the
# smaller is under 12 m2 (80 pairs); never two pieces >= 20 m2 that only touch (172 party walls); an umbrella covering >= 3 pieces of
# >= 20 m2 is refused and listed for you (01 / 07 / 09 / 11). Rollout as the linear card: sidecar re-run on the 54 windows (both
# layers), each join rendered with its bridge coloured, you mark right / wrong on the 12 clusters + the named windows, the tally gates
# promotion, then dated RULES + DESIGN entries and a windows-only re-extract; the review set only with its own approval. TAKEOVER 0.8 /
# 0.5 / 1 px untouched; a feature with a carried-slice member (31 window pairs; #2, #5) counts only after the dated WINDOW / RULES line
# body carry got on 6 October; until then rendered, not applied." Item 2(a) gives the extraction half below (BUILDING_SIMPLIFY_VERTICES).
# Design values, not tuned on the reviewer's set. `enabled` False: merge_building_fragments is NOT called from the extraction path
# (extract_objects, analyse_scene, build_scene, save_scene or any other writer of latest.json); it runs in the sidecar
# scripts/ml/experiments/roof_join_run.py and in tests until the reviewer's marks tally gates promotion.
# How the bold text is read, rule for rule (every figure in metres from the scene's pixel_to_meters):
#   pair population  two joinable building pieces (agglomerated clusters after the first join) whose gap is <= touch_m (0.5 m), the
#                    census's "touching pair"; a pair further apart is never tried.
#   joinable         status 'candidate' and not _protected; plus, with carried_undecided_joinable, a carried frame-cut target whose
#                    status is the carry's own 'needs_adjustment' placeholder with no provenance.human_decision (the card names #2
#                    1ddf5a8c and #5 afd10322, both such targets, as this join's cover and tree cases, and makes a carried-slice member
#                    a counted-later class). A reviewer-decided object (confirmed / rejected / superseded, or needs_adjustment with a
#                    human decision or without carried_from) never merges and blocks any bridge it touches ('touches_decided').
#   step 1           same_mask: components of ONE SAM3 mask = same source + prompt + score to score_round dp (the census's same_mask).
#   step 2 (a)       cover: intersection / smaller area >= cover_min (0.5); the dedupe gap (0.8-0.95 at IoU <= 0.8) is inside it.
#   step 2 (b)       occluder_strip: gap > 0 and the facing-edge strip (the closing (a (+) touch_m | b (+) touch_m) (-) touch_m minus
#                    the pieces, parts touching both sides, the linear bridge) lies inside ONE live tree / vegetation / solar_panel /
#                    car object (that object's share of the strip >= strip_cover_min, 0.95 for ring slop at the edges) and touches no
#                    third live building (> touch_eps_m2); a strip inside one occluder that touches a third building refuses the pair
#                    'touches_third_building' when no other rule fires.
#   step 2 (c)       small_touch: gap <= touch_m and the smaller side under small_m2 (12 m2).
#   party wall       NEVER two sides >= substantial_m2 (20 m2) whose cover is under party_wall_cover_max (0.05, the census's touch_only
#                    band): refused 'party_wall' before any rule is read, on the two clusters AND on their original pieces (a cluster
#                    that grew by a speck or by a mask spanning two houses must not carry one house onto the other). Step 2 only: a
#                    same-mask pair is the two components of ONE detection, which the census's 172 party walls exclude (same_mask).
#   umbrella         a live building piece covering (cover_min) >= umbrella_pieces (3) other live pieces of >= substantial_m2 each is an
#                    umbrella: listed in report['umbrellas'] and every step-2 pair with an umbrella member is refused 'umbrella'.
#   bridge           the strip is the only surface a feature carries beyond its pieces (overlapping pieces simply union); a decided
#                    object of any class overlapping the strip (> touch_eps_m2) refuses the pair ('touches_decided'); no 2x / union
#                    ratio guard is in the bold text, so none is applied (the figures are recorded for the marks sheet).
#   result floor     feature area AND largest member >= result_floor_m2 (1 m2), else the cluster is refused whole ('result_floor').
#   rings            metres -> pixels at ring_decimals, make_valid within valid_area_tol, finalise_app_ring + app_ring_issues (the
#                    app's polygonIssues), then at emission the extraction path's 2 px simplification above the BUILDING vertex rule
#                    (BUILDING_RING_SIMPLIFY: 50 vertices, card item 2a), still over the 200 cap = reported under vertex_cap.
#   lineage          feature.replaces = derived_from = member ids; merged_from names the rule, step, gap, cover and strip per member;
#                    members come back status 'superseded' with replacement_ids = [feature id]; the feature is status 'candidate',
#                    source building-merge-v1, id = sha256 of the sorted member ids. Training labels stay pieces.
BUILDING_OCCLUDER_KINDS = ('tree', 'vegetation', 'solar_panel', 'car')
BUILDING_MERGE = {'kind': 'building', 'touch_m': .5, 'cover_min': .5, 'small_m2': 12., 'substantial_m2': 20.,
                  'party_wall_cover_max': .05, 'umbrella_pieces': 3, 'occluder_kinds': BUILDING_OCCLUDER_KINDS, 'strip_cover_min': .95,
                  'decided_statuses': ('confirmed', 'rejected', 'needs_adjustment', 'superseded'), 'carried_undecided_joinable': True,
                  'result_floor_m2': 1., 'score_round': 4, 'touch_tol_m': .02, 'touch_eps_m2': 1e-3, 'weld_m': 1e-3,
                  'valid_area_tol': .005, 'ring_decimals': 2, 'source': 'building-merge-v1', 'enabled': False}
# 2026-10-07: proposal after the 7 Oct by-eye tally, not approved (.run/session-2026-10-07-roofjoin/eye-tally.md: run-v1's 74 review
# features right 25 / wrong 30 / unclear 19; small_touch 2 / 28 / 12, same_mask 1 / 2 / 4 with #15 lot-29772's party wall and #7's yard
# pergola both through step 1; cover 17 / 0 / 1). A sidecar design beside the card's bold defaults above, nothing promoted. The three
# variant flags are read by _building_join from the design dict with v1 defaults (absent / 0.5 / True), so BUILDING_MERGE itself
# behaves as run-v1 did:
#   party_wall_guard_step1   True: the party-wall guard (both sides >= substantial_m2 and cover < party_wall_cover_max) also refuses a
#                            step-1 same-mask pair ('party_wall_step1'); v1 exempts step 1 (#15: 291.7 + 31.1 m2 at 0.32 m).
#   step1_outside_gap_max_m  0.1: a step-1 component with cover 0 by the other component and a gap over this is not joined in step 1
#                            ('step1_outside_gap'); components that overlap or abut still join (#7: 5.6 m2 bridged 0.49 m). v1 = touch_m.
#   small_touch              False: the small_touch rule is off (v1 True); cover, occluder_strip and same_mask stay.
BUILDING_MERGE_V2 = {**BUILDING_MERGE, 'party_wall_guard_step1': True, 'step1_outside_gap_max_m': .1, 'small_touch': False}
BUILDING_RULES = ('same_mask', 'cover', 'occluder_strip', 'small_touch')
BUILDING_REFUSALS = ('umbrella', 'party_wall', 'party_wall_step1', 'step1_outside_gap', 'touches_third_building', 'no_rule', 'no_bridge',
                     'not_connected', 'touches_decided', 'invalid_after_rounding', 'app_ring_rules', 'result_floor')
# Card item 2(a), the extraction half (same authorisation): "At extraction, buildings: 2 px above N = 50 vertices instead of 150 (7 of
# 1003 window pieces, 300 of 1408 review-set pieces; p90 is 18 ...)". extract_objects simplifies a detector ring of this kind at
# RING_SIMPLIFY['tolerance_px'] once its exterior exceeds BUILDING_SIMPLIFY_VERTICES; every other kind keeps RING_SIMPLIFY (150).
# The editor-side Simplify action of the same item is src work (stream C), not here.
BUILDING_SIMPLIFY_VERTICES = 50
BUILDING_RING_SIMPLIFY = {**RING_SIMPLIFY, 'vertices': BUILDING_SIMPLIFY_VERTICES}


def ring_simplify_budget(kind):
    """The extraction path's ring budget for one kind: buildings simplify above BUILDING_SIMPLIFY_VERTICES (card 2026-10-06 item 2a),
    every other kind above RING_SIMPLIFY['vertices']; tolerance, hole and cap figures are shared."""
    return BUILDING_RING_SIMPLIFY if kind == 'building' else RING_SIMPLIFY


def _undecided_carry(obj):
    """A carried frame-cut target still wearing the carry's 'needs_adjustment' placeholder: no reviewer decision on it yet."""
    return bool(obj.get('carried_from')) and obj.get('status') == 'needs_adjustment' \
        and not (obj.get('provenance') or {}).get('human_decision')


def _building_joinable(obj, design):
    if _protected(obj) or obj.get('kind') != design['kind']:
        return False
    return obj.get('status') == 'candidate' or (bool(design.get('carried_undecided_joinable')) and _undecided_carry(obj))


def _building_decided(obj, design):
    return obj.get('status') in design['decided_statuses'] and not (design.get('carried_undecided_joinable') and _undecided_carry(obj))


def _closing_strip(ga, gb, d_m, design):
    """The facing-edge strip between two building geometries (metres): the closing (ga (+) d | gb (+) d) (-) d minus both pieces,
    keeping the parts that reach both sides (the linear bridge). Empty when no part reaches both."""
    sides = unary_union([ga, gb])
    closed = unary_union([ga.buffer(d_m, quad_segs=8), gb.buffer(d_m, quad_segs=8)]).buffer(-d_m, quad_segs=8)
    tol, eps = design['touch_tol_m'], design['touch_eps_m2']
    connecting = [g for g in iter_polygons(closed.difference(sides)) if g.area > eps and g.distance(ga) <= tol and g.distance(gb) <= tol]
    return unary_union(connecting) if connecting else Polygon()


def _pixel_ring(merged, inverse, design, image_size):
    """The linear join's ring finish: metres -> pixels at ring_decimals, make_valid within valid_area_tol, the two lossless repairs and
    the app's outline rules. Returns (ring, holes, repairs, refusal | None)."""
    decimals = design['ring_decimals']

    def to_pixels(coords):
        pts = np.c_[np.asarray(coords, dtype=float), np.ones(len(coords))] @ inverse
        return [[round(float(x), decimals), round(float(y), decimals)] for x, y in pts[:, :2]]
    ring = to_pixels(merged.exterior.coords[:-1])
    holes = [to_pixels(h.coords[:-1]) for h in merged.interiors]
    rounded = Polygon(ring, holes)
    valid = make_valid(rounded)
    parts = sorted((p for p in iter_polygons(valid) if p.area > 0), key=lambda p: -p.area)
    reference = abs(rounded.area)
    if not parts or not reference or abs(sum(p.area for p in parts)-reference) > design['valid_area_tol']*reference \
            or sum(p.area for p in parts[1:]) > design['valid_area_tol']*reference:
        return None, None, None, {'reason': 'invalid_after_rounding'}
    main = parts[0]
    ring = [[round(float(x), decimals), round(float(y), decimals)] for x, y in main.exterior.coords[:-1]]
    holes = [[[round(float(x), decimals), round(float(y), decimals)] for x, y in h.coords[:-1]] for h in main.interiors]
    ring, holes, issues, repairs = finalise_app_ring(ring, holes, *(image_size or (None, None)))
    if not _degenerate(ring, holes) and not Polygon(ring, holes).is_valid:
        return None, None, repairs, {'reason': 'invalid_after_rounding', **({'ring_repairs': repairs} if repairs else {})}
    if issues:
        return None, None, repairs, {'reason': 'app_ring_rules', 'app_issues': issues, **({'ring_repairs': repairs} if repairs else {})}
    return ring, holes, repairs, None


def _building_join(a, b, gap_m, step, ctx, image_size=None):
    """One guarded join of two building clusters (geometries in metres). Returns (accepted: dict | None, record: dict); the record
    names the rule that fired ('rule', 'rules') or the refusal ('reason') with every figure the marks sheet prints."""
    design = ctx['design']
    eps, touch_m = design['touch_eps_m2'], design['touch_m']
    ga, gb = a['geom'], b['geom']
    smaller_m2, larger_m2 = min(ga.area, gb.area), max(ga.area, gb.area)
    inter = ga.intersection(gb).area
    cover = inter/smaller_m2 if smaller_m2 else 0.
    record = {'step': step, 'a_ids': sorted(a['ids']), 'b_ids': sorted(b['ids']), 'gap_m': round(float(gap_m), 3),
              'cover': round(float(cover), 4), 'smaller_m2': round(float(smaller_m2), 3), 'larger_m2': round(float(larger_m2), 3)}
    if step == 2:
        hits = sorted(i for i in [*a['ids'], *b['ids']] if i in ctx['umbrellas'])
        if hits:
            return None, {**record, 'reason': 'umbrella', 'umbrellas': hits}
    # The party-wall guard: step 2 always; step 1 only under the v2 proposal flag (BUILDING_MERGE_V2 'party_wall_guard_step1'; v1 exempts
    # the components of one mask).
    if step == 2 or bool(design.get('party_wall_guard_step1')):
        wall_reason = 'party_wall' if step == 2 else 'party_wall_step1'
        if smaller_m2 >= design['substantial_m2'] and cover < design['party_wall_cover_max']:
            return None, {**record, 'reason': wall_reason}
        # NEVER, read on the ORIGINAL pieces too: a cluster that grew (a speck attached, a mask spanning two houses absorbed) must not
        # carry a substantial piece onto a substantial neighbour the piece only touches.
        walls = sorted((ia, ib) for ia, pa in a['pieces'].items() for ib, pb in b['pieces'].items()
                       if min(pa.area, pb.area) >= design['substantial_m2'] and pa.distance(pb) <= touch_m
                       and pa.intersection(pb).area/min(pa.area, pb.area) < design['party_wall_cover_max'])
        if walls:
            return None, {**record, 'reason': wall_reason, 'party_wall_pieces': [{'a': ia, 'b': ib} for ia, ib in walls]}
    outside_max = float(design.get('step1_outside_gap_max_m', touch_m))
    if step == 1 and inter <= eps and gap_m > outside_max:
        # v2 proposal flag (BUILDING_MERGE_V2 'step1_outside_gap_max_m'): a same-mask component wholly outside the other and further
        # than this is not rejoined in step 1; v1's default is touch_m, which no tried pair exceeds.
        return None, {**record, 'reason': 'step1_outside_gap', 'outside_gap_max_m': outside_max}
    # The strip between the pieces: the only surface a feature carries beyond them. Overlapping pieces (inter > eps) union as they are.
    strip = _closing_strip(ga, gb, touch_m, design) if inter <= eps else Polygon()
    record['bridge_m2'] = round(float(strip.area), 3)
    rules, occluder, third = [], None, []
    if step == 1:
        rules.append('same_mask')
    else:
        if cover >= design['cover_min']:
            rules.append('cover')
        if gap_m > 0 and strip.area > eps:
            shares = sorted(((shape.intersection(strip).area/strip.area, oid, kind) for oid, kind, shape in ctx['occluders']
                             if shape.intersects(strip)), reverse=True)
            best = shares[0] if shares else None
            record['strip'] = {'m2': round(float(strip.area), 3),
                               'occluder': {'id': best[1], 'kind': best[2], 'share': round(float(best[0]), 4)} if best else None}
            if best and best[0] >= design['strip_cover_min']:
                members = set(a['ids']) | set(b['ids'])
                third = sorted((oid, kind, status) for oid, kind, status, shape in ctx['buildings']
                               if oid not in members and shape.intersection(strip).area > eps)
                if third:
                    record['third_buildings'] = [{'id': i, 'kind': k, 'status': s} for i, k, s in third]
                else:
                    rules.append('occluder_strip')
                    occluder = record['strip']['occluder']
        if design.get('small_touch', True) and gap_m <= touch_m and smaller_m2 < design['small_m2']:   # v2 proposal flag: off
            rules.append('small_touch')
        if not rules:
            return None, {**record, 'reason': 'touches_third_building' if third else 'no_rule'}
    record.update({'rule': rules[0], 'rules': rules})
    if inter <= eps and strip.is_empty:
        return None, {**record, 'reason': 'no_bridge'}
    merged = unary_union([ga, gb, strip])
    if merged.geom_type != 'Polygon':
        merged = unary_union([ga, gb, strip.buffer(design['weld_m'], join_style='mitre')])  # the linear join's 1 mm weld
        if merged.geom_type != 'Polygon':
            return None, {**record, 'reason': 'not_connected'}
    if not strip.is_empty:
        hits = sorted((oid, kind, status) for oid, kind, status, shape in ctx['decided'] if strip.intersection(shape).area > eps)
        if hits:
            return None, {**record, 'reason': 'touches_decided', 'blockers': [{'id': i, 'kind': k, 'status': s} for i, k, s in hits]}
    pieces_union = unary_union([*a['pieces'].values(), *b['pieces'].values()]).area
    record.update({'feature_m2': round(float(merged.area), 3), 'pieces_union_m2': round(float(pieces_union), 3)})
    ring, holes, repairs, refusal = _pixel_ring(merged, ctx['inverse'], design, image_size)
    if refusal:
        return None, {**record, **refusal}
    return {'geom': merged, 'ring': ring, 'holes': holes, 'bridge_m2': float(strip.area), 'added_m2': float(merged.area-pieces_union),
            'ring_repairs': repairs, 'occluder': occluder}, record


def _building_agglomerate(clusters, keys, step, ctx, merges, refusals, refused, image_size=None):
    """Repeatedly join the best pair of clusters among `keys` whose gap is <= touch_m: the pair with the smallest gap, then the
    highest cover, then the smallest ids (so a dedupe-gap duplicate is absorbed before a speck attaches); a refused pair is not
    retried until one side has changed. Mutates clusters / merges / refusals / refused."""
    keys = set(keys)
    touch_m = ctx['design']['touch_m']
    while True:
        active = sorted(k for k in keys if k in clusters)
        best = None
        for i, ka in enumerate(active):
            for kb in active[i+1:]:
                if (frozenset(clusters[ka]['ids']), frozenset(clusters[kb]['ids'])) in refused:
                    continue
                ga, gb = clusters[ka]['geom'], clusters[kb]['geom']
                gap = ga.distance(gb)
                if gap > touch_m:
                    continue
                smaller = min(ga.area, gb.area)
                cover = ga.intersection(gb).area/smaller if smaller else 0.
                key = (round(float(gap), 6), -round(float(cover), 6), ka, kb)
                if best is None or key < best:
                    best = key
        if best is None:
            return
        gap, _, ka, kb = best
        a, b = clusters[ka], clusters[kb]
        accepted, record = _building_join(a, b, gap, step, ctx, image_size=image_size)
        record = {'kind': ctx['design']['kind'], **record}
        if accepted is None:
            refused.add((frozenset(a['ids']), frozenset(b['ids'])))
            refusals.append(record)
            continue
        joined = {**a['joined'], **b['joined']}
        entry = {'step': step, 'rule': record['rule'], 'gap_m': record['gap_m'], 'cover': record['cover'], 'bridge_m2': record['bridge_m2'],
                 'smaller_m2': record['smaller_m2'], **({'occluder': accepted['occluder']} if accepted['occluder'] else {})}
        for member in [*a['ids'], *b['ids']]:
            joined.setdefault(member, dict(entry))
        key, other = min(ka, kb), max(ka, kb)
        clusters[key] = {'ids': sorted(a['ids']+b['ids']), 'geom': accepted['geom'], 'pieces': {**a['pieces'], **b['pieces']},
                         'score': max(a['score'], b['score']), 'joined': joined, 'ring': accepted['ring'], 'holes': accepted['holes'],
                         'bridge_m2': accepted['added_m2'], 'ring_repairs': accepted.get('ring_repairs'),
                         'rules': sorted(set(a.get('rules') or []) | set(b.get('rules') or []) | set(record['rules']))}
        del clusters[other]
        keys.discard(other)
        merges.append(record)


def building_umbrellas(objects, shapes, design=BUILDING_MERGE):
    """The umbrella masks of one layer: a live building piece covering (intersection / piece area >= cover_min) at least
    umbrella_pieces other live building pieces of >= substantial_m2 each. Returns {umbrella id: [{id, area_m2, cover}]}; counts and
    geometry only, every live status counts (the dwellings under an umbrella may be confirmed)."""
    live = [(o['id'], shapes[o['id']]) for o in objects if o.get('kind') == design['kind'] and o['id'] in shapes
            and o.get('status') not in ('rejected', 'superseded')]
    out = {}
    for uid, umbrella in live:
        covered = []
        for pid, piece in live:
            if pid == uid or piece.area < design['substantial_m2'] or not piece.intersects(umbrella):
                continue
            cover = umbrella.intersection(piece).area/piece.area
            if cover >= design['cover_min']:
                covered.append({'id': pid, 'area_m2': round(float(piece.area), 3), 'cover': round(float(cover), 4)})
        if len(covered) >= design['umbrella_pieces']:
            out[uid] = sorted(covered, key=lambda c: c['id'])
    return out


def merge_building_fragments(objects, to_meters, *, image_size=None, design=BUILDING_MERGE):
    """Re-join fragmented building candidates of one layer under the 2026-10-07 card (BUILDING_MERGE above); pure, deterministic,
    nothing written. objects: the full object list of one layer (latest.json or review.json objects, dicts as stored); to_meters:
    the scene's pixel_to_meters (3x2); image_size: (image_width, image_height) px for the app's bounds rule, None skips that one
    rule. Only joinable building pieces (_building_joinable) merge; every other object is returned unchanged (same dict). Step 1
    joins, within each same-mask group (source, prompt, round(score, score_round)), the best pair within touch_m, repeatedly; step 2
    does the same across every remaining building cluster under the cover / occluder_strip / small_touch rules with the party-wall and
    umbrella guards. Returns (merged_objects, report): the input order with members re-emitted as status 'superseded' + replacement_ids
    and the features (status candidate, source building-merge-v1, id = sha256 of the sorted member ids, replaces / derived_from /
    merged_from naming the rule) appended; report = design, kinds.building counts (pieces_in, candidates, joinable, features_out,
    multi_piece_features, pieces_superseded, merges by rule and step, merges_undone_by_floor, refusals by reason, pairs_tried, gaps
    closed, bridge area, simplified, vertex_cap, ring_repaired, features_with_carried_member), umbrellas, and the merge / refusal /
    feature / vertex_cap records. A cluster under result_floor_m2 (feature area or largest member) is refused whole ('result_floor')
    and its pieces come back unchanged; an emitted ring over BUILDING_SIMPLIFY_VERTICES is simplified at 2 px and reported under
    vertex_cap when still over 200; every emitted ring has passed finalise_app_ring.
    NOT called from the extraction path (BUILDING_MERGE['enabled'] False).
    """
    affine = np.asarray(to_meters, dtype=float)
    inverse = np.linalg.inv(np.c_[affine, [0., 0., 1.]])
    kind = design['kind']
    shapes = {o['id']: shape for o in objects if (shape := _metric_shape(o, affine)) is not None}
    live = [o for o in objects if o['id'] in shapes and o.get('status') not in ('rejected', 'superseded')]
    kind_objects = [o for o in objects if o.get('kind') == kind]
    joinable = sorted((o for o in kind_objects if o['id'] in shapes and _building_joinable(o, design)), key=lambda o: o['id'])
    umbrellas = building_umbrellas(objects, shapes, design)
    ctx = {'design': design, 'inverse': inverse, 'umbrellas': umbrellas,
           'decided': [(o['id'], o.get('kind'), o.get('status'), shapes[o['id']]) for o in objects
                       if o['id'] in shapes and _building_decided(o, design)],
           'occluders': [(o['id'], o.get('kind'), shapes[o['id']]) for o in live if o.get('kind') in design['occluder_kinds']],
           'buildings': [(o['id'], o.get('kind'), o.get('status'), shapes[o['id']]) for o in live if o.get('kind') == kind]}
    report = {'version': design['source'], 'design': {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v)
                                                      for k, v in design.items()},
              'kinds': {}, 'umbrellas': [{'id': uid, 'area_m2': round(float(shapes[uid].area), 3), 'covered': covered}
                                         for uid, covered in sorted(umbrellas.items())],
              'merges': [], 'refusals': [], 'features': [], 'vertex_cap': []}
    clusters = {o['id']: {'ids': [o['id']], 'geom': shapes[o['id']], 'pieces': {o['id']: shapes[o['id']]}, 'score': float(o.get('score') or 0.),
                          'joined': {}, 'ring': None, 'holes': None, 'bridge_m2': 0., 'rules': []} for o in joinable}
    merges, refusals, refused = [], [], set()
    groups = defaultdict(list)
    for o in joinable:
        groups[(o.get('source'), o.get('prompt'), round(float(o.get('score') or 0.), design['score_round']))].append(o['id'])
    for group in sorted(groups, key=str):                                  # step 1: the components of one mask
        if len(groups[group]) > 1:
            _building_agglomerate(clusters, groups[group], 1, ctx, merges, refusals, refused, image_size=image_size)
    # v2 proposal flag only (empty under v1): a pair step 1 refused for the outside gap alone is "not joined in step 1"; step 2 may
    # still bridge it under its own rules (the occluder strip), or refuse it again with its own reason.
    refused -= {pair for r in refusals if r['reason'] == 'step1_outside_gap'
                for pair in ((frozenset(r['a_ids']), frozenset(r['b_ids'])), (frozenset(r['b_ids']), frozenset(r['a_ids'])))}
    _building_agglomerate(clusters, list(clusters), 2, ctx, merges, refusals, refused, image_size=image_size)   # step 2: across masks
    by_id = {o['id']: o for o in joinable}
    floor = float(design.get('result_floor_m2') or 0.)
    superseded, features, emitted = {}, [], []
    simplified_n = capped_n = repaired_n = carried_n = 0
    for key in sorted(clusters):
        cluster = clusters[key]
        if len(cluster['ids']) < 2:
            continue
        feature_m2 = float(cluster['geom'].area)
        largest_m2 = max(float(shapes[i].area) for i in cluster['ids'])
        if feature_m2 < floor or largest_m2 < floor:
            ids = set(cluster['ids'])
            refusals.append({'kind': kind, 'step': 'result', 'ids': list(cluster['ids']), 'reason': 'result_floor',
                             'feature_m2': round(feature_m2, 3), 'largest_member_m2': round(largest_m2, 3), 'floor_m2': floor,
                             'bridge_m2': round(cluster['bridge_m2'], 3)})
            for m in merges:
                if set(m['a_ids']) | set(m['b_ids']) <= ids:
                    m['undone'] = 'result_floor'
            continue
        feature_id = hashlib.sha256(','.join(cluster['ids']).encode()).hexdigest()[:16]
        members = [by_id[i] for i in cluster['ids']]
        largest = max(members, key=lambda o: shapes[o['id']].area)
        prompts = {o.get('prompt') for o in members if o.get('prompt')}
        ring, holes, simplified = _simplified_feature_ring(cluster['ring'], cluster['holes'], design['ring_decimals'],
                                                           budget=BUILDING_RING_SIMPLIFY, image_size=image_size)
        model = _ring_details([tuple(p) for p in ring], holes, affine, kind)
        if (largest.get('model') or {}).get('color'):
            model['color'] = largest['model']['color']
        carried = [o['id'] for o in members if o.get('carried_from')]
        feature = {'id': feature_id, 'kind': kind, 'label': kind.capitalize(), 'ring': ring, 'status': 'candidate',
                   'geometry_reviewed': False, **({'holes': holes} if holes else {}), 'source': design['source'],
                   'score': round(cluster['score'], design['score_round']), **({'prompt': prompts.pop()} if len(prompts) == 1 else {}),
                   'model': model, 'replaces': list(cluster['ids']), 'derived_from': list(cluster['ids']),
                   'merged_from': [{'id': i, **cluster['joined'][i]} for i in cluster['ids']]}
        features.append(feature)
        emitted.append(cluster)
        over_cap = exceeds_vertex_cap(ring, holes)
        report['features'].append({'id': feature_id, 'kind': kind, 'members': list(cluster['ids']), 'rules': list(cluster['rules']),
                                   'area_m2': model['area_m2'], 'pieces_union_m2': round(float(unary_union(list(cluster['pieces'].values())).area), 3),
                                   'bridge_m2': round(cluster['bridge_m2'], 3), 'vertices': len(ring),
                                   'largest_member_m2': round(largest_m2, 3), 'vertex_cap': over_cap, 'carried_members': carried,
                                   **({'ring_repairs': cluster['ring_repairs']} if cluster.get('ring_repairs') else {}),
                                   **({'simplified': simplified, 'ring_unsimplified': cluster['ring'],
                                       'holes_unsimplified': cluster['holes']} if simplified and simplified.get('applied') else {})})
        simplified_n += bool(simplified and simplified.get('applied'))
        repaired_n += bool(cluster.get('ring_repairs') or (simplified or {}).get('repairs'))
        carried_n += bool(carried)
        if over_cap:
            capped_n += 1
            report['vertex_cap'].append({'id': feature_id, 'kind': kind, 'members': list(cluster['ids']), 'vertices': len(ring),
                                         'holes': len(holes), 'cap': RING_SIMPLIFY['cap'], **({'simplified': simplified} if simplified else {})})
        for member in cluster['ids']:
            superseded[member] = feature_id
    kept = [m for m in merges if not m.get('undone')]
    gaps = sorted(m['gap_m'] for m in kept)
    pieces_superseded = sum(len(c['ids']) for c in emitted)
    report['kinds'][kind] = {
        'pieces_in': len(kind_objects), 'candidates': sum(1 for o in kind_objects if o.get('status') == 'candidate'),
        'joinable': len(joinable), 'carried_undecided_joinable': sum(1 for o in joinable if _undecided_carry(o)),
        'features_out': len(joinable)-pieces_superseded+len(emitted), 'multi_piece_features': len(emitted),
        'pieces_superseded': pieces_superseded,
        'merges': {'step1': sum(1 for m in kept if m['step'] == 1), 'step2': sum(1 for m in kept if m['step'] == 2),
                   **{rule: sum(1 for m in kept if m['rule'] == rule) for rule in BUILDING_RULES}},
        'merges_undone_by_floor': len(merges)-len(kept), 'pairs_tried': len(merges)+len(refusals),
        'refusals': {reason: sum(1 for r in refusals if r['reason'] == reason) for reason in BUILDING_REFUSALS
                     if any(r['reason'] == reason for r in refusals)},
        'umbrellas': len(umbrellas),
        'gaps_closed_m': {'n': len(gaps), 'median': round(float(np.median(gaps)), 3) if gaps else None,
                          'p90': round(float(np.percentile(gaps, 90)), 3) if gaps else None, 'max': gaps[-1] if gaps else None},
        'bridge_area_m2': round(sum(c['bridge_m2'] for c in emitted), 3),
        'simplified': simplified_n, 'vertex_cap': capped_n, 'ring_repaired': repaired_n, 'features_with_carried_member': carried_n}
    report['merges'] = merges
    report['refusals'] = refusals
    merged_objects = [{**o, 'status': 'superseded', 'replacement_ids': [superseded[o['id']]]} if o['id'] in superseded else o
                      for o in objects]
    return merged_objects + sorted(features, key=lambda f: f['id']), report


def merge_plan(scene):
    """JSON-only dry run over latest.json: which live canopy objects the merge would fold into which containers.

    Treats every live canopy object as proposed against the others; writes nothing and loads no model.
    """
    live = [o for o in scene.get('objects', []) if o.get('status') != 'superseded']
    canopy = [dict(o) for o in live if o['kind'] in CANOPY_KINDS]
    to_meters = np.asarray(scene['pixel_to_meters'], dtype=float)
    pixel_area = abs(float(np.linalg.det(to_meters[:2])))
    kept, replaces = merge_canopy_fragments(canopy, [], to_meters)
    by_id = {o['id']: o for o in live}
    merges = []
    for fragment, container in sorted(replaces.items()):
        piece, whole = Polygon(by_id[fragment]['ring'], by_id[fragment].get('holes') or []), Polygon(by_id[container]['ring'], by_id[container].get('holes') or [])
        merges.append({'fragment_id': fragment, 'fragment_kind': by_id[fragment]['kind'], 'fragment_m2': round(piece.area*pixel_area, 3),
                       'container_id': container, 'container_kind': by_id[container]['kind'], 'container_m2': round(whole.area*pixel_area, 1),
                       'cover': round(piece.intersection(whole).area/piece.area, 4)})
    small_buildings = [o['id'] for o in live if o['kind'] == 'building'
                       and Polygon(o['ring'], o.get('holes') or []).area*pixel_area < BUILDING_FLOOR_M2]
    return {'scene': scene.get('id'), 'canopy_objects': len(canopy), 'merges': merges, 'kept': len(kept),
            'buildings_under_floor': small_buildings, 'building_floor_m2': BUILDING_FLOOR_M2, 'design': dict(CANOPY_MERGE),
            'note': 'Dry run: nothing written; the merge applies only to new extractions.'}


def extract_objects(image, image_hash, to_meters, segmenter, *, objects=None, kinds=None, custom_prompt=None):
    objects = list(objects or [])
    features = segmenter.encode_image(image)
    scans = dict(PROMPTS if kinds is None else ((k, PROMPTS[k]) for k in kinds))
    if custom_prompt:
        scans['other'] = custom_prompt
    # Same-frame canopy detections must be available before roof completion,
    # regardless of the caller's class ordering. Never use review geometry.
    if 'tree' in scans and 'building' in scans:
        scans = {'tree': scans['tree'], **{k: v for k, v in scans.items() if k != 'tree'}}
    scan_results = {}
    for kind, prompt in scans.items():
        from .roof_boundary import model_canopies
        canopies = model_canopies(objects) if kind == 'building' else []
        proposed = []
        candidates = []
        pixel_area = abs(float(np.linalg.det(to_meters[:2])))
        minimum = max(12., 1./pixel_area) if kind == 'building' else 5 if kind == 'car' else 12
        for detection in segmenter.proposals(features, prompt, image.size, threshold=.45, max_proposals=80):
            for polygon in mask_to_polygons(detection.mask, min_area_px=minimum, simplify_px=.9):
                polygon = polygon.intersection(box(0, 0, image.width, image.height))
                if polygon.geom_type != 'Polygon' or not polygon.is_valid or polygon.area < 5:
                    continue
                # 2026-10-07 card item 2(a): buildings simplify at 2 px above BUILDING_SIMPLIFY_VERTICES (50); other kinds above 150.
                budget = ring_simplify_budget(kind)
                if len(polygon.exterior.coords)-1 > budget['vertices'] or any(len(h.coords)-1 > budget['hole_vertices'] for h in polygon.interiors):
                    polygon = polygon.simplify(2, preserve_topology=True)
                raw_coordinates = [[round(x, 2), round(y, 2)] for x, y in polygon.exterior.coords[:-1]]
                raw_holes = [[[round(x, 2), round(y, 2)] for x, y in h.coords[:-1]] for h in polygon.interiors]
                # Identity follows the raw detector mask so a revised roof prior
                # does not break the same-image feedback/re-analysis association.
                identity = f'{image_hash}:{kind}:{raw_coordinates}' + (f':holes:{raw_holes}' if raw_holes else '')
                refinement = None
                if kind == 'building':
                    from .roof_surface import refine_roof_surface
                    polygon, refinement = refine_roof_surface(polygon, image, to_meters, canopies=canopies)
                candidates.append((polygon, detection.score, identity, refinement))
        if kind == 'building':
            from .roof_assembly import assemble_roof_candidates
            candidates = assemble_roof_candidates(candidates, image, to_meters, canopies=canopies)
            candidates = apply_building_floor(candidates, pixel_area)
            candidates.sort(key=lambda item: -item[0].area)
        # An expanded capture can complete a formerly clipped model mask of ANY class (a tree canopy cut by the
        # 512 px frame edge as much as a roof). Let the fuller prediction replace that seed instead of silently
        # suppressing it as a duplicate. CRM geometry and review annotations are excluded.
        # The cover needed to take over a seed equals the cover at which the duplicate rule below would otherwise
        # suppress the fuller candidate (.8 of the smaller shape); assembled roofs keep the stricter .95 because a
        # larger roof blob may be a neighbour merged in. Between those numbers a fuller canopy used to vanish and
        # the half tree stayed (house-25822: covers of .93 and .94 measured on the padded frame).
        replacements = {}
        takeover = .95 if kind == 'building' else .8
        for obj in objects:
            if (obj['kind'] != kind or obj.get('source') != 'sam3-text-onnx'
                    or obj.get('geometry_reviewed') or obj.get('roof_line_edits') or obj.get('roof_feedback')):
                continue
            existing = Polygon(obj['ring'], obj.get('holes'))
            if not existing.is_valid or existing.is_empty:
                continue
            for polygon, _, identity, _ in candidates:
                if polygon.area > existing.area*1.02 and polygon.intersection(existing).area/existing.area > takeover:
                    replacements[obj['id']] = hashlib.sha256(identity.encode()).hexdigest()[:16]
                    break
        replaced_seeds = [o for o in objects if o['id'] in replacements]
        objects = [o for o in objects if o['id'] not in replacements]
        for polygon, score, identity, refinement in candidates:
            def duplicate(obj, kind=kind, polygon=polygon):
                if obj['kind'] != kind:
                    return False
                existing = Polygon(obj['ring'], obj.get('holes'))
                intersection = polygon.intersection(existing).area
                iou = intersection / max(.01, polygon.union(existing).area)
                # Misaligned/multi-roof seeds stay available for explicit review.
                if obj.get('source') in ('roof-pipeline', 'crm-roof-pipeline'):
                    return iou > .9
                if kind == 'building':
                    # A roof plane or wing almost contained by the already
                    # retained whole roof is not another building instance.
                    return iou > .8 or intersection / polygon.area > .95
                return intersection / max(.01, min(polygon.area, existing.area)) > .8
            if any(duplicate(o) for o in [*objects, *proposed]):
                continue
            coordinates = [[round(x, 2), round(y, 2)] for x, y in polygon.exterior.coords[:-1]]
            holes = [[[round(x, 2), round(y, 2)] for x, y in h.coords[:-1]] for h in polygon.interiors]
            if len(coordinates) > 200 or len(holes) > 32 or any(len(h) > 200 for h in holes) or len(coordinates)+sum(map(len, holes)) > 2000:
                continue
            if not Polygon(coordinates, holes).is_valid:
                continue
            oid = hashlib.sha256(identity.encode()).hexdigest()[:16]
            proposed.append({'id': oid, 'kind': kind, 'label': custom_prompt if kind == 'other' else kind.replace('_', ' ').capitalize(),
                             'ring': coordinates, 'status': 'candidate', 'geometry_reviewed': False,
                             **({'holes': holes} if holes else {}),
                             **({'boundary_refinement': refinement} if refinement else {}),
                             'source': 'sam3-text-onnx', 'score': round(score, 4), 'prompt': prompt,
                             'model': polygon_details([tuple(p) for p in coordinates], image, to_meters, kind, holes)})
        emitted = {o['id'] for o in proposed}
        # A candidate may itself be suppressed by another retained seed or
        # fail outline validation. Never leave children with a missing parent.
        objects.extend(o for o in replaced_seeds if replacements[o['id']] not in emitted)
        replacements = {a: b for a, b in replacements.items() if b in emitted}
        objects = [{**o, **{key: replacements[o[key]] for key in ('parent_id', 'group_id')
                           if o.get(key) in replacements}} for o in objects]
        for o in proposed:                                  # a fuller detection remembers every seed it replaced
            replaced = [a for a, b in replacements.items() if b == o['id']]   # one whole roof may take over several parts
            if replaced:
                o['replaces'] = replaced
        if kind in CANOPY_KINDS:                            # one bush split into pieces is one object with `replaces`
            proposed, _ = merge_canopy_fragments(proposed, objects, to_meters)
        objects += proposed
        scan_results[kind] = {'status': 'complete', 'count': len(proposed), 'prompt': prompt}
    return objects, scan_results


def cohort_scene_source(address_id, cohort, *, root=Path('.'), sources=None):
    """Prepare the exact source frame without loading or running a detector."""
    source = root / 'data/reviews/cohorts' / cohort
    reports, records = sources or cohort_sources(cohort, root)
    report, record = reports[address_id], records[address_id]
    image_path = source / str(address_id) / 'site-clean.png'
    image = Image.open(image_path).convert('RGB')
    image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    ring = np.asarray(report['footprint_coords'])
    pixels = np.asarray(report['ring_pixels'])
    # Dataset reports sometimes retain the closing footprint vertex.
    ring = ring[:len(pixels)]
    to_meters = np.linalg.lstsq(np.c_[pixels, np.ones(len(pixels))], ring, rcond=None)[0]
    to_pixels = np.linalg.lstsq(np.c_[ring, np.ones(len(ring))], pixels, rcond=None)[0]
    objects = []
    roof = {'id': f'roof-{address_id}', 'kind': 'building', 'label': record['full_address'].split(',')[0],
            'ring': pixels.tolist(), 'status': 'candidate', 'geometry_reviewed': False,
            'source': 'roof-pipeline', 'address_id': address_id,
            'model': polygon_details([tuple(p) for p in pixels], image, to_meters, 'building'),
            'roof_parts': report['parts'], 'roof_lines': report['rooflines'], 'roof_type': record.get('roof_type', 'hipped')}
    roof['model'].update(height_m=record.get('eave_height', 3.2), height_source='roof-pipeline-estimate')
    objects.append(roof)
    scene = {'schema_version': 1, 'id': f'{cohort}-{address_id}', 'address_id': address_id, 'cohort': cohort,
             'title': record['full_address'], 'centroid': record['centroid'], 'image_sha256': image_hash,
             'image_width': image.width, 'image_height': image.height,
             'pixel_to_meters': to_meters.tolist(), 'meters_to_pixel': to_pixels.tolist(),
             'objects': objects, 'groups': [], 'scans': {},
             'height_note': 'Heights are class/roof-model priors, not measured. Width and length follow the image footprint.'}
    return scene, image, image_path.read_bytes()


def cohort_sources(cohort, root=Path('.')):
    reports = json.loads((root/'data/reviews/cohorts'/cohort/'report.json').read_text())['results']
    records = json.loads((root/'public/address-roofs.json').read_text())
    return ({row['address_id']: row for row in reports}, {row['address_id']: row for row in records})


def build_scene(address_id, cohort, segmenter, *, kinds=None, root=Path('.'), custom_prompt=None, roof_models=False, out_root=None):
    """Inputs come from root; outputs (snapshots, parcels) go to out_root when given."""
    scene, image, image_bytes = cohort_scene_source(address_id, cohort, root=root)
    scene['objects'], scene['scans'] = extract_objects(image, scene['image_sha256'], np.array(scene['pixel_to_meters']),
        segmenter, objects=scene['objects'], kinds=kinds, custom_prompt=custom_prompt)
    scene['groups'] = group_objects(scene['objects'])
    if roof_models:
        from .scene_house import add_roof_estimates, pipeline_context_hints
        add_roof_estimates(scene, image, context_hints=pipeline_context_hints(scene, image, root))
    from .scene_property_roofs import add_property_roofs
    add_property_roofs(scene, image, segmenter, root=out_root or root)
    return save_scene(scene, image_bytes, out_root or root)


def save_scene(scene, image_bytes, root=Path('.')):
    scene = {key: value for key, value in scene.items() if key != 'model_signature'}
    image_hash = scene['image_sha256']
    encoded = json.dumps(scene, sort_keys=True)
    version = hashlib.sha256(encoded.encode()).hexdigest()
    scene['model_signature'] = version
    out = root / 'data/scenes' / scene['id']
    snapshots = out / 'models'
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f'{version}.json').write_text(json.dumps(scene, indent=2))
    image_target = out / f'{image_hash}.png'
    if not image_target.exists():
        image_target.write_bytes(image_bytes)
    temporary = out/'latest.tmp'
    temporary.write_text(json.dumps(scene, indent=2))
    temporary.replace(out/'latest.json')
    return scene


def analyse_scene(scene, image, segmenter, *, kinds=None, custom_prompt=None, roof_models=False, detector_only=False, root=Path('.'), parcel_data=None,
                  evidence_views=None):
    """New predictions from registered imagery; never read scene review geometry.

    Detector-only mode also excludes imported address CRM geometry, making it
    possible to reproduce an inference run independently of saved corrections.
    evidence_views (default None: unchanged) pass straight through to
    scene_house.add_roof_estimates when roof_models is set.
    """
    from .scene_region import corrected_roofs

    prediction = {key: value for key, value in scene.items() if key not in ('objects', 'groups', 'scans', 'analysis', 'model_signature', 'property_roofs')}
    seeds = [] if detector_only else corrected_roofs(scene, image, root=root)
    prediction['objects'], prediction['scans'] = extract_objects(image, scene['image_sha256'], np.array(scene['pixel_to_meters']),
        segmenter, objects=seeds, kinds=kinds, custom_prompt=custom_prompt)
    prediction['groups'] = group_objects(prediction['objects'])
    if roof_models:
        from .scene_house import add_roof_estimates, pipeline_context_hints
        add_roof_estimates(prediction, image, context_hints=pipeline_context_hints(prediction, image, root),
                           **({} if evidence_views is None else {'evidence_views': evidence_views}))
    from .scene_property_roofs import UNSET, add_property_roofs
    add_property_roofs(prediction, image, segmenter, root=root, parcels=parcel_data if parcel_data is not None else UNSET)
    prediction.setdefault('analysis', {}).update(input_geometry='imagery-only' if detector_only else 'imagery-and-crm-priors')
    return prediction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cohort', default='roof_training_250')
    parser.add_argument('--address-id', type=int)
    parser.add_argument('--limit', type=int, default=6)
    parser.add_argument('--prompt')
    parser.add_argument('--classes', default=','.join(PROMPTS))
    parser.add_argument('--bbox', nargs=4, type=float, metavar=('WEST', 'SOUTH', 'EAST', 'NORTH'))
    parser.add_argument('--provider', choices=['google', 'esri'], default='google')
    parser.add_argument('--scene-id')
    parser.add_argument('--expand-context', action='store_true', help='Analyse surrounding imagery while retaining the original pixels and model proposals')
    parser.add_argument('--capture-only', action='store_true')
    parser.add_argument('--house-id', type=int)
    parser.add_argument('--roof-models', action='store_true')
    parser.add_argument('--detector-only', action='store_true',
                        help='Recompute a map/house prediction from imagery without importing CRM geometry')
    parser.add_argument('--merge-plan', action='store_true',
                        help='JSON-only dry run of the canopy fragment merge and the building floor over an existing scene; writes nothing')
    parser.add_argument('--out-root', type=Path,
                        help='Isolated experiments: write every snapshot and parcel lookup under this root. '
                             'Inputs and the extraction lock stay in the working tree.')
    args = parser.parse_args()
    if args.expand_context and (not args.scene_id or args.capture_only):
        parser.error('Context expansion requires an existing scene and inference')
    if args.merge_plan:
        if not args.scene_id or not args.scene_id.replace('-', '').replace('_', '').isalnum() or len(args.scene_id) > 100:
            parser.error('The merge plan needs an existing scene id')
        scene = json.loads((Path('data/scenes')/args.scene_id/'latest.json').read_text())
        print(json.dumps(merge_plan(scene), indent=1), flush=True)
        return
    kinds = args.classes.split(',')
    if any(k not in PROMPTS for k in kinds):
        parser.error('Unknown object class')
    if not args.cohort.replace('_', '').replace('-', '').isalnum():
        parser.error('Invalid cohort name')
    if args.house_id is not None and args.house_id <= 0:
        parser.error("Choose a positive address ID")
    if sum(v is not None for v in (args.bbox, args.scene_id, args.house_id)) > 1:
        parser.error('Choose a map area or an existing scene')
    if args.detector_only and not any(v is not None for v in (args.bbox, args.scene_id, args.house_id)):
        parser.error('Detector-only inference requires a map area, scene or house')
    ids = []
    if not args.bbox and not args.scene_id and not args.house_id:
        cohort = json.loads(Path(f'data/cohorts/{args.cohort}.json').read_text())
        ids = [e['address_id'] for e in cohort['entries']]
        if args.address_id:
            if args.address_id not in ids:
                parser.error('Address is not in the selected review set')
            ids = [args.address_id]
        else:
            ids = ids[:args.limit]
    root = Path('.')
    if args.out_root is not None:
        from .scene_isolated import validate_out_root
        try:
            root = validate_out_root(args.out_root.absolute(), Path('.').resolve())
        except ValueError as error:
            parser.error(str(error))
    # Serialize heavyweight model loads across API requests and CLI runs. The
    # kernel releases this lock even if a worker is interrupted or times out.
    lock_path = Path('data/scenes/.extraction.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(75, 'Another scene extraction is running. Retry when it finishes.\n')
        scene = None
        if args.house_id:
            from .scene_house import house_source
            folder = Path('data/scenes')/f'house-{args.house_id}'
            if (folder/'latest.json').exists():
                scene = json.loads((folder/'latest.json').read_text())
                image_bytes = (folder/f"{scene['image_sha256']}.png").read_bytes()
                image = Image.open(folder/f"{scene['image_sha256']}.png").convert('RGB')
            else:
                scene, image, image_bytes = house_source(args.house_id)
                save_scene(scene, image_bytes, root)
        elif args.bbox:
            from .scene_region import capture_region
            scene, image, image_bytes = capture_region(args.bbox, args.provider)
            # Keep the captured scene usable if inference later fails.
            if not (root/'data/scenes'/scene['id']/'latest.json').exists():
                save_scene(scene, image_bytes, root)
        elif args.scene_id:
            if not args.scene_id.replace('-', '').replace('_', '').isalnum() or len(args.scene_id) > 100:
                parser.error('Invalid scene identity')
            folder = Path('data/scenes')/args.scene_id
            scene = json.loads((folder/'latest.json').read_text())
            if not scene.get('region'):
                parser.error('Use cohort and address ID for a captured roof review')
            image_bytes = (folder/f"{scene['image_sha256']}.png").read_bytes()
            image = Image.open(folder/f"{scene['image_sha256']}.png").convert('RGB')
        if args.capture_only:
            if scene is None:
                parser.error('Capture requires a map area')
            print(json.dumps({'scene': scene['id'], 'objects': len(scene['objects'])}), flush=True)
            return
        segmenter = Sam3TextSegmenter(Path('scripts/ml/models/sam3'))
        if scene is not None:
            if args.expand_context:
                from .scene_expand import analyse_expansion
                scene, image, image_bytes = analyse_expansion(scene, image_bytes, segmenter, kinds=kinds,
                                                              roof_models=args.roof_models, root=root)
            else:
                scene = analyse_scene(scene, image, segmenter, kinds=kinds, custom_prompt=args.prompt,
                                      roof_models=args.roof_models, detector_only=args.detector_only, root=root)
            save_scene(scene, image_bytes, root)
            print(json.dumps({'scene': scene['id'], 'objects': len(scene['objects']), 'scans': scene['scans']}), flush=True)
        for address_id in ids:
            scene = build_scene(address_id, args.cohort, segmenter, kinds=kinds, custom_prompt=args.prompt, roof_models=args.roof_models,
                                out_root=None if args.out_root is None else root)
            print(json.dumps({'scene': scene['id'], 'objects': len(scene['objects']), 'scans': scene['scans']}), flush=True)


if __name__ == '__main__':
    main()
