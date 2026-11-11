"""Pitched-roof geometry priors and independent mesh plausibility checks.

The prior is an equal-pitch, equal-eave straight-skeleton roof. It describes a
possible roof, not an observed one. Image/depth geometry wins when it satisfies
the geometric checks. See docs/roof-structure.md for assumptions and sources.
"""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import split, triangulate, unary_union


def _below_plane(region, abc):
    """Clip a polygon to ax + by + c <= 0, including concave regions."""
    a, b, c = abc
    norm = math.hypot(a, b)
    if norm < 1e-9:
        return region if c <= 1e-8 else Polygon()
    center = np.array(region.centroid.coords[0])
    on_line = center - np.array([a, b]) * (a * center[0] + b * center[1] + c) / norm**2
    tangent = np.array([-b, a]) / norm
    radius = 4 * math.hypot(region.bounds[2] - region.bounds[0], region.bounds[3] - region.bounds[1]) + 1
    pieces = split(region, LineString([on_line - tangent * radius, on_line + tangent * radius]))
    return unary_union([p for p in pieces.geoms if a * p.representative_point().x + b * p.representative_point().y + c <= 1e-8])


def gable_end_candidates(footprint: Polygon) -> list[list[list[float]]]:
    """Find free wing ends from two convex corners and opposing side eaves.

    Prefer short end edges over side walls, and never select adjacent edges as
    gables. This also supports shallow projecting wings without address rules.
    """
    ring = np.asarray(orient(footprint, sign=1).exterior.coords[:-1])
    edges = np.roll(ring, -1, axis=0) - ring
    lengths = np.linalg.norm(edges, axis=1)
    candidates = []
    def turn(a, b):
        return math.degrees(math.atan2(a[0]*b[1]-a[1]*b[0], float(np.dot(a, b))))
    for i, edge in enumerate(edges):
        before, after = edges[i - 1], edges[(i + 1) % len(edges)]
        turns = [turn(before, edge), turn(edge, after)]
        depth = min(lengths[i - 1], lengths[(i + 1) % len(edges)])
        # A shallow front projection is bounded by two concave shoulders.
        # Its wide front can be a gable even when depth is less than half its
        # width. Ordinary long side walls lack those shoulders and stay out.
        projecting = (turn(edges[i-2], before) < -45
                      and turn(after, edges[(i+2) % len(edges)]) < -45)
        if (depth < (.8 if projecting else 1.5)
                or not all(65 <= angle <= 115 for angle in turns)
                or lengths[i] > (5 if projecting else 2.25) * depth):
            continue
        candidates.append((lengths[i] / (lengths[i - 1] + lengths[(i + 1) % len(edges)]), i))
    chosen = []
    for _, i in sorted(candidates):
        if any((i - j) % len(edges) in (1, len(edges) - 1) for j in chosen):
            continue
        chosen.append(i)
    return [[ring[i].tolist(), ring[(i + 1) % len(ring)].tolist()] for i in chosen]


def _gable_faces(faces, edge):
    """Replace an end cap by extensions of its adjoining pitched planes.

    The lower envelope keeps the ridge/valley junctions continuous. Only the
    cap's area is repartitioned; no footprint coordinate or other face moves.
    """
    from shapely import union_all

    polygons = [Polygon(xy) for xy, _ in faces]
    eave = LineString(edge)
    caps = [i for i, p in enumerate(polygons) if p.boundary.buffer(1e-7).intersection(eave).length > eave.length * .99]
    if len(caps) != 1:
        raise ValueError("gable end does not have one supported cap")
    cap_index = caps[0]
    cap = polygons[cap_index]
    # Earlier cap conversions snap union coordinates to 1e-8 m. Use the same
    # seam tolerance as shared_edges so the second end still finds its slopes.
    neighbours = [i for i, p in enumerate(polygons) if i != cap_index
                  and cap.boundary.intersection(p.boundary.buffer(1e-7)).length > .01]
    if len(neighbours) < 2:
        raise ValueError("gable end needs adjoining roof slopes")
    planes = {i: np.array(list(_plane(*faces[i])[0].values())) for i in neighbours}
    regions = {}
    for i in neighbours:
        region = cap
        for j in neighbours:
            if i != j and not region.is_empty:
                region = _below_plane(region, planes[i] - planes[j])
        if not region.is_empty:
            regions[i] = region
    if unary_union(list(regions.values())).symmetric_difference(cap).area > 1e-5:
        raise ValueError("gable conversion left a gap")
    result = []
    for i, face in enumerate(faces):
        if i == cap_index:
            continue
        if i not in regions:
            result.append(face)
            continue
        merged = union_all([polygons[i], regions[i]], grid_size=1e-8)
        pieces = [merged] if merged.geom_type == "Polygon" else list(merged.geoms)
        for piece in pieces:
            if piece.interiors:
                raise ValueError("unsupported hole in gable roof")
            # Remove obsolete collinear hip junctions after extending slopes;
            # one straight gable ridge should remain one editable segment.
            xy = list(piece.simplify(1e-8, preserve_topology=True).exterior.coords[:-1])
            a, b, c = planes[i]
            result.append((xy, [float(a * x + b * y + c) for x, y in xy]))
    return result


