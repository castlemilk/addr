"""Competing flat-surface hypothesis, independent of pitched-fold scoring.

An aerial image without ridges cannot distinguish a flat roof from a shallow
slope, occluded pitches or stepped levels. This branch supplies a real audited
surface for comparison, without treating fewer lines as classification evidence.
"""
from __future__ import annotations

import copy

import cv2
import numpy as np
from shapely.geometry import LineString, Polygon

from .roof_boundary import model_canopies, regularize_eaves
from .scene_roof import propose_roof
from .scene_self_review import GEOMETRY_ERRORS, local_outline

VERSION = 'roof-family-review-v2'


def straight_flat_outline(obj, scene, image):
    candidate = copy.deepcopy(obj)
    raw = obj.get('boundary_refinement', {}).get('raw_ring')
    if (image is None or not raw or obj.get('source') != 'sam3-text-onnx'
            or obj.get('geometry_reviewed') or obj.get('roof_line_edits')):
        return candidate, {'accepted': False, 'reason': 'No independent raw detector boundary'}
    source = Polygon(raw, obj.get('holes', []))
    if not source.is_valid:
        return candidate, {'accepted': False, 'reason': 'Invalid raw boundary'}
    shape, report = regularize_eaves(source, image.size, scene['pixel_to_meters'],
                                     model_canopies(scene['objects']), hypothesis=True)
    if not report['accepted']:
        return candidate, report
    gray = cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY)
    distances = cv2.distanceTransform(255-cv2.Canny(gray, 50, 120), cv2.DIST_L2, 3)
    def residual(ring):
        line = LineString([*ring, ring[0]])
        points = np.array([line.interpolate(t, normalized=True).coords[0] for t in np.linspace(0, 1, 400)])
        points = np.clip(points.astype(int), (0, 0), (image.width-1, image.height-1))
        return float(np.minimum(distances[points[:, 1], points[:, 0]], 10).mean())
    original, fitted = residual(obj['ring']), residual(list(shape.exterior.coords[:-1]))
    report.update(original_image_residual_px=round(original, 4), fitted_image_residual_px=round(fitted, 4))
    if fitted > original+.15 or fitted > 1.5:
        return candidate, {**report, 'accepted': False, 'reason': 'Straight-edge hypothesis lacks original-image support'}
    if len(shape.exterior.coords)-1 > len(obj['ring']):
        return candidate, {**report, 'accepted': False, 'reason': 'Angular fit adds boundary complexity'}
    candidate['ring'] = [[round(float(x), 4), round(float(y), 4)] for x, y in shape.exterior.coords[:-1]]
    frame = np.asarray(scene['pixel_to_meters'])
    metric = Polygon(np.asarray(candidate['ring'])@frame[:2]+frame[2],
                     [np.asarray(h)@frame[:2]+frame[2] for h in obj.get('holes', [])])
    box = np.asarray(metric.minimum_rotated_rectangle.exterior.coords)
    widths = np.linalg.norm(np.diff(box, axis=0), axis=1)
    candidate['model'].update(area_m2=round(metric.area, 2), footprint_area_px=round(shape.area, 2),
                              width_m=round(float(min(widths)), 2), length_m=round(float(max(widths)), 2))
    candidate['boundary_refinement']['construction'] = report
    return candidate, report


def flat_alternative(obj, scene, image=None):
    candidate, boundary = straight_flat_outline(obj, scene, image)
    ring, holes = local_outline(candidate, scene)
    try:
        proposal = propose_roof(ring, obj['model']['height_m'], .1, 'flat', holes=holes)
    except GEOMETRY_ERRORS as error:
        return {'version': VERSION, 'status': 'unavailable', 'reason': str(error), 'alternatives': []}
    candidate.update(roof_annotation=proposal['annotation'], roof_parts=proposal['parts'],
                     roof_lines=[], roof_type='flat', roof_geometry_source='roof-family-hypothesis',
                     roof_geometry_note='Level surface hypothesis. Slope, parapets, drains and roof levels are unresolved.',
                     geometry_reviewed=False, status='candidate',
                     model={**candidate['model'], 'height_verified': False})
    # Never carry line edits or reviewed metadata into a generated hypothesis.
    for key in ('roof_line_edits', 'roof_feedback', 'geometry_reviewed_at'):
        candidate.pop(key, None)
    return {'version': VERSION, 'status': 'needs_family_evidence', 'selected_family': None, 'boundary_review': boundary,
            'reason': 'Compare flat / low-pitch against pitched forms. Missing ridges alone do not establish flatness; adjoining levels may need separate surfaces.',
            'required_evidence': ['Roof-family feedback or independent slope/height evidence',
                                  'Check parapets, drains and level changes',
                                  'Exclude panel, equipment and material edges from structural folds'],
            'alternatives': [{'family': 'flat', 'label': 'Flat / unresolved low-pitch', 'object': candidate,
                              'geometry_passed': proposal['audit']['passed'], 'drainage_assessed': False,
                              'eligible': False, 'height_verified': False,
                              'reason': 'Geometry is checked; roof family and drainage need independent evidence.'}]}
