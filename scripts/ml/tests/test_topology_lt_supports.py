"""Two-rectangle L/T supports (roof-topology-support-v2) and their optical gates (v3).

Synthetic outlines only: an L and a T (axis-aligned and rotated), a plain rectangle, a
courtyard, a U and an offset Z plan. The generator must offer exactly the two-rectangle
union for L/T plans, keep the single rectangle for rectangles, offer nothing for the rest,
and the stage must hand back the v5 selection byte for byte when no candidate passes.
No scene data, no labels.
"""
import json

import numpy as np
import pytest
import shapely
from PIL import Image
from scripts.ml import roof_topology
from scripts.ml.roof_structure import audit_roof, structural_prior
from scripts.ml.roof_topology import (
    TOPOLOGY_SUPPORT_VERSION,
    minimal_supports,
    optical_support,
    select_roof_topology,
    two_rectangle_supports,
)
from scripts.ml.tests.test_topology_image_support import FRAME, NOTCHED, SIZE, shaded_by_plane
from shapely.affinity import rotate
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

L = Polygon([[0, 0], [24, 0], [24, 8], [8, 8], [8, 20], [0, 20]])
T = Polygon([[0, 0], [24, 0], [24, 8], [16, 8], [16, 20], [8, 20], [8, 8], [0, 8]])
U = Polygon([[0, 0], [24, 0], [24, 20], [16, 20], [16, 8], [8, 8], [8, 20], [0, 20]])
Z = Polygon([[0, 0], [24, 0], [24, 8], [30, 8], [30, 20], [8, 20], [8, 8], [0, 8]])
RECTANGLE = Polygon([[0, 0], [24, 0], [24, 8], [0, 8]])
COURTYARD = Polygon([[0, 0], [24, 0], [24, 12], [0, 12]], [[[8, 3], [16, 3], [16, 9], [8, 9]]])


def chamfered(polygon, depth=1.2):
    """Cut every convex corner by `depth`, as traced eaves are: the skeleton then invents a
    face per chamfer, while the plan is still two rectangles."""
    ring = np.asarray(polygon.exterior.coords[:-1], float)
    out = []
    for i, q in enumerate(ring):
        p, r = ring[i-1], ring[(i+1) % len(ring)]
        cross = (q[0]-p[0])*(r[1]-q[1])-(q[1]-p[1])*(r[0]-q[0])
        if cross > 0:  # convex on a counter-clockwise ring
            out.append(q+(p-q)/np.linalg.norm(p-q)*depth)
            out.append(q+(r-q)/np.linalg.norm(r-q)*depth)
        else:
            out.append(q)
    return Polygon(out)


def planes(parts):
    return {tuple(round(p['plane'][k], 4) for k in ('a', 'b', 'c')) for p in parts}


@pytest.mark.parametrize('angle', [0, 31])
@pytest.mark.parametrize('shape,outline,corners', [('L', L, 6), ('T', T, 8)])
def test_l_and_t_outlines_produce_the_two_rectangle_support(shape, outline, corners, angle):
    footprint = rotate(outline, angle, origin=(0, 0))
    supports = minimal_supports(footprint)
    assert len(supports) == 1, [d for _, d in supports]
    support, detail = supports[0]
    assert detail['minimal_support'] and detail['shape'] == shape and detail['support_corners'] == corners
    assert detail['rectangle_coverage'] >= .999 and detail['union_iou'] >= .999
    assert len(detail['rectangles']) == 2
    rectangles = [Polygon(r) for r in detail['rectangles']]
    assert all(r.area >= .2*footprint.area for r in rectangles)
    assert unary_union(rectangles).symmetric_difference(footprint).area < .01  # 4-decimal rounding slivers
    assert support.buffer(1e-7).covers(footprint) and len(support.exterior.coords)-1 == corners
    assert detail['support_offset_m'] <= .05 and detail['max_support_deviation_m'] < .01
    # The union of two rectangles cannot be the single-rectangle path.
    assert footprint.area/footprint.minimum_rotated_rectangle.area < .85


