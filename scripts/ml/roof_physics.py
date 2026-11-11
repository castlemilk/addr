"""Deterministic gravity/runoff checks for an estimated piecewise-planar roof.

This is a geometric rain-flow model, not a structural load or compliance check.
No rainfall, gutter capacity, material or surveyed elevations are assumed.
"""
from __future__ import annotations

import numpy as np
from shapely import constrained_delaunay_triangles
from shapely.geometry import LineString, Point, Polygon

VERSION = 'roof-runoff-v1'


def drainage_audit(parts, footprint, *, pitched=True):
    """Trace gravity down each face, then along converging valley seams.

    Face/triangulation centroids sample every patch, including concave faces.
    At a seam, steepest admissible descent selects a neighbouring face or a
    descending seam. Level interior channels have no gravity outlet and fail.
    Boundary exits include courtyard edges; gutters/downpipes are not inferred.
    """
    if not pitched:
        return {'method': VERSION, 'status': 'not-assessed-flat-roof', 'passed': True,
                'sample_count': 0, 'boundary_exits': 0, 'trapped_samples': 0}
    surfaces, starts = [], []
    for part in parts:
        xy = np.asarray(part.get('coords', part.get('ring_meters')), dtype=float)
        z = np.asarray(part['ring_z'], dtype=float)
        polygon = Polygon(xy)
        if not polygon.is_valid or polygon.area < 1e-8:
            return {'method': VERSION, 'status': 'invalid-surface', 'passed': False,
                    'sample_count': 0, 'boundary_exits': 0, 'trapped_samples': 0}
        plane = np.linalg.lstsq(np.c_[xy, np.ones(len(xy))], z, rcond=None)[0]
        surfaces.append((polygon, xy, plane))
        starts.extend(np.asarray(t.centroid.coords[0]) for t in constrained_delaunay_triangles(polygon).geoms if t.area > 1e-8)
    # Metre-space tolerances only absorb clipping/solver floating-point noise.
    tolerance, step = 2e-5, 2e-4
    span = max(footprint.bounds[2]-footprint.bounds[0], footprint.bounds[3]-footprint.bounds[1])*3+1
    trapped, unresolved, exits = [], 0, 0
    for start in starts:
        position, visited, outcome = start.copy(), set(), 'unresolved'
        for _ in range(max(32, len(surfaces)*6)):
            point = Point(position)
            if footprint.boundary.distance(point) <= tolerance:
                outcome = 'exit'
                break
            key = tuple(np.round(position, 5))
            if key in visited:
                break
            visited.add(key)
            paths = []
            for polygon, xy, plane in surfaces:
                if not polygon.buffer(tolerance).covers(point):
                    continue
                gradient = -plane[:2]
                slope = np.linalg.norm(gradient)
                if slope > 1e-5:
                    direction = gradient/slope
                    if polygon.contains(Point(position+direction*step)):
                        ray = LineString([position+direction*step, position+direction*span])
                        hit = ray.intersection(polygon.boundary)
                        points = [hit] if hit.geom_type == 'Point' else list(getattr(hit, 'geoms', []))
                        distances = [(float((np.array(p.coords[0])-position) @ direction), np.array(p.coords[0]))
                                     for p in points if p.geom_type == 'Point']
                        forward = [(distance, end) for distance, end in distances if distance > step*.5]
                        if forward:
                            paths.append((slope, min(forward, key=lambda item: item[0])[1]))
                # Valleys constrain runoff to the seam when neither face's
                # unconstrained gradient enters the opposite face.
                for a, b in zip(xy, np.roll(xy, -1, axis=0), strict=True):
                    if LineString([a, b]).distance(point) > tolerance:
                        continue
                    for end in (a, b):
                        delta = end-position
                        distance = np.linalg.norm(delta)
                        if distance <= tolerance:
                            continue
                        descent = -float(plane[:2] @ delta)/distance
                        if descent > 1e-5:
                            paths.append((descent, end))
            if not paths:
                outcome = 'trapped'
                break
            position = max(paths, key=lambda path: path[0])[1]
        if outcome == 'exit':
            exits += 1
        elif outcome == 'trapped':
            trapped.append(position.tolist())
        else:
            unresolved += 1
    return {'method': VERSION, 'status': 'passed' if exits == len(starts) and starts else 'needs-review',
            'passed': bool(starts) and exits == len(starts), 'sample_count': len(starts),
            'boundary_exits': exits, 'trapped_samples': len(trapped), 'unresolved_samples': unresolved,
            'trapped_locations_m': [list(point) for point in dict.fromkeys(tuple(np.round(p, 3)) for p in trapped)][:12]}
