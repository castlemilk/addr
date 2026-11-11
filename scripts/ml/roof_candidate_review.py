"""Independent candidate gates: original imagery, unique surfaces and fixed evidence.

These are inference diagnostics, not probabilities, measurements or training labels.
"""
from __future__ import annotations

import copy

import cv2
import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .roof_boundary import model_canopies
from .roof_line_fit import image_segments, refine_roof_lines
from .roof_observation_checks import narrow_connections
from .roof_structure import shared_edges, structural_prior
from .roof_surface import refine_roof_surface
from .roof_topology import clip_support, topology_cost
from .rooflines import _classify_roofline
from .scene_roof import propose_roof
from .scene_self_review import GEOMETRY_ERRORS, audit_object, local_outline, mesh_parts

VERSION = 'roof-candidate-gates-v5'


def overlap_height(parts):
    """Reject multiple heights over the same XY, even in a small overlap."""
    worst, area = 0., 0.
    for i, first in enumerate(parts):
        for second in parts[i+1:]:
            overlap = Polygon(first['coords']).intersection(Polygon(second['coords']))
            if overlap.area < 1e-6:
                continue
            delta = np.array([first['plane'][k]-second['plane'][k] for k in ('a', 'b', 'c')])
            for region in [overlap] if overlap.geom_type == 'Polygon' else getattr(overlap, 'geoms', []):
                if region.geom_type == 'Polygon':
                    xy = np.array(region.exterior.coords)
                    error = float(np.max(np.abs(np.c_[xy, np.ones(len(xy))] @ delta)))
                    worst = max(worst, error)
                    if error > .002:
                        area += region.area
    return {'max_height_difference_m': round(worst, 6), 'conflicting_area_m2': round(area, 6)}


def fold_support(parts, observations):
    """Model-to-image length coverage complements image-to-model residuals.

    Collinear observations cover only their finite extent; duplicate segments
    cannot inflate coverage. Absence of support remains uncertainty, not proof
    that a fold is false (occlusion and uniform materials are possible).
    """
    total, supported = 0., 0.
    for raw in shared_edges(parts):
        line = LineString(raw)
        if line.length < .1:
            continue
        tangent = (np.array(raw[1])-raw[0])/line.length
        regions = [LineString(o['points']).buffer(.4, cap_style='flat') for o in observations
                   if abs(tangent @ o['tangent']) >= .97]
        total += line.length
        if regions:
            supported += line.intersection(unary_union(regions)).length
    return {'supported_fold_fraction': round(supported/total, 5) if total else 0.,
            'unsupported_fold_m': round(max(0., total-supported), 4)}


def evidence_context(obj, scene, image):
    ring, holes = local_outline(obj, scene)
    polygon = Polygon(ring, holes)
    # One immutable observation set for every candidate, even if its outline changes.
    observations = image_segments(image, polygon, scene['meters_to_pixel'])
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 120)
    distances = cv2.distanceTransform(255-edges, cv2.DIST_L2, 3)
    return polygon, observations, distances


def boundary_residual(obj, distances):
    line = LineString([*obj['ring'], obj['ring'][0]])
    points = [line.interpolate(i/199, normalized=True) for i in range(200)]
    height, width = distances.shape
    values = [float(distances[int(np.clip(p.y, 0, height-1)), int(np.clip(p.x, 0, width-1))]) for p in points]
    return round(float(np.mean(np.minimum(values, 10))), 4)


def inspect(obj, scene, image, context):
    audit = audit_object(obj, scene, image)
    audit['roof_family'] = obj.get('roof_type')
    polygon, observations, distances = context
    audit['boundary_edge_distance_px'] = boundary_residual(obj, distances)
    audit['fixed_observations'] = len(observations)
    if not obj.get('roof_parts'):
        return audit
    parts = mesh_parts(obj)
    audit['surface_overlap'] = overlap_height(parts)
    if audit['surface_overlap']['conflicting_area_m2'] > 0:
        audit['geometry_passed'] = False
        audit['issues'] = [i for i in audit['issues'] if i['code'] != 'editor_mesh_mismatch']
        audit['issues'].append({'code': 'conflicting_surface_overlap', 'severity': 'defect',
                                'message': 'Overlapping roof faces give different heights at the same position.'})
    cost = topology_cost(parts, observations)
    cost.update(fold_support(parts, observations))
    cost['comparison_score'] = round(cost['score'] + 2*(1-cost['supported_fold_fraction']), 5)
    audit['cost'] = cost
    if cost['crease_runs'] > 0 and cost['supported_fold_fraction'] < .7:
        audit['issues'].append({'code': 'unsupported_fold_extent', 'severity': 'review',
                                'message': 'Much of the proposed fold length lacks registered image-line support.'})
    ring, holes = local_outline(obj, scene)
    proposed = Polygon(ring, holes)
    audit['retained_observation_fraction'] = (sum(LineString(o['points']).intersection(proposed).length for o in observations)
                                             /sum(o['length'] for o in observations)) if observations else 0.
    audit['outline_iou'] = proposed.intersection(polygon).area/proposed.union(polygon).area
    audit['outline_shift_m'] = polygon.boundary.hausdorff_distance(proposed.boundary)
    return audit