def test_a_chamfered_l_with_a_bump_is_still_two_rectangles_and_the_support_covers_the_trace():
    # Chamfered corners lie inside the fitted union; a 0.6 m bump on the long eave lies outside it,
    # so the offered support is the union offset just far enough to cover the trace.
    footprint = chamfered(L).union(Polygon([[14, 7.9], [18, 7.9], [18, 8.6], [14, 8.6]]))
    assert len(footprint.exterior.coords)-1 == 15, 'five chamfered corners and a bump'
    supports = two_rectangle_supports(footprint)
    assert supports and supports[0][1]['shape'] == 'L'
    support, detail = supports[0]
    assert detail['union_iou'] > .9 and detail['rectangle_coverage'] > .97
    assert support.buffer(1e-7).covers(footprint) and len(support.exterior.coords)-1 == 6
    assert .6 <= detail['support_offset_m'] <= .75
    assert detail['support_offset_m'] <= detail['max_support_deviation_m']+.05 <= 1.5
    union = Polygon(detail['rectangles'][0]).union(Polygon(detail['rectangles'][1]))
    assert union.intersection(L).area/union.union(L).area > .97, 'the fitted plan is the L, not the bump'
    # Offsetting all sides keeps the ridge lines of the fitted union: the prior on the support,
    # clipped to the trace, is the six-plane L roof.
    parts = roof_topology.clip_support(structural_prior(support, 3, 5, regularize_eaves=False), footprint, 3)
    assert len(planes(parts)) == 6
    assert audit_roof(parts, footprint, 3)['passed']


def test_a_plain_rectangle_still_yields_only_the_single_rectangle():
    for footprint in (RECTANGLE, rotate(RECTANGLE, 31), Polygon(NOTCHED)):
        supports = minimal_supports(footprint)
        assert len(supports) == 1
        _, detail = supports[0]
        assert detail['support_corners'] == 4 and 'shape' not in detail and 'rectangles' not in detail
        assert two_rectangle_supports(footprint) == []


def test_courtyard_u_and_offset_plans_yield_no_two_rectangle_support():
    assert minimal_supports(COURTYARD) == []
    assert minimal_supports(U) == [] and two_rectangle_supports(U) == []
    assert minimal_supports(Z) == [] and two_rectangle_supports(Z) == []
    assert minimal_supports(rotate(U, 31)) == []


def test_the_stage_is_byte_identical_when_no_two_rectangle_candidate_passes(monkeypatch):
    footprint = chamfered(L)
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    assert len(planes(prior)) > 6, 'the chamfers must invent extra faces'
    blank = Image.new('RGB', SIZE, (130, 130, 130))
    parts, report = select_roof_topology(prior, footprint, 3, 2, blank, FRAME, prefer_simple=True)
    stage = report['support']
    assert stage['method'] == TOPOLOGY_SUPPORT_VERSION and not stage['accepted']
    assert stage['minimal_candidates'] >= 1
    offered = [c for c in stage['candidates'] if c.get('shape') == 'L']
    assert offered and all(len(c['rectangles']) == 2 and c['support_corners'] == 6 for c in offered)
    assert not any(c.get('gates') and all(c['gates'].values()) for c in stage['candidates'])
    assert report['method'] == roof_topology.VERSION and 'selected' not in stage
    monkeypatch.setattr(roof_topology, 'two_rectangle_supports', lambda *args, **kwargs: [])
    without, report_without = select_roof_topology(prior, footprint, 3, 2, blank, FRAME, prefer_simple=True)
    assert report_without['support']['minimal_candidates'] == 0
    assert json.dumps(parts, sort_keys=True, default=float) == json.dumps(without, sort_keys=True, default=float)
    assert report['after'] == report_without['after'] and report['reason'] == report_without['reason']


# The L with a V notch in its long eave: the notch defeats the axis envelope and the orthogonal
# wing cover of stage one (as NOTCHED does for the rectangle) and its skeleton invents valleys.
NOTCHED_L = Polygon([[0, 0], [10, 0], [11.5, 2.5], [13, 0], [24, 0], [24, 8], [8, 8], [8, 20], [0, 20]])