def _plane(coords, heights):
    xy = np.asarray(coords, dtype=float)
    z = np.asarray(heights, dtype=float)
    matrix = np.column_stack([xy, np.ones(len(xy))])
    abc, _, rank, _ = np.linalg.lstsq(matrix, z, rcond=None)
    residual = float(np.max(np.abs(matrix @ abc - z)))
    return dict(zip(("a", "b", "c"), map(float, abc), strict=True)), residual, rank


def regularize_eave_corners(footprint: Polygon, *, max_deviation_m: float = 1.5) -> dict:
    """Propose straight eaves where a short convex bevel interrupts two walls.

    Extend the two supported, near-orthogonal edges to their intersection. Do
    not use Douglas-Peucker here: joining bevel endpoints retains a diagonal
    eave and its spurious hip plane. Concave notches and full polygonal ends are
    preserved. This is a roof-form proposal, not automatic CRM authoring.
    """
    original = orient(footprint, sign=1.0)
    if original.is_empty or not original.is_valid or original.interiors or not math.isfinite(max_deviation_m) or max_deviation_m <= 0:
        return {"polygon": footprint, "removed_bevels": 0, "max_deviation_m": 0.0}
    ring = [np.asarray(p, dtype=float) for p in original.exterior.coords[:-1]]
    edges = [ring[(i + 1) % len(ring)] - p for i, p in enumerate(ring)]
    # Strong long eaves establish the local orthogonal construction grid.
    support = [(np.linalg.norm(e), math.atan2(e[1], e[0])) for e in edges if np.linalg.norm(e) >= 2.4]
    vector = sum(length * np.exp(4j * angle) for length, angle in support)
    if not support or abs(vector) / sum(length for length, _ in support) < .7:
        return {"polygon": footprint, "removed_bevels": 0, "max_deviation_m": 0.0}
    axis = float(np.angle(vector)) / 4

    def grid_error(edge):
        angle = math.atan2(edge[1], edge[0])
        return abs((angle - axis + math.pi / 4) % (math.pi / 2) - math.pi / 4)

    def cross(a, b):
        return float(a[0] * b[1] - a[1] * b[0])

    removed = 0
    while len(ring) > 4:
        proposals = []
        for i in range(len(ring)):
            # Bevel is b->c; a->b and c->d carry the real eave directions.
            a, b, c, d = [ring[k % len(ring)] for k in (i - 1, i, i + 1, i + 2)]
            before, bevel, after = b - a, c - b, d - c
            lengths = [float(np.linalg.norm(e)) for e in (before, bevel, after)]
            if min(lengths[0], lengths[2]) < 2.4 or not .05 < lengths[1] <= min(2.75, .82 * min(lengths[0], lengths[2])):
                continue
            if max(grid_error(before), grid_error(after)) > math.radians(10):
                continue
            if min(cross(before, bevel), cross(bevel, after)) <= 0:
                continue  # Never fill a concave notch or reverse an edge.
            turn = math.degrees(math.atan2(cross(before, after), float(np.dot(before, after))))
            if not 75 <= turn <= 105:
                continue
            t = cross(c - b, after) / cross(before, after)
            corner = b + before * t
            if t < 0 or np.dot(corner - c, after) > 0:
                continue
            # Rotate to make replacement independent of ring starting vertex.
            rotated = ring[i:] + ring[:i]
            proposed = [corner, *rotated[2:]]
            polygon = Polygon(proposed)
            deviation = original.boundary.hausdorff_distance(polygon.boundary)
            if (not polygon.is_valid or not polygon.buffer(1e-7).covers(original)
                    or deviation > max_deviation_m or polygon.area > original.area * 1.04):
                continue
            proposals.append((deviation, polygon.area, proposed))
        if not proposals:
            break
        ring = min(proposals, key=lambda p: (p[0], p[1]))[2]
        removed += 1
    polygon = Polygon(ring)
    return {"polygon": polygon if removed else footprint, "removed_bevels": removed,
            "max_deviation_m": round(original.boundary.hausdorff_distance(polygon.boundary), 3),
            "area_change_m2": round(polygon.area - original.area, 3)}


