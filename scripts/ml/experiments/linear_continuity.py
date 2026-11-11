"""Linear-continuity SIDECAR prototype for road / footpath / driveway fragments (2026-10-03). Read-only over data/scenes.

Reviewer direction (verbatim, 2026-10-03 13:55 AEST): "the modelling needs to be better at maintaining continuous
road/pathway/driveways rather than the little fragments."

What it does, per scene and per layer (model = latest.json objects, review = review.json objects):
  merge mode     same-class linear pieces are joined into continuous features by a guarded vector closing modelled on
                 scene_extract.merge_canopy_fragments (pure, order-stable, ids kept in `merged_from`, nothing written to the
                 scene): for the two nearest clusters of one class, closed = unary_union(a.buffer(d), b.buffer(d)).buffer(-d)
                 with d = D_CLASS_M[kind] (driveway .6 m, footpath .8 m, road 1.5 m; closing radius d bridges a gap of up to
                 2 d); only the parts of closed - (a | b) that touch BOTH sides are added (concavity fills are not), so the
                 original outlines stay as they were and holes are kept. GUARDS, each refusal recorded with its reason:
                   area_ratio      feature area must stay <= 1.15 x the union of its original pieces (cumulative, so a chain of
                                   specks cannot build up a bridge network);
                   bridge_width    the bridge's extent perpendicular to the gap direction (its cross-section across the gap)
                                   must be <= the area-weighted median piece width of that class in the scene, or under
                                   the default width rule the widest of the pieces joined if that is larger (a strip
                                   joining two parallel driveways has a cross-section of their overlap length and is
                                   refused; a butt joint's is the face width);
                   crosses_rejected the bridge may not intersect a reviewer-rejected piece of the class; rejected pieces never
                                   take part in a merge;
                   no_bridge / not_connected  the closing did not connect the two sides (gap over the reach or geometry).
                 Different classes are never merged (the merge runs per class).
  centreline mode a shapely-only skeleton (Voronoi edges of the densified boundary that lie inside the polygon, longest path
                 through the largest connected component via networkx, both already in the venv) giving skeleton length and
                 mean width (area / centreline length and 2 x the mean skeleton-to-boundary distance) per feature.

Every figure is a count or a geometry; agreement is never accuracy; nothing here is promoted into latest.json / review.json /
confidence.*. Outputs under data/reviews/eyes/experiments/linear-continuity-2026-10-03/prototype/ only.

Run from the repo root:
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_continuity --scenes win-67c56bcab05af7a4 ...
    scripts/ml/.venv/bin/python -m scripts.ml.experiments.linear_continuity --default-set
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import networkx as nx
import numpy as np
import shapely
from PIL import Image, ImageDraw, ImageFont
from shapely import make_valid, prepared
from shapely.geometry import MultiPoint, Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[3]
SCENES = ROOT/'data/scenes'
OUT = ROOT/'data/reviews/eyes/experiments/linear-continuity-2026-10-03/prototype'
VERSION = 'linear-continuity-prototype-v1'
DIRECTION = 'the modelling needs to be better at maintaining continuous road/pathway/driveways rather than the little fragments.'

LINEAR = ('road', 'footpath', 'driveway')
D_CLASS_M = {'driveway': .6, 'footpath': .8, 'road': 1.5}
GUARDS = {'area_ratio': 1.15, 'width_factor': 1.5, 'width_rule': 'median-or-joined'}
# width_factor 1.5: a butt joint's face, measured with the closing's fillets, runs 1.3-1.5 x the piece's skeleton mean width
# (win-67c56bcab05af7a4 confirmed footpath pairs: 3.22 / 3.29 m faces on pieces of 1.8-2.5 m mean width); a strip alongside two
# parallel pieces or a 15.8 m front across a 1.3 m shadow gap (win-fa2e955f73294188 road) is still refused. The literal task
# wording (bridge width <= 1.0 x median piece width) runs as the sensitivity configuration --width-rule median --width-factor 1.0.
# width_rule 'median': the bridge may be no wider than the area-weighted median piece width of the class in the scene (the
# task's wording). 'median-or-joined' (default): ... nor wider than the widest piece it joins — a butt joint's bridge is as
# wide as the face of the wider piece, which exceeds the median for half of all pieces (both reviewer-confirmed touching
# footpath pairs on win-67c56bcab05af7a4 were refused under 'median'); a strip alongside two parallel pieces has a
# cross-section of their overlap length and is still refused.
LIVE_EXCLUDED = ('superseded', 'rejected')
TOUCH_TOL_PX = .5
FRAME_TOL_PX = 1.0

# The census's 8 most fragmented windows, the reviewer's window, and 5 review-set scenes with confirmed linear pieces.
DEFAULT_WINDOWS = ['win-150d924743036f24', 'win-17b5c9b30a5d3ff9', 'win-da0e5d5badc3b3ba', 'win-d24efc00bcd6a3e4',
                   'win-fa2e955f73294188', 'win-1dff35d6922a4132', 'win-ab373041656385bf', 'win-4cc07d6908cc688b',
                   'win-67c56bcab05af7a4']
DEFAULT_REVIEW_SET = ['house-25910', 'roof_training_250-30371', 'roof_training_250-29761', 'roof_training_250-25315',
                      'roof_training_250-29582']

# Render palette: before = thin distinct colour per piece (cycling), after = one bold colour per feature in the class's hue family.
PIECE_PALETTE = [(255, 80, 80), (80, 200, 255), (255, 190, 0), (160, 255, 90), (255, 110, 255), (0, 255, 200), (255, 150, 60),
                 (130, 150, 255), (255, 255, 120), (0, 200, 120), (255, 60, 160), (120, 230, 255), (220, 160, 255), (255, 210, 150),
                 (90, 255, 160), (255, 120, 120)]
FEATURE_FAMILY = {'road': [(235, 30, 30), (255, 95, 95), (190, 0, 60), (255, 60, 150)],
                  'driveway': [(255, 150, 0), (255, 205, 0), (255, 115, 50), (225, 170, 70)],
                  'footpath': [(0, 215, 255), (70, 140, 255), (0, 255, 190), (130, 200, 255)]}
CLASS_LETTER = {'road': 'R', 'footpath': 'F', 'driveway': 'D'}
REJECTED_COLOUR = (178, 178, 178)
LEGEND_BEFORE = 'BEFORE: thin line = one model piece (colour cycles per piece); letter r/f/d = class, * = reviewer-confirmed, ~ = needs_adjustment; grey dotted = rejected'
LEGEND_AFTER = 'AFTER: bold line = one merged feature (red family = road, orange = driveway, cyan = footpath); label = class+index, n = pieces folded, m2; grey dotted = rejected (never merged)'


# ----------------------------------------------------------------------------------------------------------------- geometry

def iter_polygons(geometry):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == 'Polygon':
        yield geometry
    elif geometry.geom_type in ('MultiPolygon', 'GeometryCollection'):
        for part in geometry.geoms:
            yield from iter_polygons(part)


def polygon_of(obj):
    ring = obj.get('ring') or []
    if len(ring) < 3:
        return None
    holes = [[tuple(p[:2]) for p in h] for h in (obj.get('holes') or []) if len(h) >= 3]
    geo = make_valid(Polygon([tuple(p[:2]) for p in ring], holes))
    polys = list(iter_polygons(geo))
    if not polys:
        return None
    geo = unary_union(polys)
    return None if geo.is_empty else geo


def pixel_spacing(scene):
    """Metres per image pixel: sqrt(|det| of the linear part of pixel_to_meters) — the same arithmetic as scene_recapture.pixel_spacing."""
    affine = np.asarray(scene['pixel_to_meters'], dtype=float)
    return math.sqrt(abs(float(np.linalg.det(affine[:2]))))


def min_rect_sides(geom):
    with np.errstate(all='ignore'):
        rect = geom.minimum_rotated_rectangle
    if rect.geom_type != 'Polygon':
        return 0., 0.
    pts = list(rect.exterior.coords)
    a = math.dist(pts[0], pts[1])
    b = math.dist(pts[1], pts[2])
    if not (math.isfinite(a) and math.isfinite(b)):
        minx, miny, maxx, maxy = geom.bounds
        a, b = maxx-minx, maxy-miny
    return min(a, b), max(a, b)


def ring_touches(geom, width, height, tol=FRAME_TOL_PX):
    minx, miny, maxx, maxy = geom.bounds
    return minx <= tol or miny <= tol or maxx >= width-tol or maxy >= height-tol


def weighted_median(values_weights):
    items = sorted(values_weights)
    total = sum(w for _, w in items)
    if total <= 0:
        return 0.
    acc = 0.
    for value, weight in items:
        acc += weight
        if acc >= total/2:
            return value
    return items[-1][0]


def rings_of(geom):
    ring = [[round(x, 2), round(y, 2)] for x, y in geom.exterior.coords[:-1]]
    holes = [[[round(x, 2), round(y, 2)] for x, y in h.coords[:-1]] for h in geom.interiors]
    return ring, holes


# ------------------------------------------------------------------------------------------------------------------- pieces

def load_scene(scene_id):
    scene_dir = SCENES/scene_id
    latest = json.loads((scene_dir/'latest.json').read_text())
    review_path = scene_dir/'review.json'
    review = json.loads(review_path.read_text()) if review_path.exists() else None
    image_path = scene_dir/f"{latest['image_sha256']}.png"
    return latest, review, image_path


def linear_pieces(objects, spacing, width, height):
    pieces = []
    for obj in objects:
        if not isinstance(obj, dict) or obj.get('kind') not in LINEAR:
            continue
        geom = polygon_of(obj)
        if geom is None:
            continue
        short, long = min_rect_sides(geom)
        pieces.append({'id': obj['id'], 'kind': obj['kind'], 'status': obj.get('status', 'candidate'), 'score': obj.get('score'),
                       'source': obj.get('source'), 'geom': geom, 'area_m2': geom.area*spacing*spacing,
                       'width_m': short*spacing, 'length_m': long*spacing, 'touches_frame': ring_touches(geom, width, height),
                       'geometry_reviewed': bool(obj.get('geometry_reviewed'))})
    return pieces


# -------------------------------------------------------------------------------------------------------------------- merge

def seam_length(bridge, side):
    """Length of `side`'s boundary that the bridge is attached to (measured on the side's own boundary, so a sliver thinner
    than the touch tolerance is not double counted)."""
    return side.boundary.intersection(bridge.buffer(.02)).length


def cross_section(bridge, ga, gb, gap_px):
    """Width of the bridge across the gap: the extent of the bridge's vertices along the axis perpendicular to the gap
    direction (the shortest line between the two sides). A butt joint gives about the face width of the wider piece; a
    strip running alongside two parallel pieces gives their overlap length; a speck against a face gives the speck."""
    if gap_px <= 0:
        return 0.
    line = shapely.shortest_line(ga, gb)
    (x0, y0), (x1, y1) = line.coords[0], line.coords[-1]
    norm = math.hypot(x1-x0, y1-y0)
    if norm <= 0:
        return 0.
    ux, uy = -(y1-y0)/norm, (x1-x0)/norm
    projections = [x*ux+y*uy for part in iter_polygons(bridge) for x, y in part.exterior.coords]
    return (max(projections)-min(projections)) if projections else 0.


def try_bridge(ga, gb, d_px, tol_px=TOUCH_TOL_PX):
    """Closing of two cluster geometries with radius d_px; returns (merged, bridge, info) or (None, None, {'reason': ...}).

    Reach: two parallel faces connect up to a gap of 2 d; a small speck against a face connects only below about 4 d / 3
    (closing geometry), so 'no_bridge' refusals at gaps under 2 d are expected for specks.
    """
    parts = unary_union([ga, gb])
    gap_px = ga.distance(gb)
    if gap_px <= 1e-9:
        merged = parts
        if merged.geom_type != 'Polygon':
            merged = merged.buffer(.01).buffer(-.01)
        if merged.geom_type != 'Polygon':
            return None, None, {'reason': 'not_connected', 'gap_px': gap_px}
        return merged, None, {'gap_px': 0., 'bridge_area_px': 0., 'seam_a_px': 0., 'seam_b_px': 0., 'bridge_width_px': 0.}
    closed = unary_union([ga.buffer(d_px, quad_segs=8), gb.buffer(d_px, quad_segs=8)]).buffer(-d_px, quad_segs=8)
    extra = closed.difference(parts)
    connecting = [g for g in iter_polygons(extra) if g.area > 1e-9 and g.distance(ga) <= tol_px and g.distance(gb) <= tol_px]
    if not connecting:
        return None, None, {'reason': 'no_bridge', 'gap_px': gap_px}
    bridge = unary_union(connecting)
    merged = unary_union([parts, bridge])
    if merged.geom_type != 'Polygon':
        merged = merged.buffer(.01).buffer(-.01)
    if merged.geom_type != 'Polygon':
        return None, None, {'reason': 'not_connected', 'gap_px': gap_px}
    seam_a, seam_b = seam_length(bridge, ga), seam_length(bridge, gb)
    width = cross_section(bridge, ga, gb, gap_px)
    if width <= 0:
        width = min(seam_a, seam_b)
    return merged, bridge, {'gap_px': gap_px, 'bridge_area_px': bridge.area, 'seam_a_px': seam_a, 'seam_b_px': seam_b,
                            'bridge_width_px': width}


def merge_class(pieces, rejected, kind, spacing, d_m, guards):
    """Guarded agglomerative closing over the live pieces of one class. Pure: returns (features, merges, refusals)."""
    d_px = d_m/spacing
    reach_px = 2*d_px
    live = [p for p in pieces if p['kind'] == kind and p['status'] not in LIVE_EXCLUDED]
    blockers = [p['geom'] for p in rejected if p['kind'] == kind]
    median_width_m = weighted_median([(p['width_m'], p['area_m2']) for p in live]) if live else 0.
    parent = list(range(len(live)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    clusters = {i: {'ids': [p['id']], 'geom': p['geom'], 'base_area_px': p['geom'].area, 'merges': [], 'width_m': p['width_m']} for i, p in enumerate(live)}
    pairs = []
    for i in range(len(live)):
        for j in range(i+1, len(live)):
            gap = live[i]['geom'].distance(live[j]['geom'])
            if gap <= reach_px:
                pairs.append((gap, i, j))
    pairs.sort()
    merges, refusals = [], []
    for gap_px, i, j in pairs:
        ci, cj = find(i), find(j)
        if ci == cj:
            continue
        a, b = clusters[ci], clusters[cj]
        merged, bridge, info = try_bridge(a['geom'], b['geom'], d_px)
        record = {'kind': kind, 'a_ids': sorted(a['ids']), 'b_ids': sorted(b['ids']), 'pair': [live[i]['id'], live[j]['id']],
                  'piece_gap_m': round(gap_px*spacing, 3), 'cluster_gap_m': round(info.get('gap_px', gap_px)*spacing, 3)}
        if merged is None:
            refusals.append({**record, 'reason': info['reason']})
            continue
        base_px = a['base_area_px']+b['base_area_px']
        ratio = merged.area/base_px if base_px else float('inf')
        bridge_width_m = info['bridge_width_px']*spacing
        width_limit_m = guards['width_factor']*(median_width_m if guards.get('width_rule', 'median') == 'median'
                                                else max(median_width_m, a['width_m'], b['width_m']))
        record.update({'bridge_area_m2': round(info['bridge_area_px']*spacing*spacing, 3), 'bridge_width_m': round(bridge_width_m, 3),
                       'seam_a_m': round(info['seam_a_px']*spacing, 3), 'seam_b_m': round(info['seam_b_px']*spacing, 3),
                       'area_ratio': round(ratio, 4), 'median_width_m': round(median_width_m, 3), 'width_limit_m': round(width_limit_m, 3)})
        if ratio > guards['area_ratio']:
            refusals.append({**record, 'reason': 'area_ratio'})
            continue
        if bridge is not None and bridge_width_m > width_limit_m:
            refusals.append({**record, 'reason': 'bridge_width'})
            continue
        if bridge is not None and any(bridge.intersection(blocker).area > 1e-9 for blocker in blockers):
            refusals.append({**record, 'reason': 'crosses_rejected'})
            continue
        parent[cj] = ci
        clusters[ci] = {'ids': a['ids']+b['ids'], 'geom': merged, 'base_area_px': base_px, 'merges': a['merges']+b['merges']+[record],
                        'width_m': max(a['width_m'], b['width_m'])}
        del clusters[cj]
        merges.append(record)
    by_id = {p['id']: p for p in live}
    features = []
    for root in sorted(clusters, key=lambda r: -clusters[r]['geom'].area):
        c = clusters[root]
        geom = c['geom']
        if geom.geom_type != 'Polygon':
            geom = max(iter_polygons(geom), key=lambda g: g.area)
        ids = sorted(c['ids'])
        statuses = Counter(by_id[i]['status'] for i in ids)
        features.append({'id': 'lc-'+hashlib.sha256((kind+':'+','.join(ids)).encode()).hexdigest()[:16], 'kind': kind,
                         'merged_from': ids, 'pieces': len(ids), 'statuses': dict(statuses), 'geom': geom,
                         'area_m2': geom.area*spacing*spacing, 'pieces_area_m2': c['base_area_px']*spacing*spacing,
                         'gaps_closed_m': [m['cluster_gap_m'] for m in c['merges']], 'merges': c['merges']})
    return features, merges, refusals, median_width_m


# --------------------------------------------------------------------------------------------------------------- centreline

def centreline(geom, spacing, step_m=.3):
    """Skeleton length and mean width of one polygon, shapely + networkx only.

    Densify the boundary, take the Voronoi edges that lie inside the polygon, keep the largest connected component and
    measure the longest path through it (the centreline). mean_width_m = area / centreline length;
    skeleton_width_m = 2 x mean distance from the path's vertices to the boundary. Tiny shapes fall back to the minimum
    rotated rectangle (long side as length).
    """
    area_m2 = geom.area*spacing*spacing
    short, long = min_rect_sides(geom)
    fallback = {'method': 'min_rect', 'centreline_m': round(long*spacing, 3), 'skeleton_total_m': round(long*spacing, 3),
                'mean_width_m': round(area_m2/(long*spacing), 3) if long > 0 else 0., 'skeleton_width_m': round(short*spacing, 3),
                'path': [list(geom.representative_point().coords[0])]}
    step_px = max(1.5, step_m/spacing)
    boundary = shapely.segmentize(geom.boundary, step_px)
    coords = {tuple(round(v, 4) for v in c) for line in getattr(boundary, 'geoms', [boundary]) for c in line.coords}
    if len(coords) < 6 or geom.area < 4*step_px*step_px:
        return fallback
    edges = shapely.voronoi_polygons(MultiPoint(list(coords)), only_edges=True)
    prep = prepared.prep(geom)
    graph = nx.Graph()
    for edge in getattr(edges, 'geoms', [edges]):
        if edge.is_empty or not prep.contains(edge):
            continue
        pts = list(edge.coords)
        for u, v in zip(pts[:-1], pts[1:], strict=False):
            u, v = tuple(round(c, 6) for c in u), tuple(round(c, 6) for c in v)
            if u != v:
                graph.add_edge(u, v, weight=math.dist(u, v))
    if graph.number_of_edges() == 0:
        return fallback
    component = max(nx.connected_components(graph), key=lambda nodes: graph.subgraph(nodes).size(weight='weight'))
    sub = graph.subgraph(component)
    start = next(iter(component))
    far = nx.single_source_dijkstra_path_length(sub, start, weight='weight')
    end_a = max(far, key=far.get)
    far_b = nx.single_source_dijkstra_path_length(sub, end_a, weight='weight')
    end_b = max(far_b, key=far_b.get)
    path = nx.dijkstra_path(sub, end_a, end_b, weight='weight')
    length_px = far_b[end_b]
    if length_px <= 0:
        return fallback
    boundary_line = geom.boundary
    distances = [boundary_line.distance(Point(p)) for p in path[:: max(1, len(path)//64)]]
    return {'method': 'voronoi_skeleton', 'centreline_m': round(length_px*spacing, 3), 'skeleton_total_m': round(sub.size(weight='weight')*spacing, 3),
            'mean_width_m': round(area_m2/(length_px*spacing), 3), 'skeleton_width_m': round(2*float(np.mean(distances))*spacing, 3),
            'path': [[round(x, 1), round(y, 1)] for x, y in path[:: max(1, len(path)//200)]]}


# ------------------------------------------------------------------------------------------------------------------- render

def load_font(size):
    for candidate in ('/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/Helvetica.ttc', '/System/Library/Fonts/Supplemental/Arial.ttf'):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw_geo(draw, geo, colour, width, scale, offset=(0, 0), dotted=False):
    for part in iter_polygons(geo):
        for ring in [part.exterior, *part.interiors]:
            pts = [(x*scale+offset[0], y*scale+offset[1]) for x, y in ring.coords]
            if dotted:
                for a, b in zip(pts[:-1], pts[1:], strict=False):
                    draw_dotted(draw, a, b, colour, width)
            else:
                draw.line(pts, fill=colour, width=width, joint='curve')


def draw_dotted(draw, a, b, colour, width, on=3, off=5):
    (x0, y0), (x1, y1) = a, b
    length = math.hypot(x1-x0, y1-y0)
    if length == 0:
        return
    ux, uy = (x1-x0)/length, (y1-y0)/length
    t = 0.
    while t < length:
        s = min(length, t+on)
        draw.line([(x0+ux*t, y0+uy*t), (x0+ux*s, y0+uy*s)], fill=colour, width=width)
        t += on+off


def halo_text(draw, xy, text, font, colour, size):
    w = draw.textlength(text, font=font)
    x = max(2, min(xy[0], size[0]-w-2))
    y = max(2, min(xy[1], size[1]-font.size-4))
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
        draw.text((x+dx, y+dy), text, fill=(0, 0, 0), font=font)
    draw.text((x, y), text, fill=colour, font=font)


def wrap_lines(draw, lines, font, width):
    out = []
    for line in lines:
        current = ''
        for word in line.split(' '):
            trial = (current+' '+word).strip()
            if draw.textlength(trial, font=font) <= width or not current:
                current = trial
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
    return out


def label_point(geom):
    p = geom.representative_point()
    return p.x, p.y


def paint_before(canvas, pieces, rejected, scale, offset, font, kinds=LINEAR):
    draw = ImageDraw.Draw(canvas)
    index = 0
    for piece in sorted(pieces, key=lambda p: -p['area_m2']):
        if piece['kind'] not in kinds:
            continue
        colour = PIECE_PALETTE[index % len(PIECE_PALETTE)]
        index += 1
        draw_geo(draw, piece['geom'], colour, 2, scale, offset)
        mark = '*' if piece['status'] == 'confirmed' else '~' if piece['status'] == 'needs_adjustment' else ''
        x, y = label_point(piece['geom'])
        halo_text(draw, (x*scale+offset[0]-6, y*scale+offset[1]-7), f"{piece['kind'][0]}{mark}", font, colour, canvas.size)
    for piece in rejected:
        if piece['kind'] in kinds:
            draw_geo(draw, piece['geom'], REJECTED_COLOUR, 1, scale, offset, dotted=True)


def paint_after(canvas, features, rejected, scale, offset, font, kinds=LINEAR):
    draw = ImageDraw.Draw(canvas)
    counters = Counter()
    for feature in features:
        kind = feature['kind']
        if kind not in kinds:
            continue
        counters[kind] += 1
        colour = FEATURE_FAMILY[kind][(counters[kind]-1) % len(FEATURE_FAMILY[kind])]
        draw_geo(draw, feature['geom'], colour, 4, scale, offset)
        x, y = label_point(feature['geom'])
        text = (f"{CLASS_LETTER[kind]}{counters[kind]} n={feature['pieces']} {feature['area_m2']:.1f}" if feature['area_m2'] >= 1
                else f"{CLASS_LETTER[kind]}{counters[kind]}")
        halo_text(draw, (x*scale+offset[0]-10, y*scale+offset[1]-7), text, font, colour, canvas.size)
    for piece in rejected:
        if piece['kind'] in kinds:
            draw_geo(draw, piece['geom'], REJECTED_COLOUR, 1, scale, offset, dotted=True)


def render_scene(result, image_path, out_path):
    image = Image.open(image_path).convert('RGB')
    width, height = image.size
    panel = 1024
    scale = panel/width
    base = image.resize((panel, int(height*scale)), Image.BILINEAR)
    header_h, footer_h, gutter = 76, 64, 12
    canvas = Image.new('RGB', (2*panel+gutter, header_h+base.height+footer_h), (18, 18, 18))
    before, after = base.copy(), base.copy()
    font = load_font(13)
    paint_before(before, result['_pieces'], result['_rejected'], scale, (0, 0), font)
    paint_after(after, result['_features'], result['_rejected'], scale, (0, 0), font)
    canvas.paste(before, (0, header_h))
    canvas.paste(after, (panel+gutter, header_h))
    draw = ImageDraw.Draw(canvas)
    big, small = load_font(18), load_font(12)
    counts = ' · '.join(f"{k} {result['classes'][k]['pieces_before']}->{result['classes'][k]['features_after']}" for k in LINEAR if k in result['classes'])
    absorbed = sum(result['classes'][k]['specks_absorbed'] for k in result['classes'])
    if absorbed:
        counts += f' · specks absorbed {absorbed} (< {result["absorb_m2"]} m2)'
    refused = sum(result['classes'][k]['refusals_total'] for k in result['classes'])
    draw.text((8, 6), f"{result['scene']} · {result['layer']} layer · {width}px · {result['pixel_m']:.3f} m/px · pieces->features: {counts} · merges {result['merges_total']} · refused {refused}", fill=(255, 255, 255), font=big)
    d = result['d_class_m']
    width_rule = 'median piece width' if result['guards'].get('width_rule') == 'median' else 'max(median piece width, width of the pieces joined)'
    absorb_note = f" · specks < {result['absorb_m2']} m2 folded into a 10x same-class neighbour within 2d (outline unchanged)" if result['absorb_m2'] else ''
    draw.text((8, 32), f"d (closing radius; parallel faces bridge up to 2d): driveway {d['driveway']} m, footpath {d['footpath']} m, road {d['road']} m · guards: area <= {result['guards']['area_ratio']} x parts, bridge width <= {result['guards']['width_factor']} x {width_rule}, never across a rejected piece, never across classes{absorb_note}", fill=(210, 210, 210), font=small)
    draw.text((8, 50), 'BEFORE (model pieces)', fill=(255, 255, 255), font=big)
    draw.text((panel+gutter+8, 50), 'AFTER (prototype features; sidecar only, nothing promoted)', fill=(255, 255, 255), font=big)
    y = header_h+base.height+4
    for line in wrap_lines(draw, [LEGEND_BEFORE+'  ||  '+LEGEND_AFTER, 'Counts and geometry only; agreement is never accuracy. '+result['note']], small, canvas.width-16)[:4]:
        draw.text((8, y), line, fill=(210, 210, 210), font=small)
        y += 15
    canvas.save(out_path)
    return out_path


def class_tile(result, image_path, kind, size=400):
    """Before | after crop of one class in one scene, each `size` px square; None when the scene has no piece of the class."""
    pieces = [p for p in result['_pieces'] if p['kind'] == kind]
    rejected = [p for p in result['_rejected'] if p['kind'] == kind]
    if not pieces and not rejected:
        return None
    image = Image.open(image_path).convert('RGB')
    width, height = image.size
    minx, miny, maxx, maxy = unary_union([p['geom'] for p in [*pieces, *rejected]]).bounds
    side = max(maxx-minx, maxy-miny)*1.15+20
    side = min(max(side, 160), max(width, height))
    cx, cy = (minx+maxx)/2, (miny+maxy)/2
    x0 = min(max(0, cx-side/2), max(0, width-side))
    y0 = min(max(0, cy-side/2), max(0, height-side))
    crop_box = (int(x0), int(y0), int(min(width, x0+side)), int(min(height, y0+side)))
    crop = image.crop(crop_box)
    scale = size/max(crop.width, crop.height)
    base = crop.resize((max(1, int(crop.width*scale)), max(1, int(crop.height*scale))), Image.BILINEAR)
    font = load_font(11)
    offset = (-crop_box[0]*scale, -crop_box[1]*scale)
    before, after = base.copy(), base.copy()
    paint_before(before, pieces, rejected, scale, offset, font, kinds=(kind,))
    paint_after(after, [f for f in result['_features'] if f['kind'] == kind], rejected, scale, offset, font, kinds=(kind,))
    tile = Image.new('RGB', (2*size+6, size+22), (18, 18, 18))
    tile.paste(before, (0, 22))
    tile.paste(after, (size+6, 22))
    stats = result['classes'][kind]
    draw = ImageDraw.Draw(tile)
    text = (f"{result['scene']} {result['layer']} {kind}: {stats['pieces_before']} pieces -> {stats['features_after']} features, "
            f"confirmed {stats['confirmed_pieces']} (merged {stats['confirmed_pieces_merged']}), refused {stats['refusals_total']}")
    draw.text((4, 4), text[:150], fill=(255, 255, 255), font=font)
    return tile


def contact_sheet(tiles, kind, out_path, cols=2):
    if not tiles:
        return None
    tw, th = tiles[0].size
    rows = math.ceil(len(tiles)/cols)
    header = 64
    sheet = Image.new('RGB', (cols*tw+(cols-1)*10, header+rows*(th+10)), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    big, small = load_font(18), load_font(12)
    draw.text((8, 6), f"linear continuity prototype · class {kind} · before (thin, one colour per piece) | after (bold, one colour per feature) · d = {D_CLASS_M[kind]} m", fill=(255, 255, 255), font=big)
    draw.text((8, 30), LEGEND_BEFORE[:200], fill=(210, 210, 210), font=small)
    draw.text((8, 45), LEGEND_AFTER[:200], fill=(210, 210, 210), font=small)
    for index, tile in enumerate(tiles):
        r, c = divmod(index, cols)
        sheet.paste(tile, (c*(tw+10), header+r*(th+10)))
    sheet.save(out_path)
    return out_path


# ---------------------------------------------------------------------------------------------------------------------- run

def absorb_specks(features, rejected, kind, spacing, d_m, absorb_m2, container_ratio=10.):
    """Optional second step modelled on scene_extract.merge_canopy_fragments, proximity instead of containment: a leftover
    feature under absorb_m2 within the closing reach (2 d) of a same-class feature >= container_ratio x its area is folded
    into that container WITHOUT changing the container's outline (the speck's ids join merged_from; a record carries the
    gap). Refused when the straight line between the two nearest points crosses a rejected piece. Mutates `features`."""
    reach_px = 2*d_m/spacing
    blockers = [p['geom'] for p in rejected if p['kind'] == kind]
    absorbed = []
    for speck in sorted([f for f in features if f['area_m2'] < absorb_m2], key=lambda f: f['area_m2']):
        best = None
        for container in features:
            if container is speck or container['area_m2'] < container_ratio*speck['area_m2']:
                continue
            gap = speck['geom'].distance(container['geom'])
            if gap <= reach_px and (best is None or gap < best[0]):
                best = (gap, container)
        if best is None:
            continue
        gap, container = best
        line = shapely.shortest_line(speck['geom'], container['geom'])
        if any(line.intersects(blocker) for blocker in blockers):
            continue
        status = next(iter(speck['statuses'])) if len(speck['statuses']) == 1 else 'mixed'
        record = {'kind': kind, 'absorbed_ids': speck['merged_from'], 'into': container['id'], 'gap_m': round(gap*spacing, 3),
                  'speck_m2': round(speck['area_m2'], 3), 'container_m2': round(container['area_m2'], 2), 'status': status, 'rule': 'absorb_speck'}
        container['merged_from'] = sorted(container['merged_from']+speck['merged_from'])
        container['pieces'] += speck['pieces']
        for key, value in speck['statuses'].items():
            container['statuses'][key] = container['statuses'].get(key, 0)+value
        container['merges'] = container['merges']+[record]
        container['gaps_closed_m'] = container['gaps_closed_m']+[record['gap_m']]
        features.remove(speck)
        absorbed.append(record)
    return absorbed


def analyse_layer(scene_id, latest, objects, layer, d_class, guards, with_centreline, absorb_m2=0.):
    width, height = latest['image_width'], latest['image_height']
    spacing = pixel_spacing(latest)
    all_pieces = linear_pieces(objects, spacing, width, height)
    live = [p for p in all_pieces if p['status'] not in LIVE_EXCLUDED]
    rejected = [p for p in all_pieces if p['status'] == 'rejected']
    if with_centreline:
        # The guard's piece width is the skeleton mean width (area / centreline length) when the skeleton is computed:
        # the minimum-rotated-rectangle short side of an L-shaped road is not its width (23 m on win-da0e5d5badc3b3ba).
        for p in live:
            p['centreline'] = centreline(p['geom'], spacing)
            p['min_rect_width_m'] = p['width_m']
            if p['centreline']['mean_width_m'] > 0:
                p['width_m'] = min(p['width_m'], p['centreline']['mean_width_m'])
    classes, features_all, merges_all, refusals_all = {}, [], [], []
    for kind in LINEAR:
        kind_pieces = [p for p in live if p['kind'] == kind]
        if not kind_pieces and not any(p['kind'] == kind for p in rejected):
            continue
        features, merges, refusals, median_width = merge_class(live, rejected, kind, spacing, d_class[kind], guards)
        absorbed = absorb_specks(features, rejected, kind, spacing, d_class[kind], absorb_m2) if absorb_m2 else []
        multi = [f for f in features if f['pieces'] > 1]
        confirmed_ids = {p['id'] for p in kind_pieces if p['status'] == 'confirmed'}
        adjust_ids = {p['id'] for p in kind_pieces if p['status'] == 'needs_adjustment'}
        confirmed_merged = sum(1 for f in multi for i in f['merged_from'] if i in confirmed_ids)
        gaps = [m['cluster_gap_m'] for m in merges]
        reasons = Counter(r['reason'] for r in refusals)
        area_before = unary_union([p['geom'] for p in kind_pieces]).area*spacing*spacing if kind_pieces else 0.
        area_after = sum(f['area_m2'] for f in features)
        classes[kind] = {'pieces_before': len(kind_pieces), 'features_after': len(features), 'pieces_folded': sum(f['pieces'] for f in multi),
                         'features_multi_piece': len(multi), 'merges_accepted': len(merges), 'refusals_total': len(refusals),
                         'refusals_by_reason': dict(reasons), 'area_before_m2': round(area_before, 2), 'area_after_m2': round(area_after, 2),
                         'bridge_area_m2': round(sum(m['bridge_area_m2'] for m in merges if 'bridge_area_m2' in m), 3),
                         'area_change_pct': round(100*(area_after-area_before)/area_before, 2) if area_before else 0.,
                         'median_piece_width_m': round(median_width, 3),
                         'gaps_closed_m': {'n': len(gaps), 'median': round(float(np.median(gaps)), 3) if gaps else None, 'max': round(max(gaps), 3) if gaps else None,
                                           'zero_gap_touching': sum(1 for g in gaps if g == 0)},
                         'confirmed_pieces': len(confirmed_ids), 'confirmed_pieces_merged': confirmed_merged,
                         'features_with_2plus_confirmed': sum(1 for f in multi if sum(1 for i in f['merged_from'] if i in confirmed_ids) >= 2),
                         'features_mixing_confirmed_and_unconfirmed': sum(1 for f in multi if any(i in confirmed_ids for i in f['merged_from']) and any(i not in confirmed_ids for i in f['merged_from'])),
                         'needs_adjustment_pieces': len(adjust_ids), 'needs_adjustment_merged': sum(1 for f in multi for i in f['merged_from'] if i in adjust_ids),
                         'rejected_pieces': sum(1 for p in rejected if p['kind'] == kind),
                         'pieces_under_1m2_before': sum(1 for p in kind_pieces if p['area_m2'] < 1), 'features_under_1m2_after': sum(1 for f in features if f['area_m2'] < 1),
                         'pieces_touching_frame': sum(1 for p in kind_pieces if p['touches_frame']),
                         'specks_absorbed': len(absorbed), 'confirmed_specks_absorbed': sum(1 for a in absorbed if a['status'] == 'confirmed'),
                         'width_basis': 'skeleton mean width (area / centreline length), else min-rect short side' if with_centreline else 'min-rect short side'}
        features_all += features
        merges_all += merges
        refusals_all += refusals
    if with_centreline:
        for f in features_all:
            f['centreline'] = centreline(f['geom'], spacing)
    note = ('review layer: statuses are the reviewer\'s; rejected pieces block bridges.' if layer == 'review'
            else 'model layer: statuses as stored in latest.json (all candidate unless the model layer carries review marks).')
    return {'scene': scene_id, 'layer': layer, 'image_px': [width, height], 'pixel_m': spacing, 'd_class_m': d_class, 'guards': guards, 'absorb_m2': absorb_m2,
            'classes': classes, 'merges_total': len(merges_all), 'refusals_total': len(refusals_all), 'note': note,
            '_pieces': live, '_rejected': rejected, '_features': features_all, '_merges': merges_all, '_refusals': refusals_all}


def serialise(result, review_meta):
    def piece_row(p):
        row = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in p.items() if k not in ('geom', 'centreline')}
        if 'centreline' in p:
            row['centreline'] = {k: v for k, v in p['centreline'].items() if k != 'path'}
        return row

    def feature_row(f):
        ring, holes = rings_of(f['geom'])
        row = {'id': f['id'], 'kind': f['kind'], 'merged_from': f['merged_from'], 'pieces': f['pieces'], 'statuses': f['statuses'],
               'area_m2': round(f['area_m2'], 3), 'pieces_area_m2': round(f['pieces_area_m2'], 3), 'gaps_closed_m': f['gaps_closed_m'],
               'ring': ring, **({'holes': holes} if holes else {}), 'merges': f['merges']}
        if 'centreline' in f:
            row['centreline'] = f['centreline']
        return row

    return {'version': VERSION, 'scene': result['scene'], 'layer': result['layer'], 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'),
            'direction': DIRECTION, 'image_px': result['image_px'], 'pixel_m': round(result['pixel_m'], 5), 'review': review_meta,
            'd_class_m': result['d_class_m'], 'guards': result['guards'], 'absorb_specks_m2': result['absorb_m2'], 'classes': result['classes'],
            'before_pieces': [piece_row(p) for p in result['_pieces']], 'rejected_pieces': [piece_row(p) for p in result['_rejected']],
            'after_features': [feature_row(f) for f in result['_features']], 'merges': result['_merges'], 'refusals': result['_refusals'],
            'note': 'Sidecar only: nothing under data/scenes was written; counts and geometry, never accuracy.'}


def aggregate(results):
    agg = {}
    for layer in ('model', 'review'):
        rows = [r for r in results if r['layer'] == layer]
        per_class = {}
        for kind in LINEAR:
            stats = [r['classes'][kind] for r in rows if kind in r['classes']]
            if not stats:
                continue
            reasons = Counter()
            for s in stats:
                reasons.update(s['refusals_by_reason'])
            gaps = [g for r in rows for m in r['_merges'] if m['kind'] == kind for g in [m['cluster_gap_m']]]
            per_class[kind] = {'scenes': len(stats), **{k: sum(s[k] for s in stats) for k in (
                'pieces_before', 'features_after', 'pieces_folded', 'features_multi_piece', 'merges_accepted', 'refusals_total',
                'confirmed_pieces', 'confirmed_pieces_merged', 'features_with_2plus_confirmed', 'features_mixing_confirmed_and_unconfirmed',
                'needs_adjustment_pieces', 'needs_adjustment_merged', 'rejected_pieces', 'pieces_under_1m2_before', 'features_under_1m2_after',
                'specks_absorbed', 'confirmed_specks_absorbed')},
                'refusals_by_reason': dict(reasons), 'area_before_m2': round(sum(s['area_before_m2'] for s in stats), 1),
                'area_after_m2': round(sum(s['area_after_m2'] for s in stats), 1), 'bridge_area_m2': round(sum(s['bridge_area_m2'] for s in stats), 2),
                'gaps_closed_m': {'n': len(gaps), 'median': round(float(np.median(gaps)), 3) if gaps else None, 'p90': round(float(np.percentile(gaps, 90)), 3) if gaps else None,
                                  'max': round(max(gaps), 3) if gaps else None, 'zero_gap_touching': sum(1 for g in gaps if g == 0)}}
            before, after = per_class[kind]['area_before_m2'], per_class[kind]['area_after_m2']
            per_class[kind]['area_change_pct'] = round(100*(after-before)/before, 2) if before else 0.
        agg[layer] = {'scenes': len(rows), 'classes': per_class}
    return agg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scenes', nargs='*', default=[])
    parser.add_argument('--default-set', action='store_true', help='the census\'s 8 most fragmented windows + the reviewer\'s window + 5 review-set scenes')
    parser.add_argument('--layers', nargs='*', default=['model', 'review'], choices=['model', 'review'])
    parser.add_argument('--mode', default='both', choices=['merge', 'centreline', 'both'])
    parser.add_argument('--render-layer', default='review', choices=['model', 'review', 'both'], help='which layer the PNGs show (default review, falling back to model when the review layer has no linear piece)')
    parser.add_argument('--out', default=str(OUT))
    parser.add_argument('--d', nargs='*', default=[], help='override closing radius, e.g. road=1.0 footpath=0.5')
    parser.add_argument('--no-render', action='store_true')
    parser.add_argument('--width-rule', default=GUARDS['width_rule'], choices=['median', 'median-or-joined'])
    parser.add_argument('--width-factor', type=float, default=GUARDS['width_factor'])
    parser.add_argument('--absorb-specks', type=float, default=0., metavar='M2', help='optional second step: fold leftover features under M2 m2 into a same-class feature >= 10x their area within 2 d (outline unchanged); 0 = off')
    args = parser.parse_args(argv)
    d_class = dict(D_CLASS_M)
    guards = {**GUARDS, 'width_rule': args.width_rule, 'width_factor': args.width_factor}
    for item in args.d:
        kind, value = item.split('=')
        d_class[kind] = float(value)
    scenes = list(args.scenes) + (DEFAULT_WINDOWS+DEFAULT_REVIEW_SET if args.default_set else [])
    if not scenes:
        parser.error('give --scenes or --default-set')
    out = Path(args.out)
    (out/'renders').mkdir(parents=True, exist_ok=True)
    with_centreline = args.mode in ('centreline', 'both')
    results, renders, tiles = [], [], defaultdict(list)
    for scene_id in scenes:
        latest, review, image_path = load_scene(scene_id)
        review_meta = None
        if review is not None:
            review_meta = {'revision': review.get('revision'), 'updated_at': review.get('updated_at'),
                           'model_signature_matches_latest': review.get('model_signature') == latest.get('model_signature')}
        per_layer = {}
        for layer in args.layers:
            if layer == 'review' and review is None:
                continue
            objects = latest.get('objects', []) if layer == 'model' else review.get('objects', [])
            result = analyse_layer(scene_id, latest, objects, layer, d_class, guards, with_centreline, args.absorb_specks)
            results.append(result)
            per_layer[layer] = result
            (out/f'{scene_id}.{layer}.json').write_text(json.dumps(serialise(result, review_meta), indent=1))
        print(f"{scene_id}: " + ' | '.join(f"{r['layer']} " + ', '.join(f"{k} {v['pieces_before']}->{v['features_after']} (ref {v['refusals_total']})" for k, v in r['classes'].items()) for r in per_layer.values()), flush=True)
        if args.no_render:
            continue
        to_render = []
        if args.render_layer in ('review', 'both') and 'review' in per_layer and per_layer['review']['_pieces']:
            to_render.append(per_layer['review'])
        elif 'model' in per_layer:
            to_render.append(per_layer['model'])
        if args.render_layer == 'both' and 'model' in per_layer and per_layer['model'] not in to_render:
            to_render.append(per_layer['model'])
        for result in to_render:
            renders.append(str(render_scene(result, image_path, out/'renders'/f"{scene_id}.{result['layer']}.png")))
            for kind in LINEAR:
                tile = class_tile(result, image_path, kind)
                if tile is not None:
                    tiles[kind].append(tile)
    if not args.no_render:
        for kind in LINEAR:
            path = contact_sheet(tiles[kind], kind, out/'renders'/f'contact-{kind}.png')
            if path:
                renders.append(str(path))
    metrics = {'version': VERSION, 'computed_at': datetime.now(UTC).isoformat(timespec='seconds'), 'direction': DIRECTION,
               'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'd_class_m': d_class, 'guards': guards,
               'scenes': scenes, 'layers': args.layers, 'mode': args.mode, 'absorb_specks_m2': args.absorb_specks, 'aggregate': aggregate(results),
               'per_scene': {f"{r['scene']}.{r['layer']}": {'pixel_m': round(r['pixel_m'], 5), 'image_px': r['image_px'], 'classes': r['classes'],
                                                            'merges_total': r['merges_total'], 'refusals_total': r['refusals_total']} for r in results},
               'renders': renders,
               'definitions': {'piece': 'one live (not superseded, not rejected) road/footpath/driveway object of the layer',
                               'feature': 'one connected polygon after the guarded closing; merged_from lists every original id; single pieces are features of one id',
                               'gap_closed_m': 'shapely distance between the two cluster geometries at the moment they were joined (0 = touching/overlapping)',
                               'bridge_width_m': 'extent of the added bridge perpendicular to the gap direction (shortest line a-b); seam_a/seam_b = boundary length of each side the bridge attaches to; width_limit_m = the guard threshold applied',
                               'area_ratio': 'feature area / union area of its original pieces, cumulative over the cluster',
                               'confirmed_pieces_merged': 'reviewer-confirmed pieces that ended inside a feature of 2+ pieces (their geometry would change if promoted)',
                               'centreline_m': 'longest path through the inside Voronoi skeleton; mean_width_m = area / centreline length'},
               'note': 'Sidecar prototype; nothing promoted; every figure is a count or geometry, never accuracy.'}
    (out/'metrics.json').write_text(json.dumps(metrics, indent=1))
    print(json.dumps(metrics['aggregate'], indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