@pytest.mark.parametrize('angle', [0, 31])
def test_an_l_roof_whose_creases_are_visible_is_simplified_to_the_two_wing_roof(angle):
    ideal = rotate(L, angle, origin=(0, 0))
    footprint = rotate(NOTCHED_L, angle, origin=(0, 0))
    image = shaded_by_plane(structural_prior(ideal, 3, 5, regularize_eaves=False))
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    assert len(planes(prior)) > 6
    parts, report = select_roof_topology(prior, footprint, 3, 2, image, FRAME, prefer_simple=True)
    stage = report['support']
    assert report['accepted'] and report['method'] == TOPOLOGY_SUPPORT_VERSION, stage
    assert stage['accepted'] and stage['selected']['shape'] == 'L' and len(stage['selected']['rectangles']) == 2
    assert report['selected_support']['shape'] == 'L' and report['selected_support']['support_corners'] == 6
    assert len(planes(parts)) == 6 and report['after']['physical_planes'] == 6
    assert report['after']['supported_share'] >= .5
    audit = audit_roof(parts, footprint, 3)
    assert audit['passed'] and audit['drainage']['passed']
    assert unary_union([Polygon(p['coords']) for p in parts]).symmetric_difference(footprint).area < 1e-6


def ridge_lines_only(footprint, ideal_parts, *, roof=130, line=60, width=.35, longest_only=False):
    """Uniform roof brightness (no hip or valley contrast) with the ideal roof's ridges (or only its
    longest ridge) drawn as thin dark lines: the only optical evidence is the visible ridge line(s)."""
    from scripts.ml.roof_structure import shared_edges
    from scripts.ml.rooflines import _classify_roofline
    from scripts.ml.tests.test_topology_image_support import metres_grid
    x, y = metres_grid()
    gray = np.full(x.shape, roof, np.uint8)
    inside = shapely.contains_xy(footprint, x, y)
    ridges = [LineString(edge) for edge in shared_edges(ideal_parts) if _classify_roofline(edge, ideal_parts) == 'ridge']
    if longest_only:
        ridges = [max(ridges, key=lambda ridge: ridge.length)]
    band = unary_union(ridges).buffer(width/2)
    gray[inside & shapely.contains_xy(band, x, y)] = line
    return Image.fromarray(np.dstack([gray]*3))


def test_optical_support_records_line_and_contrast_evidence_and_explained_lines():
    footprint = NOTCHED_L
    ideal = structural_prior(L, 3, 5, regularize_eaves=False)
    image = ridge_lines_only(footprint, ideal)
    observations = roof_topology.image_segments(image, footprint, FRAME)
    assert observations, 'the drawn ridges must be observed as segments'
    candidate = roof_topology.clip_support(ideal, footprint, 3)
    optics = optical_support(candidate, footprint, observations, image, FRAME)
    kinds = {c['kind']: c for c in optics['per_crease']}
    assert optics['method'] == TOPOLOGY_SUPPORT_VERSION
    assert all(c['evidence'] in ('line', 'both') for c in optics['per_crease'] if c['kind'] == 'ridge')
    assert all(c['evidence'] is None for c in optics['per_crease'] if c['kind'] == 'hip'), kinds
    assert optics['supported_creases'] >= 2 and optics['unsupported_creases'] >= 4
    assert 0 < optics['combined_supported_share'] < .5
    assert optics['explained_line_share'] > .9 and optics['interior_line_length_m'] > 10
    blank = Image.new('RGB', SIZE, (130, 130, 130))
    nothing = optical_support(candidate, footprint, [], blank, FRAME)
    assert nothing['supported_creases'] == 0 and nothing['explained_line_share'] == 0. and nothing['interior_line_length_m'] == 0


def test_an_l_with_a_visible_ridge_but_no_hip_contrast_is_accepted_under_v3_and_refused_by_the_v2_gate():
    # Only the long wing's ridge is visible; by length that is a third of the L's creases, so the v2
    # floor (share >= 0.5) refuses it while the fan it replaces has ten unsupported creases and
    # explains none of the observed line.
    footprint = NOTCHED_L
    image = ridge_lines_only(footprint, structural_prior(L, 3, 5, regularize_eaves=False), longest_only=True)
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    parts, report = select_roof_topology(prior, footprint, 3, 2, image, FRAME, prefer_simple=True)
    stage = report['support']
    assert stage['accepted'] and report['method'] == TOPOLOGY_SUPPORT_VERSION, stage
    accepted = next(c for c in stage['candidates'] if c.get('shape') == 'L' and not c['failed_optical_gates'])
    assert accepted['accepted_by'] == 'optical' and stage['selected']['accepted_by'] == 'optical'
    assert 'supported_share' in accepted['failed_gates'], 'the v2 set refuses this plan'
    assert accepted['combined_supported_share'] < .5, 'hips and the short ridge have no evidence'
    assert accepted['v2_gate_supported_share'] is False, 'the v2 floor would refuse this plan'
    assert stage['current_optical']['explained_line_share'] < accepted['explained_line_share'] == 1.
    assert accepted['unsupported_creases'] <= stage['current_optical']['unsupported_creases']
    assert accepted['explained_line_share'] >= stage['current_optical']['explained_line_share']-.1
    evidence = stage['selected']['optical']['per_crease']
    ridges = [c['evidence'] for c in evidence if c['kind'] == 'ridge']
    assert any(e in ('line', 'both') for e in ridges) and None in ridges, 'the drawn ridge by its line, the other unseen'
    assert all(c['evidence'] is None for c in evidence if c['kind'] == 'hip')
    # Two wings, ends decided by the existing end evidence (gable or hip): 4 to 6 planes.
    assert 4 <= len(planes(parts)) <= 6 and len(planes(parts)) < len(planes(prior))
    assert audit_roof(parts, footprint, 3)['passed']