def audit_roof(parts: list[dict], footprint: Polygon, eave: float, *, pitched: bool = True) -> dict:
    """Check the emitted mesh, independently of the depth-fit metadata."""
    issues: set[str] = set()
    polygons, planes, vertex_heights = [], [], {}
    adjacency: dict[tuple, set] = {}
    residual_max = seam_max = 0.0
    peak = eave
    for part in parts:
        coords, z = part.get("coords", part.get("ring_meters", [])), part.get("ring_z", [])
        if len(coords) < 3 or len(coords) != len(z) or not np.isfinite(np.asarray(coords)).all() or not np.isfinite(z).all():
            issues.add("invalid-face")
            continue
        poly = Polygon(coords)
        if not poly.is_valid or poly.area < 1e-8:
            issues.add("invalid-face")
            continue
        plane, residual, rank = _plane(coords, z)
        residual_max = max(residual_max, residual)
        if rank < 3 or residual > .05:
            issues.add("non-planar-face")
        if min(z) < eave - .05:
            issues.add("below-eave")
        peak = max(peak, max(z))
        polygons.append(poly)
        planes.append(plane)
        keys = [tuple(round(float(v), 4) for v in p) for p in coords]
        for i, key in enumerate(keys):
            vertex_heights.setdefault(key, []).append(float(z[i]))
            adjacency.setdefault(key, set()).update((keys[i - 1], keys[(i + 1) % len(keys)]))
    if not polygons:
        return {"passed": False, "issues": ["no-faces"], "coverage": 0.0}
    union = unary_union(polygons)
    coverage = footprint.intersection(union).area / max(footprint.area, 1e-9)
    overlap = max(0.0, sum(p.area for p in polygons) - union.area)
    if coverage < .995 or union.difference(footprint).area > max(.05, footprint.area * .005):
        issues.add("footprint-coverage")
    if overlap > max(.05, footprint.area * .005):
        issues.add("overlapping-faces")
    # Shared edges must have one elevation all along the seam, not merely at
    # the original endpoints (partitions often contain T junctions).
    for i, first in enumerate(polygons):
        for j in range(i + 1, len(polygons)):
            shared = first.boundary.intersection(polygons[j].boundary)
            if shared.length < .01:
                continue
            lines = [shared] if shared.geom_type == "LineString" else list(getattr(shared, "geoms", []))
            for line in lines:
                if line.geom_type != "LineString":
                    continue
                for x, y in line.coords:
                    a, b = planes[i], planes[j]
                    seam_max = max(seam_max, abs((a["a"] - b["a"]) * x + (a["b"] - b["b"]) * y + a["c"] - b["c"]))
    if seam_max > .05 or any(max(z) - min(z) > .05 for z in vertex_heights.values()):
        issues.add("disconnected-heights")
    if pitched and peak < eave + .25:
        issues.add("missing-apex")
    # A local low point inside a pitched roof needs evidence of an internal
    # drain. Without that evidence it is a suspicious basin, not a ridge.
    heights = {key: float(np.mean(z)) for key, z in vertex_heights.items()}
    for key, neighbours in adjacency.items():
        if footprint.boundary.distance(Point(key)) < .03:
            continue
        delta = [heights[n] - heights[key] for n in neighbours]
        if delta and min(delta) >= -.01 and max(delta) > .05:
            issues.add("interior-low-point")
    from .roof_physics import drainage_audit
    drainage = {'status': 'not-assessed-invalid-geometry', 'passed': False}
    if not issues:
        if len(parts) <= 64:
            drainage = drainage_audit(parts, footprint, pitched=pitched)
            if not drainage['passed']:
                issues.add('trapped-roof-runoff' if drainage['trapped_samples'] else 'unresolved-roof-runoff')
        else:
            drainage = {'status': 'not-assessed-complexity-limit', 'passed': None}
    return {"passed": not issues, "issues": sorted(issues), "coverage": round(coverage, 5),
            "drainage": drainage,
            "max_planarity_error_m": round(residual_max, 4), "max_seam_error_m": round(seam_max, 4),
            "overlap_m2": round(overlap, 4)}