def candidate_gate(before, after):
    if not after.get('geometry_passed'):
        return False, 'Physical geometry or unique-surface check failed.'
    if after.get('roof_family') == 'flat':
        return False, 'Flat surfaces need independent roof-family evidence; absent folds and lower complexity cannot establish flatness.'
    if after['fixed_observations'] < 3:
        return False, 'Insufficient original-image evidence; request better imagery or human line feedback.'
    a, b = before.get('cost'), after['cost']
    if after['retained_observation_fraction'] < .98:
        return False, 'The candidate drops observed roof lines outside its outline.'
    if after['outline_iou'] < .95 or after['outline_shift_m'] > .5:
        return False, 'Boundary change exceeds the conservative comparison budget.'
    if after['boundary_edge_distance_px'] > before['boundary_edge_distance_px']+.15:
        return False, 'Original-image boundary support becomes worse.'
    if b['supported_fold_fraction'] < max(.35, (a or {}).get('supported_fold_fraction', 0)-.02):
        return False, 'Too many proposed folds lack image support.'
    if a and b['image_line_mismatch_m'] > a['image_line_mismatch_m']+.03:
        return False, 'Existing image-line agreement becomes worse.'
    if not a and b['image_line_mismatch_m'] > .75:
        return False, 'New roof surface lacks sufficient image agreement.'
    if a and before['geometry_passed']:
        if a['comparison_score']-b['comparison_score'] < max(.15, .08*a['comparison_score']):
            return False, 'No material combined geometry/image improvement.'
        if b['construction_complexity'] > a['construction_complexity']+.05:
            return False, 'Candidate introduces additional construction complexity.'
    return True, 'Passes candidate checks; remains an unverified model hypothesis.'


def strategies(obj, scene, image):
    """Bounded general hypotheses, never hand-entered roof coordinates."""
    outlines = [('existing-outline', copy.deepcopy(obj))]
    raw = obj.get('boundary_refinement', {})
    if raw.get('raw_ring'):
        polygon, refinement = refine_roof_surface(Polygon(raw['raw_ring'], raw.get('raw_holes', [])), image,
                                                  scene['pixel_to_meters'], canopies=model_canopies(scene['objects']))
        ring = [list(p) for p in polygon.exterior.coords[:-1]]
        if polygon.is_valid and Polygon(obj['ring'], obj.get('holes', [])).symmetric_difference(polygon).area > .1:
            changed = copy.deepcopy(obj)
            changed.update(ring=ring, holes=[list(map(list, h.coords[:-1])) for h in polygon.interiors], boundary_refinement=refinement)
            outlines.append(('raw-mask-eaves', changed))
        # Rectangular construction is an alternative, never an imposed label.
        # Propose from the raw silhouette; later gates limit displacement and
        # require non-worsening edge support in the original image.
        raw_polygon = Polygon(raw['raw_ring'], raw.get('raw_holes', []))
        rectangle = raw_polygon.minimum_rotated_rectangle
        if not raw_polygon.interiors and raw_polygon.area/rectangle.area >= .9:
            changed = copy.deepcopy(obj)
            changed.update(ring=list(map(list, rectangle.exterior.coords[:-1])), holes=[])
            outlines.append(('rectangular-eaves', changed))
    eave = obj['model']['height_m']
    maximum = max((z for p in obj.get('roof_parts', []) for z in p['ring_z']), default=eave+2.)
    rise = max(.5, min(5., maximum-eave))
    for name, shape in outlines:
        for form in ('auto', 'gabled', 'hipped'):
            yield name+'/'+form, shape, form, rise
    # A few extra silhouette corners should not force a fan of roof planes.
    # Fit two/four planes to a nearby rectangular support, then clip back to
    # the exact outline. Height/coverage/runoff and image checks still decide.
    ring, holes = local_outline(obj, scene)
    polygon = Polygon(ring, holes)
    if not holes and polygon.area/polygon.minimum_rotated_rectangle.area >= .85:
        for form in ('gabled', 'hipped'):
            yield 'broad-planes/'+form, obj, 'support-'+form, rise


