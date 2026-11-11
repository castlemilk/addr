"""Registered image-line refinement of a physically checked roof proposal.

CLAHE only reveals existing contrast: original RGB remains the evidence gate.
Joint vertex/plane fitting keeps the footprint and face connectivity intact.
Diagnostics are fitting residuals, never calibrated accuracy or checked labels.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.optimize import least_squares
from shapely import constrained_delaunay_triangles
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .roof_form import line_evidence
from .roof_structure import _parts_from_faces, audit_roof

VERSION = 'registered-roof-lines-v6'
# Extra evidence views (CP3) are opt-in. VERSION stays v6 because refine_roof_lines
# writes it as the report 'method' of every proposal (surfaced by the API, review
# tools and eyes diagnostics) and the module hash already enters model_signature;
# bumping it would relabel every default run that never saw an extra view. The
# extra-view method is recorded separately, only when views are used.
EXTRA_VIEWS_VERSION = 'registered-roof-lines-v8-layer-evidence'
# Layer evidence (optical-layers-v1): an extra view named 'layer:<name>' declares itself a deterministic
# photometric transform of the saved pixels (scripts.ml.optical_layers). A line detected on such a view
# may pass the evidence gate on that view's own array when the original RGB refuses it; views without the
# prefix (dated captures, stack medians) keep the original-RGB gate. Same pixels, no new evidence source.
LAYER_VIEW_PREFIX = 'layer:'
EVIDENCE_GATE = {'min_score': .12, 'min_samples': 6}


def is_layer_view(name):
    return isinstance(name, str) and name.startswith(LAYER_VIEW_PREFIX) and len(name) > len(LAYER_VIEW_PREFIX)


def enhance_roof_detail(image):
    """Photometric-only view: same resolution, registration and source pixels."""
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    denoised = cv2.bilateralFilter(gray, 5, 15, 3)
    return cv2.createCLAHE(clipLimit=2., tileGridSize=(8, 8)).apply(denoised)


def validate_extra_views(extra_views, image):
    """Named HxW uint8 arrays on the saved image grid: LSD views only, never evidence themselves."""
    if not isinstance(extra_views, (list, tuple)):
        raise ValueError('extra_views must be a list of (name, HxW uint8 array) pairs')
    width, height = image.size
    names = []
    for item in extra_views:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError('extra_views must be a list of (name, HxW uint8 array) pairs')
        name, view = item
        if not isinstance(name, str) or not name or name in ('gray', 'clahe') or name in names:
            raise ValueError('extra_views names must be unique non-empty strings other than gray/clahe')
        if not isinstance(view, np.ndarray) or view.dtype != np.uint8 or view.shape != (height, width):
            raise ValueError(f'extra_views[{name!r}] must be an HxW uint8 array matching the image ({height}x{width})')
        names.append(name)
    return names


def image_segments(image, footprint, meters_to_pixel, *, extra_views=None, layer_evidence=True,
                   views_only=False, diagnostics=None):
    """LSD lines from the original gray and CLAHE views, gated by line_evidence on the original RGB.

    extra_views (default None: behaviour and return value unchanged) appends named HxW
    uint8 arrays (e.g. a registered multi-date median) as further LSD views. Their lines
    pass the same dedupe and the same original-RGB line_evidence gate; when views are
    given every returned segment carries 'view_origin' ('gray', 'clahe' or the view name).

    layer_evidence (default True; only consulted when extra_views holds a view named
    'layer:<name>', see is_layer_view): a line detected on such a same-pixel layer that the
    original RGB refuses may instead pass the identical gate scored on that layer's own
    array. With any layer view present every returned segment carries 'evidence_layer'
    ('original' or the layer name) and 'support' is the score of the array that passed it.
    Gray/CLAHE lines and unprefixed views never take layer evidence.

    views_only is an internal discovery mode for controlled optical guidance:
    filter one named layer independently, then append survivors to the frozen
    original observations. diagnostics records the original RGB gate, including
    refusals; neither option changes the ordinary detector path.
    """
    frame = np.asarray(meters_to_pixel, dtype=float)
    if frame.shape != (3, 2) or not np.isfinite(frame).all() or abs(np.linalg.det(frame[:2])) < 1e-10:
        raise ValueError('A registered metric image frame is required')
    if extra_views is not None:
        validate_extra_views(extra_views, image)
    if views_only and not extra_views:
        raise ValueError('views_only requires a named extra view')
    layers_active = bool(layer_evidence) and extra_views is not None and any(is_layer_view(name) for name, _ in extra_views)
    inverse = np.linalg.inv(frame[:2])

    class Frame:
        def metre_to_pixel(self, x, y):
            return np.array([x, y, 1.]) @ frame

    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    pixels = np.c_[np.asarray(footprint.exterior.coords), np.ones(len(footprint.exterior.coords))] @ frame
    roi = Polygon(pixels).buffer(-3)
    detector = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD)
    segments = []
    views = [] if views_only else [('gray', gray), ('clahe', enhance_roof_detail(image))]
    if extra_views is not None:
        views.extend((name, np.ascontiguousarray(view)) for name, view in extra_views)
    for origin, view in views:
        detected = detector.detect(view)[0]
        if detected is None:
            continue
        for raw in detected.reshape(-1, 2, 2):
            line = LineString(raw)
            inside = line.intersection(roi)
            if line.length < 12 or inside.geom_type != 'LineString' or inside.length < max(12., line.length*.7):
                continue
            raw = np.array([inside.coords[0], inside.coords[-1]])
            points = (raw-frame[2]) @ inverse
            vector = points[1]-points[0]
            length = np.linalg.norm(vector)
            if length < 1.4 or LineString(points).intersection(footprint).length < length*.99:
                continue
            tangent = vector/length
            normal = np.array([-tangent[1], tangent[0]])
            # Remove duplicate original/enhanced detections without treating two
            # nearby independent roof features as repeated evidence.
            if any(abs(tangent @ old['tangent']) > .995
                   and np.max(np.abs((points-old['points'][0]) @ old['normal'])) < .18
                   and LineString(points).distance(LineString(old['points'])) < .18
                   for old in segments):
                continue
            evidence = line_evidence(points, image, Frame(), footprint)
            passed = evidence['score'] >= EVIDENCE_GATE['min_score'] and evidence['samples'] >= EVIDENCE_GATE['min_samples']
            if diagnostics is not None:
                diagnostics.append({'points_meters': points.tolist(), 'view_origin': origin,
                                    'length_m': float(length), 'original_rgb': dict(evidence),
                                    'original_gate_passed': bool(passed)})
            evidence_layer = 'original'
            if not passed and layers_active and is_layer_view(origin) and evidence['samples'] >= EVIDENCE_GATE['min_samples']:
                # The same gate on the layer's own pixels (a gray triplet, so no chroma test): allowed only
                # where the original RGB sampled roof on both sides, so its vegetation and material vetoes
                # (which discard samples) cannot be bypassed by a colourless layer.
                own = line_evidence(points, np.repeat(view[:, :, None], 3, axis=2), Frame(), footprint)
                if own['score'] >= EVIDENCE_GATE['min_score'] and own['samples'] >= EVIDENCE_GATE['min_samples']:
                    evidence, evidence_layer, passed = own, origin, True
            if not passed:
                continue
            segment = {'points': points, 'tangent': tangent, 'normal': normal,
                       'length': length, 'support': evidence['score']}
            if extra_views is not None:
                segment['view_origin'] = origin
            if layers_active:
                segment['evidence_layer'] = evidence_layer
            segments.append(segment)
    # Dense, evenly spaced parallel families are more likely tile/metal seams.
    useful = []
    for item in segments:
        offsets = sorted(float((other['points'].mean(axis=0)-item['points'].mean(axis=0)) @ item['normal'])
                         for other in segments if abs(item['tangent'] @ other['tangent']) > .995
                         and np.linalg.norm(other['points'].mean(axis=0)-item['points'].mean(axis=0)) < 6)
        separated = [value for i, value in enumerate(offsets) if i == 0 or value-offsets[i-1] > .2]
        if len(separated) >= 5 and np.median(np.diff(separated)) < .8:
            continue
        useful.append(item)
    return sorted(useful, key=lambda s: s['length']*s['support'], reverse=True)[:100]


def _graph(parts):
    vertices, faces, indices = [], [], {}
    for part in parts:
        face = []
        for xy, z in zip(part['coords'], part['ring_z'], strict=True):
            key = tuple(round(float(p), 6) for p in xy)
            if key not in indices:
                indices[key] = len(vertices)
                vertices.append([*xy, z])
            face.append(indices[key])
        faces.append(face)
    # Plane intersections can put a T-junction in one face's edge without
    # subdividing its neighbour. Node both sides before fitting so they share
    # the same movable vertex; otherwise the fit can open a gap or overlap.
    xy = np.asarray(vertices)[:, :2]
    noded = []
    for face in faces:
        ring = []
        for a, b in zip(face, face[1:]+face[:1], strict=True):
            vector = xy[b]-xy[a]
            squared = vector @ vector
            if squared < 1e-12:
                continue
            t = (xy-xy[a]) @ vector/squared
            distances = np.linalg.norm(xy-xy[a]-t[:, None]*vector, axis=1)
            inside = np.flatnonzero((t > 1e-7) & (t < 1-1e-7) & (distances < 1e-6))
            ring.extend([a, *sorted(inside.tolist(), key=lambda i: t[i])])
        noded.append(ring)
    faces = noded
    edges = {}
    for face in faces:
        for a, b in zip(face, face[1:]+face[:1], strict=True):
            key = tuple(sorted((a, b)))
            edges[key] = edges.get(key, 0)+1
    return np.array(vertices), faces, [edge for edge, count in edges.items() if count == 2]


def _ridge_priors(vertices, faces, edges, planes, plane_indices, footprint, observations):
    """Retain the bisector of evidenced level, parallel eaves on pitched pairs.

    Only symmetric seed planes with real level-eave runs qualify. Sloped gutters,
    unequal seed pitches, valleys and clipped faces without eaves are excluded.
    A long, parallel image observation can release the midpoint assumption;
    a short tile/shadow line cannot tilt an entire roof wing.
    """
    groups = {}
    for a, b in edges:
        adjacent = sorted({int(plane_indices[i]) for i, face in enumerate(faces) if a in face and b in face})
        if len(adjacent) != 2:
            continue
        p, q = planes[adjacent]
        slope = np.linalg.norm(p[:2])
        if slope < .05 or np.linalg.norm(p[:2]+q[:2]) > slope*.01:
            continue
        normal = p[:2]/slope
        center = float(vertices[a, :2] @ normal)
        eaves = []
        for group, plane in zip(adjacent, (p, q), strict=True):
            runs = []
            for i, face in enumerate(faces):
                if plane_indices[i] != group:
                    continue
                for u, v in zip(face, face[1:]+face[:1], strict=True):
                    xy = vertices[[u, v], :2]
                    if (np.ptp(vertices[[u, v], 2]) < .02
                            and abs((xy[1]-xy[0]) @ normal) < .02
                            and LineString(xy).difference(footprint.boundary.buffer(.02)).length < .02
                            # The planes rise from their eaves towards the ridge.
                            and (vertices[a, :2]-xy.mean(axis=0)) @ plane[:2] > .1):
                        runs.append((np.linalg.norm(xy[1]-xy[0]), float(vertices[[u, v], 2].mean())))
            eaves.append(runs)
        # A wing's common eave need not be the global minimum (a clipped slope
        # or a lower adjoining wing can establish that). Compare the pair.
        supported = any(sum(length for length, z in eaves[0] if abs(z-level) < .02) >= 2.
                        and sum(length for length, z in eaves[1] if abs(z-level) < .02) >= 2.
                        for _, level in eaves[0])
        if supported:
            key = tuple(adjacent)
            group = groups.setdefault(key, {'indices': set(), 'normal': normal, 'center': center})
            group['indices'].update((a, b))
    priors = []
    for group in groups.values():
        indices = sorted(group['indices'])
        normal, center = group['normal'], group['center']
        tangent = np.array([-normal[1], normal[0]])
        along = vertices[indices, :2] @ tangent
        lo, hi = min(along), max(along)
        if hi-lo < 2.:
            continue
        evidence = []
        for obs in observations:
            projected = obs['points'] @ tangent
            overlap = max(0., min(hi, max(projected))-max(lo, min(projected)))
            if (abs(obs['tangent'] @ tangent) >= np.cos(np.deg2rad(2.))
                    and overlap >= (hi-lo)*.6
                    and np.max(np.abs(obs['points'] @ normal-center)) <= 1.5):
                evidence.append(obs)
        priors.append({**group, 'indices': indices, 'centered': not bool(evidence)})
    return priors


def _independent_matches(matches, vertices):
    """One observed image segment supports one continuous model crease.

    Adjacent collinear subdivisions may share an observation, but separate or
    angled creases must not both be pulled onto that same visible feature.
    Keep the strongest supported run; ambiguous losers retain their shape prior.
    """
    by_observation = {}
    for match in matches:
        by_observation.setdefault(id(match[2]), []).append(match)
    retained = []
    for candidates in by_observation.values():
        observation = candidates[0][2]
        parents = list(range(len(candidates)))

        def root(i, parents=parents):
            while parents[i] != i:
                i = parents[i]
            return i

        for i, (a, b, _) in enumerate(candidates):
            xy = vertices[[a, b], :2]
            tangent = xy[1]-xy[0]
            tangent = tangent/np.linalg.norm(tangent)
            normal = np.array([-tangent[1], tangent[0]])
            for j, (c, d, _) in enumerate(candidates[:i]):
                if not {a, b}.intersection((c, d)):
                    continue
                other = vertices[[c, d], :2]
                vector = other[1]-other[0]
                if (abs(tangent @ vector/np.linalg.norm(vector)) > .9998
                        and np.max(np.abs((other-xy[0]) @ normal)) < .02):
                    parents[root(i)] = root(j)
        groups = {}
        for i, match in enumerate(candidates):
            groups.setdefault(root(i), []).append(match)

        def support(run, observation=observation):
            score = 0.
            for a, b, _ in run:
                xy = vertices[[a, b], :2]
                vector = xy[1]-xy[0]
                length = np.linalg.norm(vector)
                along = (observation['points']-xy[0]) @ (vector/length)
                overlap = max(0., min(length, max(along))-max(0., min(along)))
                distance = np.max(np.abs((xy-observation['points'][0]) @ observation['normal']))
                score += observation['support']*overlap/(.5+distance)
            return score

        retained.extend(max(groups.values(), key=support))
    # Preserve original ordering for stable optimisation and diagnostics.
    selected = {(a, b) for a, b, _ in retained}
    return [match for match in matches if match[:2] in selected]


def view_origin_counts(observations):
    counts = {}
    for observation in observations:
        origin = observation.get('view_origin', 'unknown')
        counts[origin] = counts.get(origin, 0)+1
    return counts


def evidence_layer_counts(observations):
    """{'original': n, 'layer:<name>': n, ...}: which array passed each observation's evidence gate."""
    counts = {}
    for observation in observations:
        layer = observation.get('evidence_layer', 'original')
        counts[layer] = counts.get(layer, 0)+1
    return counts


