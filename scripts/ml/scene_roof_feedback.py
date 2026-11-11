"""Image-observed roof labels, independently gated from estimated 3D heights."""
from __future__ import annotations

import math

import numpy as np
from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiPoint, Polygon
from shapely.ops import unary_union

from .review_authority import review_authority_blocker

FAMILIES = ('gabled', 'hipped', 'flat', 'mixed')


def checked_linework(obj):
    graph = obj.get('roof_annotation')
    if not graph:
        return None
    return {'vertices': [p[:2] for p in graph['vertices']], 'faces': graph['faces'], 'edges': graph['edges']}


def linework_problem(lines, obj, model):
    """Audit XY coverage independently; plausible heights cannot approve lines."""
    try:
        points = np.asarray(lines['vertices'], dtype=float)
        if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
            return 'invalid_linework'
        pixels = np.c_[points, np.ones(len(points))] @ np.asarray(model['meters_to_pixel'])
        footprint = Polygon(obj['ring'], obj.get('holes'))
        if not np.isfinite(pixels).all() or not footprint.buffer(.05).covers(MultiPoint(pixels)):
            return 'invalid_linework'
        faces = []
        for face in lines['faces']:
            if len(set(face)) < 3 or any(type(i) is not int or i < 0 or i >= len(points) for i in face):
                return 'invalid_linework'
            polygon = Polygon(pixels[face])
            if not polygon.is_valid or polygon.area <= 1e-7:
                return 'invalid_linework'
            faces.append(polygon)
        if not faces:
            return 'invalid_linework'
        union = unary_union(faces)
        tolerance = max(.05, footprint.area * .005)
        if union.symmetric_difference(footprint).area > tolerance or sum(p.area for p in faces) - union.area > tolerance:
            return 'linework_coverage'
        boundaries = unary_union([p.boundary for p in faces]).buffer(.05)
        for edge in lines['edges']:
            a, b = edge['a'], edge['b']
            if (type(a) is not int or type(b) is not int or a == b or min(a, b) < 0 or max(a, b) >= len(points)
                    or edge['kind'] not in ('ridge', 'hip', 'valley', 'eave', 'step_eave')):
                return 'invalid_linework'
            line = LineString([pixels[a], pixels[b]])
            if line.length <= 1e-7 or not boundaries.covers(line):
                return 'invalid_linework'
        return None
    except (KeyError, TypeError, ValueError, IndexError, GEOSException):
        return 'invalid_linework'


def roof_targets(obj, model, blockers):
    """Return independent targets and reasons; never infer checks from edits/swipes."""
    feedback = obj.get('roof_feedback') or {}
    reference = {}
    blocked = {'family': [], 'linework': []}
    checked = {'family': feedback.get('family') in FAMILIES, 'linework': bool(feedback.get('linework'))}
    common = list(blockers)
    if authority_blocker := review_authority_blocker(obj):
        common.append(authority_blocker)
    if obj['status'] != 'confirmed' or not obj.get('geometry_reviewed') or {'outline', 'class', 'instances'}.intersection(obj.get('review_issues', [])):
        common.append('outline_unreviewed')
    if obj.get('visibility') != 'full':
        common.append('visibility')
    if feedback.get('outline') != {'ring': obj['ring'], 'holes': obj.get('holes', [])}:
        common.append('changed_outline')
    for target in blocked:
        blocked[target] = list(common) if checked[target] else ['unchecked']
    if checked['family'] and not blocked['family']:
        reference['family'] = feedback['family']
    if checked['linework']:
        edits = obj.get('roof_line_edits')
        if edits and edits.get('outline') == {'ring': obj['ring'], 'holes': obj.get('holes', [])}:
            blocked['linework'].append('separate_line_edits')
        lines = feedback['linework']
        if lines != checked_linework(obj):
            blocked['linework'].append('changed_linework')
        if 'shape_3d' in obj.get('review_issues', []):
            blocked['linework'].append('shape_flag')
        issue = linework_problem(lines, obj, model)
        if issue:
            blocked['linework'].append(issue)
        if not blocked['linework']:
            # Export registered image XY only, never copied estimated Z values.
            vertices = np.c_[np.asarray(lines['vertices']), np.ones(len(lines['vertices']))] @ np.asarray(model['meters_to_pixel'])
            if not all(math.isfinite(n) for p in vertices for n in p):
                blocked['linework'].append('invalid_linework')
            else:
                reference['linework'] = {**lines, 'vertices': vertices.tolist(), 'coordinate_space': 'image_pixels'}
    return reference, checked, blocked