def test_an_l_with_no_visible_lines_is_refused(monkeypatch):
    footprint = NOTCHED_L
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    blank = Image.new('RGB', SIZE, (130, 130, 130))
    parts, report = select_roof_topology(prior, footprint, 3, 2, blank, FRAME, prefer_simple=True)
    stage = report['support']
    assert not stage['accepted'] and report['method'] == roof_topology.VERSION
    offered = [c for c in stage['candidates'] if c.get('shape') == 'L']
    assert offered and all('optical_evidence' in c['failed_optical_gates'] for c in offered)
    assert all('supported_share' in c['failed_gates'] and c['accepted_by'] is None for c in offered)
    assert all(c['supported_creases'] == 0 for c in offered)
    monkeypatch.setattr(roof_topology, 'two_rectangle_supports', lambda *args, **kwargs: [])
    without, report_without = select_roof_topology(prior, footprint, 3, 2, blank, FRAME, prefer_simple=True)
    assert json.dumps(parts, sort_keys=True, default=float) == json.dumps(without, sort_keys=True, default=float)
    assert report['after'] == report_without['after']


def test_single_rectangle_candidates_keep_the_v2_gates():
    from scripts.ml.tests.test_topology_image_support import RIDGE, two_tone
    footprint = Polygon(NOTCHED)
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    parts, report = select_roof_topology(prior, footprint, 3, 2, two_tone(footprint, RIDGE), FRAME, prefer_simple=True)
    stage = report['support']
    assert stage['accepted'] and stage['minimal_candidates'] == 1 and 'current_optical' not in stage
    single = next(c for c in stage['candidates'] if c.get('minimal_support'))
    assert set(single['gates']) == {'simpler', 'residual', 'supported_share', 'ranking'}
    assert 'combined_supported_share' not in single and 'shape' not in single and 'accepted_by' not in single
    assert len(planes(parts)) == 2


def test_a_plan_passing_only_the_v2_gates_is_still_accepted_with_accepted_by_contrast_share(monkeypatch):
    """The v2 set (brightness-step share and ranking) keeps accepting on its own: the optical set is
    made to fail by pretending the current fan explains every observed line."""
    ideal = structural_prior(L, 3, 5, regularize_eaves=False)
    footprint = NOTCHED_L
    image = shaded_by_plane(ideal)
    prior = structural_prior(footprint, 3, 5, regularize_eaves=False)
    real = roof_topology.optical_support

    def biased(parts, *args, **kwargs):
        result = real(parts, *args, **kwargs)
        if len(planes(parts)) > 6:  # the current fan
            result['explained_line_share'] = 1.
            result['unsupported_creases'] = 0
        else:
            result['explained_line_share'] = 0.
        return result
    monkeypatch.setattr(roof_topology, 'optical_support', biased)
    parts, report = select_roof_topology(prior, footprint, 3, 2, image, FRAME, prefer_simple=True)
    stage = report['support']
    assert stage['accepted'] and report['method'] == TOPOLOGY_SUPPORT_VERSION, stage
    assert stage['selected']['shape'] == 'L' and stage['selected']['accepted_by'] == 'contrast-share'
    chosen = next(c for c in stage['candidates'] if c.get('shape') == 'L' and c['accepted_by'] == 'contrast-share')
    assert chosen['failed_gates'] == [] and chosen['failed_optical_gates'], 'v2 set passes, optical set fails'
    assert chosen['v2_gate_supported_share'] is True
    assert len(planes(parts)) == 6
