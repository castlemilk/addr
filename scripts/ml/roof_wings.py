"""Overlapping roof-wing hypotheses for L/T footprints.

Long ridges can continue through a junction to an outer wall, even when that
wall is not a free end of the complete silhouette. No labels or address-specific
coordinates enter this candidate generator. Independent geometry and image
checks decide whether a hypothesis is usable.
"""
from __future__ import annotations

import numpy as np
from shapely import set_precision, union_all
from shapely.errors import ShapelyError
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .roof_structure import _below_plane, _parts_from_faces

VERSION = 'connected-gable-wings-v1'


def roof_rectangles(footprint):
    """Bounded maximal-rectangle cover of an already orthogonal support."""
    if footprint.interiors or len(footprint.exterior.coords) > 25:
        return []
    ring = np.asarray(footprint.exterior.coords[:-1])
    vectors = np.roll(ring, -1, axis=0)-ring
    angle = np.arctan2(*vectors[np.argmax(np.linalg.norm(vectors, axis=1))][::-1])
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    local = ring @ rotation
    local = np.round(local, 8)
    polygon = Polygon(local)
    if any(min(abs(v)) > 1e-6 for v in np.roll(local, -1, axis=0)-local):
        return []
    xs, ys = [sorted(set(local[:, axis])) for axis in (0, 1)]
    if len(xs) > 12 or len(ys) > 12:
        return []
    cells = np.array([[polygon.covers(box(a, c, b, d)) for c, d in zip(ys[:-1], ys[1:], strict=True)]
                      for a, b in zip(xs[:-1], xs[1:], strict=True)], dtype=int)
    rectangles = []
    for i, x in enumerate(xs[:-1]):
        for j in range(i+1, len(xs)):
            for k, y in enumerate(ys[:-1]):
                for end_y in range(k+1, len(ys)):
                    if min(xs[j]-x, ys[end_y]-y) < 1.:
                        continue
                    if cells[i:j, k:end_y].all():
                        rectangles.append(box(x, y, xs[j], ys[end_y]))
    rectangles.sort(key=lambda p: -p.area)
    maximal = []
    for p in rectangles:
        if not any(q.covers(p) for q in maximal):
            maximal.append(p)
    covered, chosen = Polygon(), []
    while maximal and len(chosen) < 6:
        p = max(maximal, key=lambda q: q.difference(covered).area)
        if p.difference(covered).area < .5:
            break
        chosen.append(p)
        covered = covered.union(p)
        maximal.remove(p)
    if not 2 <= len(chosen) <= 6 or covered.symmetric_difference(polygon).area > 1e-5:
        return []
    return [(p, rotation) for p in chosen]


def _tent(rectangle, rotation, eave, rise, axis=None, hip_end=None, domain=None, limits=None):
    x0, y0, x1, y1 = rectangle.bounds
    widths = np.array([x1-x0, y1-y0])
    along = int(np.argmax(widths)) if axis is None else axis
    across = 1-along
    # Start with two whole pitched planes. Optional end planes create a hip
    # without making every other wing end hipped as well.
    center = np.array([(x0+x1)/2, (y0+y1)/2])
    planes = []
    for sign in (-1, 1):
        abc = np.zeros(3)
        abc[across] = sign*rise/(widths[across]/2)
        abc[2] = eave+rise-abc[:2] @ center
        planes.append(abc)
    if hip_end is not None:
        abc = np.zeros(3)
        abc[along] = (1 if hip_end == 0 else -1)*rise/(widths[across]/2)
        end = np.array([x0, y0]) if hip_end == 0 else np.array([x1, y1])
        abc[2] = eave-abc[:2] @ end
        planes.append(abc)
    faces = []
    for abc in planes:
        region = rectangle if domain is None else Polygon(np.asarray(domain.exterior.coords) @ rotation)
        for end, limit in enumerate(limits or (None, None)):
            if limit is not None:
                cut = np.zeros(3)
                cut[along] = -1 if end == 0 else 1
                cut[2] = -cut[along]*limit
                region = _below_plane(region, cut)
        for other in planes:
            region = _below_plane(region, abc-other)
        if region.is_empty:
            continue
        regions = [region] if region.geom_type == 'Polygon' else list(getattr(region, 'geoms', []))
        for piece in regions:
            if piece.geom_type != 'Polygon' or piece.area < 1e-6:
                continue
            xy = np.asarray(piece.exterior.coords[:-1])
            faces.append((xy @ rotation.T, np.c_[xy, np.ones(len(xy))] @ abc))
    return _parts_from_faces(faces, eave)