def structural_prior(footprint: Polygon, eave: float, ridge: float, roof_type: str = "hipped", *, regularize_eaves: bool = True) -> list[dict]:
    """Construct planar faces with shared nodes and outward drainage.

    Complex roofs use a hipped prior. Rectangular gables are supported exactly;
    dormers, varying eaves, mansards and internal drains need separate evidence.
    Pitch bounds are modelling defaults, not building-code or material limits.
    """
    from py_straight_skeleton import compute_skeleton

    if not footprint.is_valid or footprint.is_empty:
        raise ValueError("structural prior requires a valid footprint")
    if not math.isfinite(eave) or not math.isfinite(ridge) or eave < 0:
        raise ValueError("invalid roof heights")
    proposal = regularize_eave_corners(footprint) if regularize_eaves and roof_type != "flat" and not footprint.interiors else {"polygon": footprint, "removed_bevels": 0}
    polygon = orient(proposal["polygon"], sign=1.0)
    ring = list(polygon.exterior.coords[:-1])
    if len(ring) > 150:
        raise ValueError("footprint is too detailed for the structural prior")
    faces = []
    # A rectangle's gable ridge joins the midpoints of its short ends.
    # A quadrilateral is rectangular when all adjacent edges are orthogonal;
    # this avoids fitting/squaring an almost-rectangular captured boundary.
    edges = [np.subtract(ring[(i + 1) % len(ring)], ring[i]) for i in range(len(ring))]
    rectangular = not polygon.interiors and len(ring) == 4 and all(abs(float(np.dot(edges[i], edges[i - 1]))) < 1e-4 * np.linalg.norm(edges[i]) * np.linalg.norm(edges[i - 1]) for i in range(4))
    if roof_type == "gabled" and rectangular:
        if math.dist(ring[0], ring[1]) < math.dist(ring[1], ring[2]):
            ring = ring[1:] + ring[:1]
        a, b, c, d = np.asarray(ring)
        start, end = (a + d) / 2, (b + c) / 2
        run = math.dist(a, d) / 2
        rise = np.clip(ridge - eave, run * math.tan(math.radians(8)), run * math.tan(math.radians(60)))
        faces = [([a, b, end, start], [eave, eave, eave + rise, eave + rise]),
                 ([c, d, start, end], [eave, eave, eave + rise, eave + rise])]
    elif roof_type == "flat":
        if polygon.interiors:
            for triangle in triangulate(polygon):
                clipped = triangle.intersection(polygon)
                pieces = [clipped] if clipped.geom_type == 'Polygon' else list(getattr(clipped, 'geoms', []))
                for piece in pieces:
                    if piece.geom_type != 'Polygon' or piece.area < 1e-8:
                        continue
                    if piece.interiors:
                        raise ValueError('Could not triangulate the interior gaps.')
                    xy = list(piece.exterior.coords[:-1])
                    faces.append((xy, [eave] * len(xy)))
        else:
            faces = [(ring, [eave] * len(ring))]
    else:
        try:
            skeleton = compute_skeleton(exterior=ring, holes=[list(h.coords[:-1]) for h in polygon.interiors])
            skeleton_faces = skeleton.get_faces()
        except RuntimeError as error:
            # The skeleton library can reject numerically degenerate but valid
            # polygons. Treat this as a failed candidate, not a scene failure.
            raise ValueError(f'Roof skeleton could not be constructed: {error}') from error
        peak_time = max(node.time for node in skeleton.nodes)
        if peak_time <= 1e-6:
            raise ValueError("skeleton has no interior apex")
        pitch = float(np.clip((ridge - eave) / peak_time, math.tan(math.radians(8)), math.tan(math.radians(60))))
        for face in skeleton_faces:
            indices = face[:-1] if face[0] == face[-1] else face
            nodes = [skeleton.nodes[i] for i in indices]
            faces.append(([(n.position.x, n.position.y) for n in nodes], [eave + n.time * pitch for n in nodes]))
    # A noisy captured outline remains authoritative. Construct fewer planes
    # from supported eaves, then clip those planes back to the original ring.
    # This removes invented hips without expanding the captured roof boundary.
    if proposal["removed_bevels"]:
        clipped_faces = []
        for coords, heights in faces:
            plane, _, _ = _plane(coords, heights)
            clipped = Polygon(coords).intersection(footprint)
            polygons = [clipped] if clipped.geom_type == "Polygon" else list(getattr(clipped, "geoms", []))
            for piece in polygons:
                if piece.geom_type != "Polygon" or piece.area < 1e-8:
                    continue
                xy = list(piece.exterior.coords[:-1])
                clipped_faces.append((xy, [plane["a"] * x + plane["b"] * y + plane["c"] for x, y in xy]))
        faces = clipped_faces
    gable_ends = []
    if roof_type == "gabled" and not rectangular:
        for edge in gable_end_candidates(polygon):
            converted = _gable_faces(faces, edge)
            candidate_parts = _parts_from_faces(converted, eave, roof_type)
            if audit_roof(candidate_parts, footprint, eave)["passed"]:
                faces = converted
                gable_ends.append(edge)
    parts = _parts_from_faces(faces, eave, roof_type)
    if parts:
        parts[0]["eave_regularization"] = {k: v for k, v in proposal.items() if k != "polygon"}
        parts[0]["gable_ends"] = gable_ends
    audit = audit_roof(parts, footprint, eave, pitched=roof_type != "flat")
    if not audit["passed"]:
        raise ValueError(f"structural prior failed validation: {audit['issues']}")
    return parts


