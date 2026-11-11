"""Route roof defects to useful next actions; never create approved labels.

These observations describe model/image consistency. Flat roofs, canopy coverage
and nearby detections remain hypotheses until independently reviewed.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import numpy as np
from shapely.geometry import LineString, Polygon, shape
from shapely.ops import unary_union

from .roof_candidate_review import evidence_context, inspect
from .roof_observation_checks import narrow_connections
from .scene_confidence import geographic

VERSION = 'roof-review-diagnostics-v4'


def diagnose(obj, scene, image, audit=None):
    context = evidence_context(obj, scene, image)
    polygon, observations, _ = context
    audit = audit or inspect(obj, scene, image, context)
    codes = {i['code'] for i in audit['issues']}
    frame = np.asarray(scene['pixel_to_meters'])
    resolution = float(np.sqrt(abs(np.linalg.det(frame[:2]))))
    # Extraction erodes three pixels and requires +/- .65 m RGB samples inside
    # the roof. Inspect beyond that margin, capped by width so a narrow roof's
    # centre ridge is not an eave cue. This is triage, not fitting evidence.
    rectangle = np.asarray(polygon.minimum_rotated_rectangle.exterior.coords)
    short_side = min(np.linalg.norm(rectangle[i+1]-rectangle[i]) for i in range(4))
    edge_band = min(2., max(.85, resolution*6), short_side*.18)
    edge_zone = polygon.boundary.buffer(edge_band)
    length = sum(o['length'] for o in observations)
    edge_fraction = sum(LineString(o['points']).intersection(edge_zone).length for o in observations)/length if length else 0.
    pixels = Polygon(obj['ring'], obj.get('holes', []))
    trees, panels = [], []
    for other in scene['objects']:
        if other.get('source') != 'sam3-text-onnx' or other.get('status') != 'candidate':
            continue
        if other['kind'] not in ('tree', 'solar_panel'):
            continue
        region = Polygon(other['ring'], other.get('holes', []))
        if region.is_valid and region.intersects(pixels.buffer(3)):
            (trees if other['kind'] == 'tree' else panels).append(region)
    canopy = pixels.intersection(unary_union(trees)).area/pixels.area if trees else 0.
    canopy_edge = pixels.boundary.intersection(unary_union(trees).buffer(3)).length/pixels.length if trees else 0.
    solar_length = 0.
    if panels:
        solar = unary_union(panels)
        for observed in observations:
            xy = np.c_[observed['points'], np.ones(2)] @ np.asarray(scene['meters_to_pixel'])
            line = LineString(xy)
            solar_length += observed['length']*line.intersection(solar).length/line.length
    solar_fraction = solar_length/length if length else 0.
    cost = audit.get('cost', {})
    actions = []

    def add(code, title, reason, action):
        actions.append({'code': code, 'title': title, 'reason': reason, 'action': action})

    if 'image_cut' in codes:
        add('capture_context', 'Recover the complete roof', 'The roof intersects the image edge.',
            'Inspect a fuller registered capture before fitting endpoints or roof shape. Do not straighten the crop edge into an eave.')
    connection = narrow_connections(polygon)
    if connection:
        add('roof_connection', 'Check whether these roofs are connected',
            'Substantial roof sections meet through a connection narrower than 1.5 m.',
            'Look for a real covered connector or a gap. Use Separate / merged objects in the editor if needed. A narrow neck alone does not prove separation; no automatic split is applied.')
    if not audit['geometry_passed']:
        add('surface_consistency', 'Repair the surface representation', 'The mesh or editable graph fails geometric checks.',
            'Compare face coverage, shared heights and drainage; keep the outline fixed while testing a repaired surface.')
    if canopy >= .08 or canopy_edge >= .15 or 'hidden_eaves' in codes:
        add('occlusion', 'Check hidden eaves', 'Model tree masks overlap or closely border the roof, or eaves were inferred behind canopy.',
            'Use visible sides as constraints; retain hidden-edge uncertainty. Compare another date or viewpoint if available.')
    if solar_fraction >= .15:
        add('rooftop_objects', 'Separate solar edges from roof folds', 'Some detected image-line length lies inside model solar masks.',
            'Inspect these lines against RGB before using them as ridges. Detector solar masks are not verified occlusion labels.')
    if len(observations) < 3:
        add('weak_observations', 'Establish the roof family', 'Too few structural-line candidates are visible to compare topology reliably.',
            'Inspect original imagery for low-pitch, shed or flat forms as well as occlusion. Sharpening alone is not new evidence.')
    elif edge_fraction >= .6 and cost.get('supported_fold_fraction', 0) < .2:
        add('roof_family', 'Check the assumed pitched form', 'Most observed line length is near the outline, with little support for internal folds.',
            'Compare roof-family hypotheses and inspect parapets, level changes and rooftop equipment. Do not infer flatness solely from absent ridges.')
    if 'complex_folds' in codes:
        add('topology', 'Check segmentation and roof topology', 'The proposed roof has many folds, tiny planes or short junctions.',
            'Check whether several buildings or roof levels were merged, then compare simpler whole-plane hypotheses against visible ridges and valleys.')
    if 'image_line_disagreement' in codes or 'unsupported_fold_extent' in codes:
        add('line_alignment', 'Inspect structural-line agreement', 'The proposed folds and registered image lines disagree or have weak support.',
            'Compare original RGB, model lines and detected lines at the same crop. Identify shadows, tile seams and missing valleys before changing the fitter.')
    if not actions:
        add('review_evidence', 'Inspect the remaining uncertainty', 'No specific diagnostic proves which roof form is correct.',
            'Check outline and linework against the original image; record uncertainty rather than approving the model automatically.')
    return {'version': VERSION, 'primary': actions[0]['code'], 'actions': actions,
            'metrics': {'image_segments': len(observations), 'connection_check': connection, 'near_eave_band_m': round(edge_band, 4),
                        'near_eave_line_fraction': round(edge_fraction, 4),
                        'model_canopy_overlap_fraction': round(canopy, 4), 'model_canopy_edge_fraction': round(canopy_edge, 4),
                        'model_solar_line_fraction': round(solar_fraction, 4),
                        'ground_pixel_m': round(resolution, 4)},
            'scope': 'Agent diagnostics only; not verified geometry, addresses, heights or training labels.'}


def context_match(target, proposed):
    """A fuller overlapping observation is a navigation suggestion, not a join."""
    if not target.is_valid or not proposed.is_valid or target.area <= 0 or proposed.area <= 0:
        return None
    retained = target.intersection(proposed).area/target.area
    ratio = proposed.area/target.area
    if retained < .9 or not 1.1 <= ratio <= 3.:
        return None
    return {'target_coverage': round(retained, 4), 'area_ratio': round(ratio, 4)}


def fuller_captures(root, scene, obj):
    """Read existing indexed detections without extracting or altering scenes."""
    from .roof_review_loop import protected_reason
    database = root/'data/suburbs/mooroolbark/confidence.sqlite'
    if not database.exists():
        return []
    target = geographic(obj, scene)
    west, south, east, north = target.bounds
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        encoded = db.execute('SELECT payload FROM roofs WHERE east>=? AND west<=? AND north>=? AND south<=?',
                             (west, east, south, north)).fetchall()
    found = []
    for value, in encoded:
        feature = json.loads(value)
        p = feature['properties']
        if p['scene_id'] == scene['id'] or p.get('clipped'):
            continue
        match = context_match(target, shape(feature['geometry']))
        if not match:
            continue
        try:
            other = json.loads((root/'data/scenes'/p['scene_id']/'latest.json').read_text())
            proposed = next(o for o in other['objects'] if o['id'] == p['object_id'])
        except (OSError, ValueError, StopIteration):
            continue
        if other['model_signature'] != p['model_signature'] or protected_reason(root, other, proposed):
            continue
        found.append({'scene_id': other['id'], 'object_id': proposed['id'], 'model_signature': other['model_signature'],
                      'capture_title': other['title'], 'review_url': p['review_url'], **match,
                      'note': 'Possible fuller observation of this roof. Verify identity and completeness; no automatic merge.'})
    return sorted(found, key=lambda r: (-r['target_coverage'], r['area_ratio'], r['scene_id'], r['object_id']))[:3]
