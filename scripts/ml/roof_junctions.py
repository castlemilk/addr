"""Bounded, level-ridge alternatives to short clusters of roof junctions.

Footprints and eaves stay fixed. This construction prior permits different wing
pitches instead of forcing equal pitches to create several nearby apex nodes.
It never reads address identity or reviewer line coordinates.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares
from shapely import constrained_delaunay_triangles
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .roof_line_fit import _graph
from .roof_structure import _parts_from_faces, audit_roof, shared_edges
from .rooflines import _classify_roofline

VERSION = 'joined-roof-ridges-v1'


def construction_complexity(parts):
    """Count physical planes, crease runs and junctions, not storage fragments."""
    planes = []
    origin = np.mean([xy for p in parts for xy in p['coords']], axis=0)
    for part in parts:
        plane = np.array([part['plane'][key] for key in ('a', 'b', 'c')])
        height = float(plane @ np.r_[origin, 1.])
        match = next((p for p in planes if np.linalg.norm(plane[:2]-p[0][:2]) < 1e-4 and abs(height-p[1]) < .002), None)
        if match is None:
            planes.append([plane, height, [Polygon(part['coords'])]])
        else:
            match[2].append(Polygon(part['coords']))
    small = sum(unary_union(p[2]).area < .5 for p in planes)
    lines = [np.asarray(line) for line in shared_edges(parts)]
    parents = list(range(len(lines)))
    def root(i):
        while parents[i] != i:
            i = parents[i]
        return i
    for i, line in enumerate(lines):
        direction = line[1]-line[0]
        direction /= np.linalg.norm(direction)
        normal = np.array([-direction[1], direction[0]])
        for j, other in enumerate(lines[:i]):
            vector = other[1]-other[0]
            if (abs(direction @ vector/np.linalg.norm(vector)) > .9998
                    and np.max(np.abs((other-line[0]) @ normal)) < .02
                    and LineString(line).distance(LineString(other)) < .02):
                parents[root(i)] = root(j)
    nodes, incidents = [], []
    for line in lines:
        pair = []
        for point in line:
            index = next((i for i, node in enumerate(nodes) if np.linalg.norm(node-point) < 1e-4), None)
            if index is None:
                index = len(nodes)
                nodes.append(point)
                incidents.append(set())
            pair.append(index)
        a, b = pair
        incidents[a].add(b)
        incidents[b].add(a)
    junctions = {i for i, neighbours in enumerate(incidents) if len(neighbours) >= 3}
    # Traverse degree-two subdivisions so adding an editor/mesh vertex cannot
    # hide a short junction link from the construction cost.
    short = 0
    for start in junctions:
        for neighbour in incidents[start]:
            previous, current, length = start, neighbour, float(np.linalg.norm(nodes[start]-nodes[neighbour]))
            seen = {start}
            while current not in seen and len(incidents[current]) == 2:
                seen.add(current)
                following = next(i for i in incidents[current] if i != previous)
                length += float(np.linalg.norm(nodes[current]-nodes[following]))
                previous, current = current, following
            if current in junctions and current > start and length < 2.5:
                short += 1
    runs = len({root(i) for i in range(len(lines))})
    length = unary_union([LineString(line) for line in lines]).length
    cost = .08*len(planes)+.04*runs+.08*len(junctions)+.12*short+.12*small+.01*length
    return {'construction_complexity': round(cost, 5), 'physical_planes': len(planes),
            'crease_runs': runs, 'junctions': len(junctions), 'short_junction_links': short,
            'crease_length_m': round(length, 4), 'small_planes': small}


def _contract(parts, footprint, eave, clusters):
    vertices, faces, _ = _graph(parts)
    replacement = {i: cluster[0] for cluster in clusters for i in cluster}
    faces = [[replacement.get(i, i) for i in face] for face in faces]
    faces = [[i for j, i in enumerate(face) if i != face[j-1]] for face in faces]
    if any(len(set(face)) < 3 for face in faces):
        raise ValueError('Contraction would erase a roof plane')
    used = sorted({i for face in faces for i in face})
    indices = {key: i for i, key in enumerate(used)}
    seed = vertices[used].copy()
    faces = [[indices[i] for i in face] for face in faces]
    for cluster in clusters:
        seed[indices[cluster[0]]] = vertices[cluster].mean(axis=0)
    corners = np.asarray(footprint.exterior.coords[:-1])
    bases, low, high, movable = [], [], [], []
    peak = float(vertices[:, 2].max())
    # Check original nodes before averaging a cluster: a low roof level must
    # not be hidden by its higher neighbours in the contraction seed.
    if any(peak-vertex[2] > min(1.5, (peak-eave)*.6)
           for vertex in vertices if np.min(np.linalg.norm(corners-vertex[:2], axis=1)) >= .02):
        raise ValueError('Distinct roof levels must not be flattened')
    for i, vertex in enumerate(seed):
        if np.min(np.linalg.norm(corners-vertex[:2], axis=1)) < .02:
            continue
        movable.append(i)
        point = Point(vertex[:2])
        if footprint.boundary.distance(point) < .02:
            choices = [(LineString([a, b]).distance(point), a, b) for a, b in zip(corners, np.roll(corners, -1, axis=0), strict=True)]
            _, a, b = min(choices, key=lambda row: row[0])
            length = np.linalg.norm(b-a)
            direction = (b-a)/length
            position = (vertex[:2]-a) @ direction
            basis = np.zeros((len(seed), 2))
            basis[i] = direction
            bases.append(basis)
            low.append(max(-2.5, .01-position))
            high.append(min(2.5, length-.01-position))
        else:
            for axis in (0, 1):
                basis = np.zeros((len(seed), 2))
                basis[i, axis] = 1
                bases.append(basis)
                low.append(-2.5)
                high.append(2.5)
    seed[movable, 2] = peak
    if not bases:
        raise ValueError('No supported apex junctions')
    triangles = []
    for face in faces:
        polygon = Polygon(seed[face, :2])
        if not polygon.is_valid or polygon.area < .01:
            raise ValueError('Contraction would invert a roof face')
        for triangle in constrained_delaunay_triangles(polygon).geoms:
            triangles.append([min(face, key=lambda i: np.linalg.norm(seed[i, :2]-xy)) for xy in list(triangle.exterior.coords)[:3]])
    triangles = np.asarray(triangles)
    basis = np.stack(bases, axis=2)
    count = len(bases)
    planes = np.array([np.linalg.lstsq(np.c_[seed[face, :2], np.ones(len(face))], seed[face, 2], rcond=None)[0] for face in faces])
    vidx = np.concatenate(faces)
    fidx = np.concatenate([[i]*len(face) for i, face in enumerate(faces)])

    def areas(v):
        a, b, c = np.moveaxis(v[triangles, :2], 1, 0)
        u, w = b-a, c-a
        return u[:, 0]*w[:, 1]-u[:, 1]*w[:, 0]

    original_areas = areas(seed)
    def decode(parameters):
        v = seed.copy()
        v[:, :2] += np.tensordot(basis, parameters[:count], axes=([2], [0]))
        return v, parameters[count:].reshape(-1, 3)

    def residual(parameters):
        v, ps = decode(parameters)
        planarity = (np.sum(v[vidx, :2]*ps[fidx, :2], axis=1)+ps[fidx, 2]-v[vidx, 2])*100
        orientation = np.minimum(0, areas(v)*np.sign(original_areas)-np.abs(original_areas)*.02)*100
        return np.r_[planarity, orientation, parameters[:count]*.02]

    fitted = least_squares(residual, np.r_[np.zeros(count), planes.flatten()], max_nfev=100, ftol=1e-8,
        bounds=(np.r_[low, np.tile([-4., -4., -10000.], len(faces))], np.r_[high, np.tile([4., 4., 10000.], len(faces))]))
    v, _ = decode(fitted.x)
    result = _parts_from_faces([(v[face, :2], v[face, 2]) for face in faces], eave, parts[0].get('roof_type', 'hipped'))
    audit = audit_roof(result, footprint, eave)
    if not audit['passed'] or audit.get('drainage', {}).get('passed') is not True:
        raise ValueError('Joined ridge proposal failed geometry checks')
    covered = unary_union([Polygon(p['coords']) for p in result])
    movement = max(float(np.linalg.norm(v[indices[replacement.get(i, i)], :2]-point[:2])) for i, point in enumerate(vertices))
    # A bounded optimizer may stop before its movement penalty converges. Only
    # the independently measured geometry tolerances authorize a candidate.
    if (not audit['passed'] or audit['drainage']['passed'] is not True or movement > 2.5
            or audit['max_planarity_error_m'] > .001 or audit['max_seam_error_m'] > .001
            or covered.symmetric_difference(footprint).area > 1e-5 or any(p['slope_deg'] > 65 for p in result)):
        raise ValueError('Joined ridge proposal failed geometry bounds')
    return result, {'method': VERSION, 'joined_clusters': len(clusters), 'solver_converged': bool(fitted.success),
                    'removed_junction_nodes': sum(len(c)-1 for c in clusters),
                    'max_shift_m': round(movement, 4), 'geometry_audit': audit}


def junction_candidates(parts, footprint, eave):
    """Offer connected short internal-hip clusters, never remove a free end hip."""
    vertices, _, edges = _graph(parts)
    if footprint.interiors or len(vertices) > 64 or len(parts) > 24:
        return []
    groups = []
    for a, b in edges:
        line = vertices[[a, b], :2]
        if (a == b or not .02 < np.linalg.norm(line[1]-line[0]) < 2.5
                or min(vertices[[a, b], 2]) <= eave+.25
                or any(footprint.boundary.distance(Point(point)) < .1 for point in line)
                or _classify_roofline(line.tolist(), parts) != 'hip'):
            continue
        joined = {a, b}
        rest = []
        for group in groups:
            if group & joined:
                joined |= group
            else:
                rest.append(group)
        groups = [*rest, joined]
    groups = [sorted(group) for group in groups if max(np.linalg.norm(vertices[a, :2]-vertices[b, :2]) for a in group for b in group) <= 3.5]
    if not groups:
        return []
    choices = [groups]
    if 1 < len(groups) <= 3:
        choices += [[group] for group in groups]
    results = []
    for clusters in choices:
        try:
            results.append(_contract(parts, footprint, eave, clusters))
        except (ValueError, ArithmeticError, RuntimeError, GEOSException):
            continue
    return results