def _upper_envelope(wings, eave):
    """Intersect actual planes across overlaps, sharing crease elevations."""
    pieces = [p for wing in wings for p in wing]
    faces = []
    for i, part in enumerate(pieces):
        region = Polygon(part['coords'])
        abc = np.array([part['plane'][key] for key in ('a', 'b', 'c')])
        for j, other in enumerate(pieces):
            if i == j or region.is_empty:
                continue
            polygon = Polygon(other['coords'])
            overlap = region.intersection(polygon)
            if overlap.geom_type == 'GeometryCollection':
                overlap = unary_union([p for p in overlap.geoms if p.geom_type in ('Polygon', 'MultiPolygon')])
            if overlap.area < 1e-9:
                continue
            delta = np.array([other['plane'][key] for key in ('a', 'b', 'c')])-abc
            if np.linalg.norm(delta) < 1e-7:
                if j < i:
                    region = region.difference(polygon)
                continue
            region = set_precision(region.difference(polygon).union(_below_plane(overlap, delta)), 1e-8)
        regions = [region] if region.geom_type == 'Polygon' else list(getattr(region, 'geoms', []))
        for p in regions:
            if p.geom_type != 'Polygon' or p.area < 1e-6:
                continue
            if p.interiors:
                raise ValueError('Unsupported opening in a roof wing')
            xy = np.asarray(p.exterior.coords[:-1])
            faces.append((xy, np.c_[xy, np.ones(len(xy))] @ abc))
    if not faces:
        raise ValueError('No roof wing surfaces')
    offset = min(float(min(z)) for _, z in faces)-eave
    parts = _parts_from_faces([(xy, z-offset) for xy, z in faces], eave, 'mixed')
    planes = []
    for part in parts:
        abc = np.array([part['plane'][key] for key in ('a', 'b', 'c')])
        group = next((g for g in planes if np.max(np.abs(g[0]-abc)) < 1e-6), None)
        if group is None:
            planes.append((abc, [Polygon(part['coords'])]))
        else:
            group[1].append(Polygon(part['coords']))
    combined = []
    for abc, polygons in planes:
        region = union_all(polygons, grid_size=1e-8)
        pieces = [region] if region.geom_type == 'Polygon' else list(region.geoms)
        for p in pieces:
            p = set_precision(p, 1e-8).simplify(1e-7)
            if p.is_empty or p.area < 1e-6:
                continue
            if p.interiors:
                raise ValueError('Unsupported opening in a roof wing')
            xy = np.asarray(set_precision(p, 1e-8).simplify(1e-7).exterior.coords[:-1])
            combined.append((xy, np.c_[xy, np.ones(len(xy))] @ abc))
    return _parts_from_faces(combined, eave, 'mixed')


def wing_candidates(footprint, eave, rise):
    rectangles = roof_rectangles(footprint)
    if not rectangles:
        return []
    # A small eave step can create a second, mostly redundant rectangle beside
    # a long wing. It must not invent a second parallel ridge. Prefer the larger
    # supported rectangle; all final faces still cover the exact footprint.
    distinct = []
    for p, rotation in sorted(rectangles, key=lambda row: -row[0].area):
        widths = np.ptp(np.asarray(p.exterior.coords), axis=0)
        axis = int(np.argmax(widths))
        if any(int(np.argmax(np.ptp(np.asarray(q.exterior.coords), axis=0))) == axis
               and p.intersection(q).area/p.area > .8 for q, _ in distinct):
            continue
        distinct.append((p, rotation))
    rectangles = distinct
    limits = [[None, None] for _ in rectangles]
    centers = [np.array(p.centroid.coords[0]) for p, _ in rectangles]
    widths = [np.ptp(np.asarray(p.exterior.coords), axis=0) for p, _ in rectangles]
    axes = [int(np.argmax(w)) for w in widths]
    # A branch joins the through-wing at its ridge; continuing both prism
    # pairs beyond that junction would invent a four-way cross and extra valleys.
    # Compare how far each visible wing continues either side of the crossing,
    # normalized by width. Similar through-spans remain ambiguous cross roofs.
    for i, (p, _) in enumerate(rectangles):
        for j, (q, _) in enumerate(rectangles[:i]):
            if axes[i] == axes[j] or p.intersection(q).area < .5:
                continue
            spans = []
            for a, b in ((i, j), (j, i)):
                axis = axes[a]
                bounds = rectangles[a][0].bounds
                spans.append(min(centers[b][axis]-bounds[axis], bounds[axis+2]-centers[b][axis])/widths[a][1-axis])
            if abs(spans[0]-spans[1]) < .15 or min(spans) > 1.25:
                continue
            branch, through = (i, j) if spans[0] < spans[1] else (j, i)
            axis = axes[branch]
            cut = centers[through][axis]
            end = int(cut > centers[branch][axis])
            old = limits[branch][end]
            limits[branch][end] = cut if old is None else (min(old, cut) if end else max(old, cut))
    # Extend each pitched pair through the junction before taking the upper
    # envelope. Cutting a gable prism at an internal rectangle end creates a
    # vertical step; the infinite plane pairs instead meet along shared folds.
    base = [_tent(p, r, eave, rise, domain=footprint, limits=limits[i])
            for i, (p, r) in enumerate(rectangles)]
    options = [(base, {'hip_ends': 0})]
    for i, (p, rotation) in enumerate(rectangles):
        for end in (0, 1):
            if limits[i][end] is not None:
                continue
            options.append(([*(base[:i]), _tent(p, rotation, eave, rise, hip_end=end, domain=footprint, limits=limits[i]), *base[i+1:]],
                            {'hip_ends': 1, 'hip_wing': i, 'hip_end': end}))
        x0, y0, x1, y1 = p.bounds
        if max(x1-x0, y1-y0)/min(x1-x0, y1-y0) < 1.4 and not any(v is not None for v in limits[i]):
            axis = 1-int(np.argmax([x1-x0, y1-y0]))
            options.append(([*(base[:i]), _tent(p, rotation, eave, rise, axis=axis, domain=footprint), *base[i+1:]],
                            {'hip_ends': 0, 'alternative_axis_wing': i}))
    results = []
    for wings, detail in options:
        try:
            parts = _upper_envelope(wings, eave)
            if unary_union([Polygon(p['coords']) for p in parts]).symmetric_difference(footprint).area > 1e-5:
                continue
            results.append((parts, {'method': VERSION, 'wings': len(rectangles),
                                   'branch_ends': sum(v is not None for pair in limits for v in pair), **detail}))
        except (ValueError, ArithmeticError, ShapelyError):
            continue
    return results