def _parts_from_faces(faces, eave, roof_type="hipped"):
    parts = []
    for i, (coords, heights) in enumerate(faces):
        coords = [list(map(float, point)) for point in coords]
        heights = list(map(float, heights))
        plane, residual, _ = _plane(coords, heights)
        if residual > .01:
            raise ValueError("skeleton generated a non-planar face")
        slope = math.degrees(math.atan(math.hypot(plane["a"], plane["b"])))
        area = Polygon(coords).area
        parts.append({"index": i, "coords": coords, "ring_meters": coords, "ring": coords,
                      "ring_z": heights, "z": heights, "plane": plane,
                      "area_m2": round(area, 2), "area_3d_m2": round(area / math.cos(math.radians(slope)), 2),
                      "slope_deg": round(slope, 1), "pitch_deg": round(slope, 1),
                      "pitch_ratio": f"{math.tan(math.radians(slope)) * 12:.1f}:12",
                      "azimuth_deg": round(math.degrees(math.atan2(-plane["a"], -plane["b"])) % 360, 1),
                      "eave_m": eave, "ridge_m": max(heights), "z_range": [min(heights), max(heights)],
                      "kind": "flat_surface" if roof_type == "flat" else "main_pitch"})
    return parts


def convert_end_to_gable(parts, footprint, eave, edge):
    faces = _gable_faces([(p["coords"], p["ring_z"]) for p in parts], edge)
    converted = _parts_from_faces(faces, eave, "gabled")
    audit = audit_roof(converted, footprint, eave)
    if not audit["passed"]:
        raise ValueError(f"gable conversion failed geometry checks: {audit['issues']}")
    if converted:
        converted[0]["eave_regularization"] = parts[0].get("eave_regularization", {"removed_bevels": 0})
    return converted


def shared_edges(parts: list[dict]):
    """Yield folds of the final faces, excluding coplanar mesh storage seams."""
    polygons = [Polygon(part["coords"]) for part in parts]
    for i, first in enumerate(polygons):
        for j, second in enumerate(polygons[i + 1:], i + 1):
            delta = [parts[i]['plane'][k] - parts[j]['plane'][k] for k in ('a', 'b', 'c')]
            # Fitted plane coefficients contain numerical residuals. Compare
            # tilt and physical height over both faces, rather than the raw
            # intercept (which changes with coordinate origin). Millimetre
            # storage seams must not be emitted as roof valleys or hips.
            if (math.hypot(*delta[:2]) < 1e-4
                    and max(abs(delta[0]*x+delta[1]*y+delta[2])
                            for x, y in [*parts[i]['coords'], *parts[j]['coords']]) < .002):
                continue
            shared = first.boundary.intersection(second.boundary)
            if first.distance(second) < 1e-6:
                # Plane clipping can put a shared edge a few nanometres off
                # its neighbour. Recover the whole seam even if one fragment
                # intersects exactly, otherwise a continuous apex gets a gap.
                tolerant = first.boundary.intersection(second.boundary.buffer(1e-7))
                if tolerant.length > shared.length+.01:
                    shared = tolerant
            lines = [shared] if isinstance(shared, LineString) else list(getattr(shared, "geoms", []))
            for line in lines:
                if isinstance(line, LineString) and line.length > .02:
                    for a, b in zip(list(line.coords)[:-1], list(line.coords)[1:], strict=True):
                        if math.dist(a, b) < .02:
                            continue
                        vertices = [*first.exterior.coords[:-1], *second.exterior.coords[:-1]]
                        a = min(vertices, key=lambda p: math.dist(p, a)) if min(math.dist(p, a) for p in vertices) < 1e-5 else a
                        b = min(vertices, key=lambda p: math.dist(p, b)) if min(math.dist(p, b) for p in vertices) < 1e-5 else b
                        yield [list(a), list(b)]