def refine_roof_lines(parts, footprint, eave, image, meters_to_pixel, *, evidence_footprint=None, extra_views=None,
                      layer_evidence=True, observations=None):
    report = {'method': VERSION, 'accepted': False, 'reason': 'insufficient-image-lines',
              'detected_segments': 0, 'matched_edges': 0}
    layer_views = [] if extra_views is None or not layer_evidence else [name for name, _ in extra_views if is_layer_view(name)]
    if extra_views is not None:
        report['extra_views'] = {'method': EXTRA_VIEWS_VERSION, 'names': validate_extra_views(extra_views, image)}
        report['view_origins'] = {}
        if layer_views:
            report['extra_views']['layer_evidence'] = layer_views
            report['evidence_layers'] = {}
    vertices, faces, edges = _graph(parts)
    if len(vertices) > 64 or len(faces) > 24:
        return parts, {**report, 'reason': 'complexity-limit'}
    segment_kwargs = {} if extra_views is None else {'extra_views': extra_views}
    if extra_views is not None and not layer_evidence:
        segment_kwargs['layer_evidence'] = False
    if observations is None:
        observations = image_segments(image, footprint if evidence_footprint is None else evidence_footprint, meters_to_pixel,
                                      **segment_kwargs)
    report['detected_segments'] = len(observations)
    if extra_views is not None:
        report['view_origins'] = view_origin_counts(observations)
        if layer_views:
            report['evidence_layers'] = evidence_layer_counts(observations)
    if not observations:
        return parts, report
    # Corner XY stays fixed. A gable apex on a straight boundary may move along
    # that boundary, while interior junctions can move in both image directions.
    rings = [list(footprint.exterior.coords), *[list(h.coords) for h in footprint.interiors]]
    corners = np.array([p for ring in rings for p in ring[:-1]])
    bases, lower, upper = [], [], []
    for index, vertex in enumerate(vertices):
        if np.min(np.linalg.norm(corners-vertex[:2], axis=1)) < .02:
            continue
        point = Point(vertex[:2])
        if footprint.boundary.distance(point) < .02:
            options = [(LineString([a, b]).distance(point), np.array(a), np.array(b))
                       for ring in rings for a, b in zip(ring[:-1], ring[1:], strict=True)]
            _, a, b = min(options, key=lambda item: item[0])
            length = np.linalg.norm(b-a)
            direction = (b-a)/length
            position = (vertex[:2]-a) @ direction
            basis = np.zeros((len(vertices), 2))
            basis[index] = direction
            bases.append(basis)
            lower.append(max(-1.5, .01-position))
            upper.append(min(1.5, length-.01-position))
        else:
            for axis in range(2):
                basis = np.zeros((len(vertices), 2))
                basis[index, axis] = 1
                bases.append(basis)
                lower.append(-1.5)
                upper.append(1.5)
    if not bases:
        return parts, {**report, 'reason': 'no-movable-roof-points'}
    basis = np.stack(bases, axis=2)
    movable = np.any(basis != 0, axis=(1, 2))
    matches = []
    for a, b in edges:
        start, end = vertices[[a, b], :2]
        vector = end-start
        length = np.linalg.norm(vector)
        if length < .8:
            continue
        choices = []
        for observation in observations:
            p, normal = observation['points'], observation['normal']
            distance = np.max(np.abs((np.array([start, end])-p[0]) @ normal))
            along = (p-start) @ (vector/length)
            overlap = max(0., min(length, max(along))-max(0., min(along)))
            if (abs(observation['tangent'] @ (vector/length)) < .9 or distance > 1.8
                    or overlap < min(1.2, length*.3)
                    or any(not movable[i] and abs((vertices[i, :2]-p[0]) @ normal) > .75 for i in (a, b))):
                continue
            choices.append((observation['support']*overlap/(.5+distance), observation))
        if choices:
            chosen = max(choices, key=lambda item: item[0])[1]
            matches.append((a, b, chosen))
    proposed_matches = len(matches)
    matches = _independent_matches(matches, vertices)
    report['conflicting_matches_removed'] = proposed_matches-len(matches)
    report['matched_edges'] = len(matches)
    if not matches:
        return parts, report
    count = len(bases)
    # A footprint corner above the seed's eave can be a clipped slope, not a
    # measured gutter. Keep its XY fixed, but allow its estimated height to fit
    # the shared plane. Only actual level-eave corner heights stay locked.
    free_z = np.flatnonzero(movable | (vertices[:, 2] > eave+.05))
    nz = len(free_z)
    # Clipping can split a single physical plane into multiple mesh pieces.
    # Fit one shared plane parameter set, so storage seams cannot become folds.
    planes, plane_indices = [], []
    for face in faces:
        abc = np.linalg.lstsq(np.c_[vertices[face, :2], np.ones(len(face))], vertices[face, 2], rcond=None)[0]
        group = next((i for i, prior in enumerate(planes) if np.max(np.abs(prior-abc)) < 1e-6), None)
        if group is None:
            group = len(planes)
            planes.append(abc)
        plane_indices.append(group)
    planes = np.asarray(planes)
    plane_indices = np.asarray(plane_indices)
    ridge_priors = _ridge_priors(vertices, faces, edges, planes, plane_indices, footprint, observations)
    report['ridge_priors'] = {'parallel_runs': len(ridge_priors),
                              'centered_runs': sum(p['centered'] for p in ridge_priors),
                              'image_supported_offsets': sum(not p['centered'] for p in ridge_priors)}
    report['physical_plane_parameters'] = len(planes)
    fidx = np.concatenate([[i]*len(face) for i, face in enumerate(faces)])
    vidx = np.concatenate(faces)
    triangles = []
    for face in faces:
        for tri in constrained_delaunay_triangles(Polygon(vertices[face, :2])).geoms:
            triangles.append([min(face, key=lambda i: np.linalg.norm(vertices[i, :2]-p)) for p in list(tri.exterior.coords)[:3]])
    triangles = np.array(triangles)

    def areas(v):
        a, b, c = np.moveaxis(v[triangles, :2], 1, 0)
        u, w = b-a, c-a
        return u[:, 0]*w[:, 1]-u[:, 1]*w[:, 0]

    original_areas = areas(vertices)
    aidx, bidx = np.array([m[0] for m in matches]), np.array([m[1] for m in matches])
    anchors = np.array([m[2]['points'][0] for m in matches])
    normals = np.array([m[2]['normal'] for m in matches])
    weights = np.sqrt([m[2]['support'] for m in matches])

    def alignment(v):
        return np.r_[np.sum((v[aidx, :2]-anchors)*normals, axis=1)*weights,
                     np.sum((v[bidx, :2]-anchors)*normals, axis=1)*weights]

    def decode(parameters):
        v = vertices.copy()
        v[:, :2] += np.tensordot(basis, parameters[:count], axes=([2], [0]))
        # Preserve level-eave corner elevations as well as XY. Otherwise a fitter
        # can follow shadows by tilting the gutters instead of finding a roof.
        v[free_z, 2] = parameters[count:count+nz]
        return v, parameters[count+nz:].reshape(-1, 3)

    plane_weight = 100.

    def residual(parameters):
        v, ps = decode(parameters)
        # Geometry must converge below the crease extractor's tolerance. A
        # millimetre residual on disconnected pieces of the same physical
        # plane can otherwise be misread as an extra hip or valley.
        plane_error = (np.sum(v[vidx, :2]*ps[plane_indices[fidx], :2], axis=1)+ps[plane_indices[fidx], 2]-v[vidx, 2])*plane_weight
        # Positive projected triangles prevent face inversion and crossing lines.
        topology = np.minimum(0, areas(v)*np.sign(original_areas)-np.abs(original_areas)*.02)*100
        ridge_errors = []
        for prior in ridge_priors:
            offsets = v[prior['indices'], :2] @ prior['normal']-prior['center']
            # Parallel eaves imply a parallel ridge even for unequal pitches.
            # Centre only when imagery cannot establish an asymmetric ridge.
            ridge_errors.extend((offsets if prior['centered'] else offsets-offsets.mean())*100.)
        return np.r_[plane_error, alignment(v)*4, parameters[:count]*.16,
                     (v[:, 2]-vertices[:, 2])*.5, (ps[:, :2]-planes[:, :2]).flatten()*.3, topology, ridge_errors]

    initial = np.r_[np.zeros(count), vertices[free_z, 2], planes.flatten()]
    # Bounds and priors limit uncertain shifts; Z remains an estimated shape.
    low = np.r_[lower, np.full(nz, eave), np.tile([-4., -4., -10000.], len(planes))]
    high = np.r_[upper, np.full(nz, min(100., vertices[:, 2].max()+1.)), np.tile([4., 4., 10000.], len(planes))]
    if np.any(initial < low-1e-6) or np.any(initial > high+1e-6):
        return parts, {**report, 'reason': 'unsupported-pitch-or-height'}
    try:
        fitted = least_squares(residual, np.clip(initial, low, high), bounds=(low, high), max_nfev=100, ftol=1e-6)
        # Start from the image-aligned solution, then polish coplanarity.
        # Faces may also split into fragments during later silhouette clipping,
        # so precision is required even when there are no fragments yet.
        plane_weight = 10000.
        fitted = least_squares(residual, fitted.x, bounds=(low, high), max_nfev=60, ftol=1e-8)
        candidate, _ = decode(fitted.x)
        before, after = np.mean(alignment(vertices)**2), np.mean(alignment(candidate)**2)
        result = _parts_from_faces([(candidate[f, :2].tolist(), candidate[f, 2].tolist()) for f in faces], eave, parts[0].get('roof_type', 'hipped'))
    except (ValueError, ArithmeticError, np.linalg.LinAlgError):
        return parts, {**report, 'reason': 'unstable-fit'}
    audit = audit_roof(result, footprint, eave)
    polygons = [Polygon(p['coords']) for p in result]
    exact_coverage = all(p.is_valid for p in polygons) and unary_union(polygons).symmetric_difference(footprint).area < 1e-5
    movement = float(np.max(np.linalg.norm(candidate[:, :2]-vertices[:, :2], axis=1)))
    report.update(weighted_residual_before_m=round(float(np.sqrt(before)), 4),
                  weighted_residual_after_m=round(float(np.sqrt(after)), 4), max_shift_m=round(movement, 4),
                  geometry_audit=audit)
    for prior in ridge_priors:
        offsets = candidate[prior['indices'], :2] @ prior['normal']-prior['center']
        if (np.ptp(offsets) > .025 or (prior['centered'] and np.max(np.abs(offsets)) > .025)):
            return parts, {**report, 'reason': 'parallel-eave-constraint-failed'}
    if not audit['passed'] or not exact_coverage:
        return parts, {**report, 'reason': 'geometry-check-failed'}
    if any(p['slope_deg'] > 65 for p in result):
        return parts, {**report, 'reason': 'implausible-pitch'}
    if before < .0025 or after > before*.8 or movement > 2.15:
        return parts, {**report, 'reason': 'no-clear-alignment-gain'}
    return result, {**report, 'accepted': True, 'reason': 'image-lines-and-geometry'}


def main():
    parser = argparse.ArgumentParser(description='Contrast view of an unchanged registered scene image')
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    data = args.image.read_bytes()
    if hashlib.sha256(data).hexdigest() != args.sha256:
        raise ValueError('Source image changed')
    view = enhance_roof_detail(Image.open(io.BytesIO(data)).convert('RGB'))
    Image.fromarray(view).save(sys.stdout.buffer, format='PNG')


if __name__ == '__main__':
    main()
