"""Image-registered canopy-aware eave hypotheses, never reviewed boundaries.

Detector canopy masks downweight missing observations. Visible eaves establish
construction axes; genuine oblique sides remain independent. Hidden extensions
are bounded and reported separately from visible-mask fitting error.
"""
from __future__ import annotations

from itertools import product

import numpy as np
from shapely import intersects_xy
from shapely import points as shapely_points
from shapely.geometry import LineString, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

VERSION = 'canopy-eaves-v2'


def model_canopies(objects):
    """Only same-frame detector output may support a pure inference boundary."""
    result = []
    for obj in objects:
        if (obj.get('kind') != 'tree' or obj.get('source') != 'sam3-text-onnx'
                or obj.get('geometry_reviewed') or obj.get('roof_line_edits')
                or obj.get('status') in ('rejected', 'superseded')):
            continue
        polygon = Polygon(obj['ring'], obj.get('holes'))
        if polygon.is_valid and not polygon.is_empty:
            result.append(polygon)
    return result


def _samples(line, spacing=.12):
    return np.array([line.interpolate(t).coords[0] for t in np.linspace(0, line.length, max(3, min(2048, int(line.length/spacing)+1)))])


def regularize_eaves(polygon, image_size, frame, canopies=(), *, hypothesis=False):
    """Return an angular hypothesis only when observed support passes all gates."""
    frame = np.asarray(frame, dtype=float)
    scale = float(np.linalg.svd(frame[:2], compute_uv=False).max())
    # A wider construction hypothesis is exposed only for explicit comparison,
    # with a separate image-edge veto. Published extraction keeps strict gates.
    max_visible = min(1.25 if hypothesis else 1., 8*scale)
    p95_limit = min(.7 if hypothesis else .45, 4*scale)
    area_limit = .07 if hypothesis else .04
    inverse = np.linalg.inv(frame[:2])
    report = {'method': VERSION, 'accepted': False, 'reason': 'insufficient-construction-evidence'}
    def metric(ring):
        return np.asarray(ring) @ frame[:2]+frame[2]
    def pixels(ring):
        return (np.asarray(ring)-frame[2]) @ inverse
    raw = Polygon(metric(polygon.exterior.coords))
    ring = np.asarray(orient(raw).exterior.coords[:-1])
    if len(ring) > 300 or raw.area < 8:
        return polygon, report
    # A crop is not a physical eave. Leave its endpoints and partial roof alone.
    if any(np.any(np.abs(np.asarray(polygon.exterior.coords)[:, axis]-edge) < .01)
           for axis, size in enumerate(image_size) for edge in (0, size)):
        return polygon, {**report, 'reason': 'image-cut'}
    nearby = [p for p in canopies if p.distance(polygon) < 2/scale]
    canopy = unary_union([Polygon(metric(p.exterior.coords), [metric(h.coords) for h in p.interiors]) for p in nearby]).buffer(.35)
    axis_ring = np.asarray(orient(raw.simplify(min(.4, 3*scale))).exterior.coords[:-1])
    edges = np.roll(axis_ring, -1, axis=0)-axis_ring
    lengths = np.linalg.norm(edges, axis=1)
    visible = np.array([1-np.mean(intersects_xy(canopy, *_samples(LineString([a, a+d])).T)) for a, d in zip(axis_ring, edges, strict=True)])
    weights = lengths*np.maximum(.05, visible)
    long = lengths >= 2.4
    if np.count_nonzero(long & (visible > .5)) < 3:
        return polygon, report
    angles = np.arctan2(edges[:, 1], edges[:, 0])
    vector = np.sum(weights[long]*np.exp(4j*angles[long]))
    coherence = abs(vector)/sum(weights[long])
    angle = np.angle(np.sum(weights*np.exp(4j*angles)))/4
    axis_method = 'circular-mean'
    if coherence < .75:
        if not hypothesis:
            return polygon, {**report, 'reason': 'mixed-orientation', 'axis_coherence': round(float(coherence), 4)}
        # Short chamfers and repeated setbacks can cancel the circular mean
        # even when most visible eave length shares one construction grid.
        # Require a dominant consensus, then retain independent oblique sides
        # and the same two-way distance/area gates below.
        eligible = long & (visible > .5)
        choices = []
        for seed in angles[eligible]:
            error = abs((angles-seed+np.pi/4) % (np.pi/2)-np.pi/4)
            members = eligible & (error <= np.deg2rad(15))
            choices.append((float(sum(weights[members])), members))
        support, members = max(choices, key=lambda item: item[0])
        if support/sum(weights[long]) < .78 or np.count_nonzero(members) < 3:
            return polygon, {**report, 'reason': 'mixed-orientation', 'axis_coherence': round(float(coherence), 4)}
        angle = np.angle(np.sum(weights[members]*np.exp(4j*angles[members])))/4
        axis_method = 'dominant-visible-eaves'
    base_angle = angle
    offsets = (0, -2, 2, -4, 4, -6, 6) if axis_method == 'dominant-visible-eaves' else (0,)
    candidates = []
    for tolerance, offset in product((.45, .65, .9), offsets):
        angle = base_angle + np.deg2rad(offset)
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        local = ring @ rotation
        vertices = np.asarray(Polygon(local).simplify(tolerance).exterior.coords[:-1])
        indices = [int(np.argmin(np.linalg.norm(local-p, axis=1))) for p in vertices]
        groups = []
        for i, a in enumerate(vertices):
            delta = vertices[(i+1) % len(vertices)]-a
            length = np.linalg.norm(delta)
            axis = int(abs(delta[1]) > abs(delta[0]))
            error = abs((np.arctan2(delta[1], delta[0])+np.pi/4) % (np.pi/2)-np.pi/4)
            pts = _samples(LineString([a @ rotation.T, (a+delta) @ rotation.T]))
            fraction = np.mean(intersects_xy(canopy, *pts.T))
            # A clearly visible diagonal is an independent architectural side,
            # not noise to snap to the main building's right-angle grid.
            oblique = error > np.deg2rad(28) and length >= 1.5 and fraction < .3
            label = ('oblique', i) if oblique else (axis, 1 if delta[axis] > 0 else -1)
            start, end = indices[i], indices[(i+1) % len(indices)]
            samples = local[np.arange(start, end+1 if end > start else end+len(local)+1) % len(local)]
            # Parallel eaves on successive units may be offset. Keep that
            # setback instead of averaging both sides into a diagonal run.
            setback = (hypothesis and groups and label[0] != 'oblique' and groups[-1][0] == label
                       and abs(np.median(np.asarray(groups[-1][1])[:, 1-axis])
                               - np.median(samples[:, 1-axis])) > .45)
            if groups and groups[-1][0] == label and not setback:
                groups[-1][1].extend(samples)
            else:
                groups.append([label, list(samples)])
        if (len(groups) > 1 and groups[-1][0] == groups[0][0]
                and (not hypothesis or abs(np.median(np.asarray(groups[-1][1])[:, 1-groups[0][0][0]])
                        - np.median(np.asarray(groups[0][1])[:, 1-groups[0][0][0]])) <= .45)):
            groups[0][1].extend(groups.pop()[1])
        lines, supported = [], True
        for label, samples in groups:
            samples = _samples(LineString(samples))
            covered = intersects_xy(canopy, *(samples @ rotation.T).T)
            weights = np.where(covered, .03, 1.)
            center = np.average(samples, axis=0, weights=weights)
            if label[0] == 'oblique':
                _, _, axes = np.linalg.svd((samples-center)*np.sqrt(weights[:, None]), full_matrices=False)
                normal = axes[-1]
            else:
                normal = np.zeros(2)
                normal[1-label[0]] = 1.
            # An obscured eave still needs at least a metre (or 20% for a short
            # side) of observed support. Canopy masks alone do not locate it.
            observed_length = (1-np.mean(covered))*LineString(samples).length
            if observed_length < min(1., LineString(samples).length*.2):
                supported = False
                break
            lines.append((normal, float(normal @ center)))
        if not supported or len(lines) < 4:
            continue
        stepped = []
        for i, (normal, offset) in enumerate(lines):
            previous, previous_offset = lines[i-1]
            if abs(np.linalg.det([previous, normal])) < .1:
                if abs(offset-previous_offset) < .15 or np.dot(normal, previous) < .99:
                    break
                tangent = np.array([-normal[1], normal[0]])
                junction = np.asarray(groups[i][1][0])
                stepped.append((tangent, float(tangent @ junction)))
            stepped.append((normal, offset))
        else:
            lines = stepped
        corners = []
        for i, (normal, offset) in enumerate(lines):
            matrix = np.array([lines[i-1][0], normal])
            if abs(np.linalg.det(matrix)) < .1:
                break
            corners.append(np.linalg.solve(matrix, [lines[i-1][1], offset]))
        if len(corners) != len(lines):
            continue
        candidate = Polygon(np.asarray(corners) @ rotation.T)
        output = Polygon(pixels(candidate.exterior.coords), [h.coords for h in polygon.interiors])
        if (not output.is_valid or not box(0, 0, *image_size).covers(output)
                or candidate.area <= 0 or len(corners) > len(ring)):
            continue
        # Check both directions. Unsupported additions cannot hide behind a
        # good average, and real courtyards/large concave wings remain intact.
        differences = candidate.symmetric_difference(raw)
        visible_fraction = differences.difference(canopy).area/raw.area
        total_fraction = differences.area/raw.area
        distances, hidden_distances = [], []
        for first, second in ((raw, candidate), (candidate, raw)):
            points = _samples(first.boundary, .08)
            covered = intersects_xy(canopy, *points.T)
            dist = second.boundary.distance(shapely_points(points))
            distances.extend(dist[~covered])
            hidden_distances.extend(dist[covered])
        if not distances:
            continue
        maximum, p95 = float(max(distances)), float(np.quantile(distances, .95))
        hidden = float(max(hidden_distances, default=0))
        if (maximum > max_visible or p95 > p95_limit
                or hidden > 2. or visible_fraction > area_limit or total_fraction > .10):
            continue
        inferred = []
        coords = np.asarray(candidate.exterior.coords)
        for a, b in zip(coords[:-1], coords[1:], strict=True):
            segment = LineString([a, b])
            hidden_line = segment.intersection(canopy)
            if hidden_line.length > .5:
                inferred.append({'line_pixels': pixels([a, b]).tolist(), 'canopy_length_m': round(hidden_line.length, 3)})
        detail = {**report, 'accepted': True, 'reason': 'visible-eaves-and-canopy' if inferred else 'visible-eaves',
                  'axis_coherence': round(float(coherence), 4), 'axis_angle_deg': round(float(np.rad2deg(angle)), 3),
                  'axis_method': axis_method,
                  'hypothesis_only': hypothesis,
                  'input_vertices': len(ring), 'output_vertices': len(corners),
                  'max_visible_deviation_m': round(maximum, 4), 'p95_visible_deviation_m': round(p95, 4),
                  'max_canopy_deviation_m': round(hidden, 4), 'visible_area_change_fraction': round(visible_fraction, 5),
                  'area_change_fraction': round(total_fraction, 5), 'inferred_edges': inferred,
                  'limits': {'max_visible_deviation_m': max_visible, 'p95_visible_deviation_m': p95_limit,
                             'max_canopy_deviation_m': 2., 'visible_area_change_fraction': area_limit, 'area_change_fraction': .10},
                  'canopy_count': len(nearby), 'review_required': True}
        candidates.append(((len(corners), visible_fraction), output, detail))
    if not candidates:
        return polygon, {**report, 'reason': 'unsupported-angular-fit'}
    _, output, detail = min(candidates, key=lambda item: item[0])
    return output, detail