def broad_plane_proposal(shape, scene, image, rise, family):
    ring, holes = local_outline(shape, scene)
    polygon, eave = Polygon(ring, holes), shape['model']['height_m']
    support = polygon.minimum_rotated_rectangle
    parts = structural_prior(support, eave, eave+rise, family, regularize_eaves=False)
    parts, _ = refine_roof_lines(parts, support, eave, image, scene['meters_to_pixel'], evidence_footprint=polygon)
    parts = clip_support(parts, polygon, eave, family)
    vertices, faces, indices = [], [], {}
    for part in parts:
        face = []
        for xy, z in zip(part['coords'], part['ring_z'], strict=True):
            key = tuple(round(v, 6) for v in [*xy, z])
            if key not in indices:
                indices[key] = len(vertices)
                vertices.append([*xy, z])
            face.append(indices[key])
        faces.append(face)
    edges, lines = [], []
    for line in shared_edges(parts):
        ids = [next((i for i, v in enumerate(vertices) if np.linalg.norm(np.array(v[:2])-p) < 1e-5), None) for p in line]
        if None not in ids:
            kind = _classify_roofline(line, parts)
            edges.append({'a': ids[0], 'b': ids[1], 'kind': kind})
            lines.append({'line_meters': line, 'kind': kind})
    return {'annotation': {'schema_version': 1, 'roof_type': family, 'vertices': vertices, 'faces': faces, 'edges': edges},
            'parts': [{'ring_meters': p['coords'], 'ring_z': p['ring_z']} for p in parts], 'lines': lines,
            'roof_type': family, 'note': 'Broad planar support clipped to the outline. Roof form and heights remain unverified.'}


def improve(obj, scene, image, *, equipment=None):
    from .roof_family_review import flat_alternative
    from .roof_image_evidence import quality_report
    from .roof_unit_patterns import review_units

    context = evidence_context(obj, scene, image)
    before = inspect(obj, scene, image, context)
    review_layers = {'family_review': flat_alternative(obj, scene, image),
                     'unit_review': review_units(obj, scene, image, equipment, context[1]),
                     'imagery_review': quality_report(obj, scene, image)}
    attempts, best, best_audit, best_name = [], None, None, None
    rejected, rejected_rank = None, None
    if any(i['code'] == 'image_cut' for i in before['issues']):
        return {**review_layers, 'status': 'needs_evidence', 'before': before, 'attempts': [],
                'reason': 'Roof touches the image edge; capture more context before fitting its shape.'}, None
    connection = narrow_connections(context[0])
    if connection:
        return {**review_layers, 'status': 'needs_evidence', 'before': before, 'attempts': [], 'connection_check': connection,
                'reason': 'Substantial roof sections meet through a narrow connection. Check whether these are separate roofs or a real connector before fitting one surface.'}, None
    if before['fixed_observations'] < 3:
        return {**review_layers, 'status': 'needs_evidence', 'before': before, 'attempts': [],
                'reason': 'Fewer than three usable original-image lines; no automatic shape replacement.'}, None
    for name, shape, form, rise in strategies(obj, scene, image):
        try:
            ring, holes = local_outline(shape, scene)
            proposal = (broad_plane_proposal(shape, scene, image, rise, form.removeprefix('support-')) if form.startswith('support-') else
                        propose_roof(ring, shape['model']['height_m'], rise, form, image=image,
                                     meters_to_pixel=scene['meters_to_pixel'], holes=holes))
            candidate = {**copy.deepcopy(shape), 'roof_annotation': proposal['annotation'], 'roof_parts': proposal['parts'],
                         'roof_lines': proposal['lines'], 'roof_type': proposal['roof_type'],
                         'roof_geometry_source': 'self-review-candidate', 'roof_geometry_note': proposal['note'],
                         'status': 'candidate', 'geometry_reviewed': False,
                         'model': {**shape['model'], 'height_verified': False}}
            after = inspect(candidate, scene, image, context)
            allowed, reason = candidate_gate(before, after)
            attempts.append({'strategy': name, 'eligible': allowed, 'reason': reason, 'audit': after})
            if allowed and (best is None or after['cost']['comparison_score'] < best_audit['cost']['comparison_score']):
                best, best_audit, best_name = candidate, after, name
            elif not allowed and after.get('cost'):
                rank = (not after['geometry_passed'], after['cost']['comparison_score'])
                if rejected_rank is None or rank < rejected_rank:
                    rejected_rank = rank
                    rejected = {'object': candidate, 'audit': after, 'strategy': name, 'reason': reason, 'eligible': False}
        except GEOMETRY_ERRORS as error:
            attempts.append({'strategy': name, 'eligible': False, 'reason': str(error)})
    return {**review_layers, 'status': 'candidate_ready' if best else 'no_safe_gain', 'before': before, 'after': best_audit,
            'strategy': best_name, 'attempts': attempts,
            'rejected_preview': rejected if best is None else None,
            'reason': 'A better-scoring candidate is ready to compare; production model is unchanged.' if best else
                      'No alternative passed every check. Inspect segmentation, occlusion or image detail.'}, best
